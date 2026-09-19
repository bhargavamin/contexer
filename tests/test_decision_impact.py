"""Contract 04's bounded, opt-in, read-only decision-impact history."""

import fcntl
import json
import os

from contexer import decision_impact, store


def _enable(tmp_repo, roots=()):
    target = store.store_dir() / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[diagnostics]", "decision_impact = true"]
    if roots:
        encoded = ", ".join(json.dumps(str(root)) for root in roots)
        lines += ["", "[policy]", f"artifact_read_roots = [{encoded}]"]
    target.write_text("\n".join(lines) + "\n")


def _guidance(tmp_repo, decision_id="d1"):
    return decision_impact.guidance_envelope(
        tmp_repo, tmp_repo, route="explicit_lookup",
        rows=[{"scope": "personal", "id": decision_id, "revision_id": "r1",
               "fingerprint": "sha256:x", "authority": "approved", "tier": "full",
               "reason": "query", "files": ["src/app.py"]}])


def test_disabled_collection_creates_no_history_or_lock(tmp_repo):
    assert decision_impact.append(tmp_repo, _guidance(tmp_repo)) == ""
    assert not decision_impact.path(tmp_repo).exists()
    assert not decision_impact.lock_path(tmp_repo).exists()


def test_append_and_exact_read_are_content_free(tmp_repo):
    _enable(tmp_repo)
    envelope = _guidance(tmp_repo)
    receipt_id = decision_impact.append(tmp_repo, envelope, now=100)
    assert len(receipt_id) == 32

    result = decision_impact.report(tmp_repo, receipt_id=receipt_id, now=101)
    assert result["status"] == "ok"
    record = result["records"][0]
    assert record["decisions"][0]["revision_id"] == "r1"
    raw = decision_impact.path(tmp_repo).read_text()
    assert str(tmp_repo) not in raw
    assert "decision text" not in raw


def test_malformed_history_is_not_overwritten(tmp_repo):
    _enable(tmp_repo)
    target = decision_impact.path(tmp_repo)
    target.write_text("{broken")
    assert decision_impact.append(tmp_repo, _guidance(tmp_repo)) == ""
    assert target.read_text() == "{broken"
    assert decision_impact.report(tmp_repo)["status"] == "history_unavailable"


def test_invalid_header_integrity_is_unavailable_and_never_reset(tmp_repo):
    _enable(tmp_repo)
    target = decision_impact.path(tmp_repo)
    target.write_text(json.dumps({
        "v": 1, "epoch": "a" * 32, "cursor_secret": "z" * 64,
        "next_sequence": 1, "pruned_through": 0, "records": [],
    }))
    before = target.read_bytes()
    assert decision_impact.report(tmp_repo)["status"] == "history_unavailable"
    assert decision_impact.append(tmp_repo, _guidance(tmp_repo)) == ""
    assert target.read_bytes() == before


def test_contended_writer_fails_soft_without_changing_history(tmp_repo):
    _enable(tmp_repo)
    first = decision_impact.append(tmp_repo, _guidance(tmp_repo, "d1"))
    before = decision_impact.path(tmp_repo).read_bytes()
    fd = os.open(decision_impact.lock_path(tmp_repo), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert decision_impact.append(tmp_repo, _guidance(tmp_repo, "d2")) == ""
    finally:
        os.close(fd)
    assert first and decision_impact.path(tmp_repo).read_bytes() == before


def test_pagination_is_stable_across_new_appends_and_exact_lookup_finds_old_receipt(tmp_repo):
    _enable(tmp_repo)
    ids = [decision_impact.append(tmp_repo, _guidance(tmp_repo, f"d{i}"), now=100 + i)
           for i in range(55)]
    first = decision_impact.report(tmp_repo, limit=50, now=200)
    assert len(first["records"]) == 50 and first["next_cursor"]
    seen = [row["receipt_id"] for row in first["records"]]

    newer = decision_impact.append(tmp_repo, _guidance(tmp_repo, "new"), now=201)
    second = decision_impact.report(
        tmp_repo, limit=50, cursor=first["next_cursor"], now=202)
    seen += [row["receipt_id"] for row in second["records"]]
    assert len(seen) == len(set(seen)) == 55
    assert newer not in seen
    assert decision_impact.report(tmp_repo, receipt_id=ids[0], now=202)["status"] == "ok"


def test_clear_invalidates_an_existing_cursor(tmp_repo):
    _enable(tmp_repo)
    for i in range(3):
        decision_impact.append(tmp_repo, _guidance(tmp_repo, f"d{i}"), now=100 + i)
    page = decision_impact.report(tmp_repo, limit=1, now=110)
    assert page["next_cursor"]
    assert decision_impact.clear(tmp_repo)
    assert decision_impact.report(
        tmp_repo, cursor=page["next_cursor"], now=111)["status"] == "invalid_cursor"


def test_eviction_during_pagination_reports_history_changed(tmp_repo):
    _enable(tmp_repo)
    for i in range(decision_impact.MAX_RECORDS):
        assert decision_impact.append(tmp_repo, _guidance(tmp_repo, f"d{i}"), now=100 + i)
    first = decision_impact.report(tmp_repo, limit=1, now=500)
    assert first["next_cursor"]

    assert decision_impact.append(tmp_repo, _guidance(tmp_repo, "new"), now=501)
    changed = decision_impact.report(
        tmp_repo, cursor=first["next_cursor"], now=502)
    assert changed == {"status": "history_changed", "restart_required": True, "records": []}


def test_expired_receipt_is_hidden_without_rewriting_history(tmp_repo):
    _enable(tmp_repo)
    receipt_id = decision_impact.append(tmp_repo, _guidance(tmp_repo), now=100)
    before = decision_impact.path(tmp_repo).read_bytes()
    result = decision_impact.report(
        tmp_repo, receipt_id=receipt_id,
        now=100 + decision_impact.RETENTION_SECONDS + 1)
    assert result["status"] == "not_retained_or_unknown"
    assert decision_impact.path(tmp_repo).read_bytes() == before


def test_expiry_of_an_unvisited_page_reports_history_changed(tmp_repo):
    _enable(tmp_repo)
    for i in range(3):
        assert decision_impact.append(tmp_repo, _guidance(tmp_repo, f"d{i}"), now=float(i))
    first = decision_impact.report(
        tmp_repo, limit=1, now=decision_impact.RETENTION_SECONDS - 1)
    assert [row["sequence"] for row in first["records"]] == [3]
    assert first["next_cursor"]

    changed = decision_impact.report(
        tmp_repo, cursor=first["next_cursor"],
        now=decision_impact.RETENTION_SECONDS + 1.5)

    assert changed == {"status": "history_changed", "restart_required": True, "records": []}


def test_kind_specific_malformed_record_is_refused_and_invalidates_a_tampered_ledger(tmp_repo):
    _enable(tmp_repo)
    malformed = {"kind": "evaluation", "conditions": ["not-a-condition"]}
    assert decision_impact.append(tmp_repo, malformed, now=100) == ""

    state = decision_impact._empty()
    state["records"] = [{
        **malformed, "receipt_id": "a" * 32, "sequence": 1, "created_at": 100,
    }]
    state["next_sequence"] = 2
    decision_impact.path(tmp_repo).write_text(json.dumps(state), encoding="utf-8")

    assert decision_impact.report(tmp_repo, now=101)["status"] == "history_unavailable"


def test_malformed_and_cross_filter_cursors_are_rejected(tmp_repo):
    _enable(tmp_repo)
    for i in range(3):
        decision_impact.append(tmp_repo, _guidance(tmp_repo, f"d{i}"), now=100 + i)
    page = decision_impact.report(
        tmp_repo, files=["src/app.py"], limit=1, now=110)
    assert page["next_cursor"]
    assert decision_impact.report(
        tmp_repo, files=["src/other.py"], cursor=page["next_cursor"],
        now=111)["status"] == "invalid_cursor"
    assert decision_impact.report(
        tmp_repo, cursor="not-a-cursor", now=111)["status"] == "invalid_cursor"


def test_file_filter_selects_only_matching_metadata(tmp_repo):
    _enable(tmp_repo)
    decision_impact.append(tmp_repo, _guidance(tmp_repo, "d1"), now=100)
    assert len(decision_impact.report(
        tmp_repo, files=["src/app.py"], now=101)["records"]) == 1
    assert decision_impact.report(
        tmp_repo, files=["src/other.py"], now=101)["records"] == []


def test_page_and_text_projection_remain_below_the_response_cap(tmp_repo):
    _enable(tmp_repo)
    rows = [{
        "scope": "personal", "id": f"d{i}", "revision_id": "r" * 100,
        "fingerprint": "f" * 120, "authority": "approved", "tier": "full",
        "reason": "query", "files": [f"src/{i}.py"],
    } for i in range(32)]
    envelope = decision_impact.guidance_envelope(
        tmp_repo, tmp_repo, route="explicit_lookup", rows=rows)
    for i in range(8):
        assert decision_impact.append(tmp_repo, envelope, now=100 + i)

    page = decision_impact.report(tmp_repo, limit=50, now=200)
    assert page["next_cursor"]
    assert len(json.dumps(page, separators=(",", ":")).encode()) <= \
        decision_impact.MAX_RESPONSE_BYTES
    assert len(decision_impact.format_report(page).encode()) <= \
        decision_impact.MAX_RESPONSE_BYTES
