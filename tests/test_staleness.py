"""Tests for staleness anchoring — source_files + anchor_commit, checked at injection."""
import os
import subprocess
from pathlib import Path

import pytest

from contexer import revisions, store


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Real git repo with one commit, STORE_DIR isolated; returns its path as a str."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    path = tmp_path / "gitrepo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "auth.py").write_text("def login(): pass\n", encoding="utf-8")
    (path / "other.py").write_text("x = 1\n", encoding="utf-8")
    _commit(path, "initial")
    return str(path)


def _commit(repo, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@test.local", "-c", "user.name=T",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", message],
        cwd=repo, check=True)


def _touch(repo: str, name: str, body: str) -> None:
    Path(repo, name).write_text(body, encoding="utf-8")
    _commit(repo, f"change {name}")


def _entry(repo: str) -> dict:
    return store.load(repo)["entries"][0]


SUMMARY = "auth flow: login() verifies the token then issues a session cookie"


def test_unchanged_file_renders_no_note(repo):
    stored, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                        source_files=["auth.py"])
    assert stored
    assert _entry(repo)["anchor_commit"]  # HEAD resolved
    assert " [may be stale" not in store.get_context(repo, query="auth")
    assert " [may be stale" not in store._render_prompt_decisions(repo, [eid])


def test_changed_file_renders_note_in_both_sites(repo):
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")

    out = store.get_context(repo, query="auth")
    assert "[may be stale: auth.py changed since capture]" in out

    rendered = store._render_prompt_decisions(repo, [eid])
    assert "[may be stale: auth.py changed since capture]" in rendered


def test_files_hit_renders_staleness_note(repo):
    """get_context(files=...) (issue #174 Task 1) is the THIRD render site that must run
    a source_files hit through the same staleness-note machinery as query/id lookup."""
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")

    out = store.get_context(repo, files=["auth.py"])
    assert "[may be stale: auth.py changed since capture]" in out


def test_uncommitted_edit_renders_note(repo):
    """The dominant case: the session is editing the file right now, nothing committed yet."""
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    Path(repo, "auth.py").write_text("def login(): return 'edited, uncommitted'\n",
                                     encoding="utf-8")
    assert "[may be stale: auth.py changed since capture]" in store.get_context(repo, query="auth")
    assert "[may be stale: auth.py changed since capture]" in store._render_prompt_decisions(repo, [eid])


def test_note_counts_extra_changed_files(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture",
                          source_files=["auth.py", "other.py"])
    _touch(repo, "auth.py", "def login(): return 1\n")
    _touch(repo, "other.py", "x = 2\n")
    out = store.get_context(repo, query="auth")
    assert "[may be stale: auth.py changed since capture, +1 more]" in out


def test_unrelated_file_change_is_not_stale(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    _touch(repo, "other.py", "x = 99\n")
    assert " [may be stale" not in store.get_context(repo, query="auth")


def test_bogus_anchor_fails_soft(repo):
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    data = store.load(repo)
    data["entries"][0]["anchor_commit"] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    store.save(repo, data)
    assert store._staleness_note(repo, data["entries"][0]) == ""
    assert " [may be stale" not in store.get_context(repo, query="auth")
    assert " [may be stale" not in store._render_prompt_decisions(repo, [eid])


def test_non_git_repo_fails_soft(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    plain = str(tmp_path / "plain")
    os.mkdir(plain)
    store.update_decision(plain, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    entry = _entry(plain)
    assert entry["source_files"] == ["auth.py"]
    assert entry["anchor_commit"] == ""  # no HEAD to resolve — files still stored
    assert " [may be stale" not in store.get_context(plain, query="auth")


def test_no_source_files_leaves_entry_unanchored(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture")
    entry = _entry(repo)
    assert "source_files" not in entry and "anchor_commit" not in entry
    _touch(repo, "auth.py", "def login(): return 2\n")
    assert " [may be stale" not in store.get_context(repo, query="auth")


def test_source_files_capped_at_ten(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture",
                          source_files=[f"m{i}.py" for i in range(25)])
    files = _entry(repo)["source_files"]
    assert len(files) == store.MAX_SOURCE_FILES == 10
    assert files[0] == "m0.py"


def test_non_string_and_blank_entries_dropped(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture",
                          source_files=["auth.py", "", None, 7, "  "])
    assert _entry(repo)["source_files"] == ["auth.py"]


def test_recurrence_does_not_reanchor(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    first = _entry(repo)["anchor_commit"]
    _touch(repo, "auth.py", "def login(): return 3\n")
    stored, _ = store.update_decision(repo, SUMMARY, "s2", "architecture",
                                      source_files=["other.py"])
    assert not stored  # duplicate -> recurrence
    entry = _entry(repo)
    assert entry["source_files"] == ["auth.py"] and entry["anchor_commit"] == first


def test_check_budget_caps_git_calls(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "run_git", lambda *a, **kw: calls.append(a) or "")
    entries = [{"id": f"e{i}", "source_files": ["a.py"], "anchor_commit": "abc"}
               for i in range(6)]
    store._staleness_notes(repo, entries)
    assert len(calls) == store._STALENESS_MAX_CHECKS == 3


def test_replace_id_with_source_files_reanchors_and_clears_note(repo):
    """A corrected summary re-captured via replace_id with fresh source_files clears the
    stale flag. Without source_files, the old anchor (and the note) is left untouched."""
    _, eid = store.update_decision(repo, SUMMARY, "s1", "convention",
                                   source_files=["auth.py"])
    old_anchor = _entry(repo)["anchor_commit"]
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")
    assert " [may be stale" in store.get_context(repo, query="auth")

    stored, _ = store.update_decision(
        repo, "auth flow: login() now returns a JWT instead of a session cookie",
        "s2", "convention", replace_id=eid, source_files=["auth.py"])
    assert stored
    entry = _entry(repo)
    assert entry["anchor_commit"] != old_anchor
    assert " [may be stale" not in store.get_context(repo, query="auth")

    # A second correction WITHOUT source_files keeps the anchor from the prior correction.
    reanchored = entry["anchor_commit"]
    _touch(repo, "auth.py", "def login(): return 'rewritten again'\n")
    stored, _ = store.update_decision(
        repo, "auth flow: login() now returns a JWT and logs the attempt",
        "s3", "convention", replace_id=eid)
    assert stored
    assert _entry(repo)["anchor_commit"] == reanchored
    assert " [may be stale" in store.get_context(repo, query="auth")


def test_identical_content_recapture_reanchors_and_clears_note(repo):
    """The no-op path: the model re-reads the changed file, confirms the summary still
    holds, and re-captures the SAME text via replace_id+source_files. The anchor must
    still refresh (there's nothing else to correct), or the note would fire forever."""
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    old_anchor = _entry(repo)["anchor_commit"]
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")
    assert " [may be stale" in store.get_context(repo, query="auth")

    stored, returned_id = store.update_decision(
        repo, SUMMARY, "s2", replace_id=eid, source_files=["auth.py"])
    assert stored and returned_id == eid
    entry = _entry(repo)
    assert entry["anchor_commit"] != old_anchor
    # no revision was created — truly a no-op on content
    assert entry["content"] == revisions.normalize_content(SUMMARY)
    assert " [may be stale" not in store.get_context(repo, query="auth")


def test_gated_correction_defers_reanchor_until_approved(repo):
    """A significant (architecture, AI-inferred) correction attaches a Suggested Update —
    the live entry keeps its OLD content as the standing one until a developer approves
    (#193 renders the proposal beside it, clearly labeled unreviewed), so its anchor must
    keep describing that old content. Only approval re-anchors."""
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    assert store.entry_status(_entry(repo)) in ("approved", "suggested")  # trusted, renders
    old_anchor = _entry(repo)["anchor_commit"]
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")
    assert " [may be stale" in store.get_context(repo, query="auth")

    new_content = "auth flow: login() now returns a JWT instead of a session cookie"
    stored, returned_id = store.update_decision(
        repo, new_content, "s2", "architecture", replace_id=eid, source_files=["auth.py"])
    assert stored and returned_id == eid
    entry = _entry(repo)
    assert entry.get("proposed_revision", {}).get("content") == revisions.normalize_content(new_content)
    # The OLD content is still what's live/rendered, and its anchor — and note — are untouched.
    assert entry["anchor_commit"] == old_anchor
    assert entry["content"] == revisions.normalize_content(SUMMARY)
    out = store.get_context(repo, query="auth")
    assert " [may be stale" in out
    # #193: the proposal IS rendered now, but only as the labeled unreviewed-update line —
    # never as the entry's standing content, which is still the old text.
    update_lines = [ln for ln in out.splitlines() if "Unreviewed update" in ln]
    assert len(update_lines) == 1 and "JWT" in update_lines[0]
    assert "JWT" not in out.replace(update_lines[0], "")

    ok, _ = store.approve_decision(repo, eid, "approve")
    assert ok
    entry = _entry(repo)
    assert entry["content"] == revisions.normalize_content(new_content)
    assert entry["anchor_commit"] != old_anchor
    assert " [may be stale" not in store.get_context(repo, query="auth")


def test_identical_content_new_title_recapture_reanchors_nongated(repo):
    """The common real shape of the recovery loop: the model re-reads the changed file,
    confirms the summary still holds, and re-captures it with the same content but a
    REGENERATED title (the MCP instructions always ask for a title, and it rarely matches
    byte-for-byte). For a non-gated subtype the title updates in place — the anchor must
    still refresh, since the live content IS the re-validated text here."""
    _, eid = store.update_decision(repo, SUMMARY, "s1", "convention",
                                   source_files=["auth.py"])
    old_anchor = _entry(repo)["anchor_commit"]
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")
    assert " [may be stale" in store.get_context(repo, query="auth")

    stored, returned_id = store.update_decision(
        repo, SUMMARY, "s2", replace_id=eid, source_files=["auth.py"],
        title="Login issues a session cookie after verifying the token")
    assert stored and returned_id == eid
    entry = _entry(repo)
    assert entry["anchor_commit"] != old_anchor
    assert entry["title"] == "Login issues a session cookie after verifying the token"
    assert " [may be stale" not in store.get_context(repo, query="auth")
    assert " [may be stale" not in store._render_prompt_decisions(repo, [eid])


def test_identical_content_new_title_recapture_reanchors_gated(repo):
    """Same recovery-loop shape as above, but on a gated (architecture, AI-inferred)
    decision: the title change is deferred to a proposal, but the content itself is
    unchanged and re-validated right now, so the anchor must refresh immediately —
    not wait for the title proposal to be approved."""
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    old_anchor = _entry(repo)["anchor_commit"]
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")
    assert " [may be stale" in store.get_context(repo, query="auth")

    stored, returned_id = store.update_decision(
        repo, SUMMARY, "s2", "architecture", replace_id=eid, source_files=["auth.py"],
        title="Login issues a session cookie after verifying the token")
    assert stored and returned_id == eid
    entry = _entry(repo)
    assert entry.get("proposed_revision", {}).get("title") == \
        "Login issues a session cookie after verifying the token"
    assert entry["anchor_commit"] != old_anchor  # content is unchanged, so this applies now
    assert " [may be stale" not in store.get_context(repo, query="auth")
    assert " [may be stale" not in store._render_prompt_decisions(repo, [eid])


def test_session_start_payload_never_shows_staleness_note(repo):
    """session_start_payload is never one of the two render sites _staleness_notes runs
    at — a changed source file must not surface a note there."""
    store.update_decision(repo, SUMMARY, "s1", "convention", source_files=["auth.py"])
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")
    payload = store.session_start_payload(repo)
    assert " [may be stale" not in payload["context"]


def test_legacy_entry_without_fields_round_trips(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture")
    before = store.load(repo)["entries"]
    store.save(repo, {"repo_path": repo, "entries": before})
    after = store.load(repo)["entries"]
    assert after == before
    assert "No matching" not in store.get_context(repo, query="auth")


# ── approval-time anchoring (issue #172 Task 2) ─────────────────────────────────

def test_approve_already_anchored_entry_refreshes_anchor_commit_and_clears_note(repo):
    """A decision captured with source_files while still pending_approval is already
    anchored. Approving it later (no source_files param — the param is independent of
    this) is a human revalidation of the content: anchor_commit refreshes to current
    HEAD, and a stale note picked up in between clears."""
    _, eid = store.update_decision(repo, SUMMARY, "s1", "constraint",
                                   source_files=["auth.py"])
    entry = _entry(repo)
    assert entry["status"] == "pending_approval"
    old_anchor = entry["anchor_commit"]
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")
    assert " [may be stale" in store.get_context(repo, query="auth")

    ok, _msg = store.approve_decision(repo, eid, "approve")
    assert ok
    entry = _entry(repo)
    assert entry["status"] == "approved"
    assert entry["anchor_commit"] != old_anchor
    assert " [may be stale" not in store.get_context(repo, query="auth")


def test_approve_with_source_files_param_anchors_and_clears_future_staleness(repo):
    """approve_decision(source_files=...) anchors a not-yet-anchored entry (source_files
    stored, anchor_commit set to current HEAD), and the anchor takes effect immediately —
    a later change to that file surfaces the staleness note."""
    stored, eid = store.update_decision(repo, "auth flow: login() verifies the token", "s1",
                                        "constraint")
    assert stored
    entry = _entry(repo)
    assert entry["status"] == "pending_approval"
    assert "source_files" not in entry

    ok, _msg = store.approve_decision(repo, eid, "approve", source_files=["auth.py", "other.py"])
    assert ok
    entry = _entry(repo)
    assert entry["source_files"] == ["auth.py", "other.py"]
    assert entry["anchor_commit"]
    assert " [may be stale" not in store.get_context(repo, query="auth")

    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")
    assert " [may be stale" in store.get_context(repo, query="auth")


def test_share_projection_carries_source_files_but_never_anchor_commit(repo):
    """Wire-safety (updated for issue #174 Task 5): approval-time anchoring now
    deliberately projects `source_files` (the anchored files, for the preview/outbox)
    but `anchor_commit` — a machine-local ref — must still never leak onto the push
    wire shape."""
    stored, eid = store.update_decision(repo, SUMMARY, "s1", "constraint")
    assert stored
    store.approve_decision(repo, eid, "approve", source_files=["auth.py"])
    entry = _entry(repo)
    assert entry["source_files"] and entry["anchor_commit"]
    projected = store._share_projection(entry, redact_on=False)
    assert projected["source_files"] == entry["source_files"]
    assert "anchor_commit" not in projected


# ── _anchor_sources canonicalization (M6, issue #172 fix wave) ─────────────────

def test_capture_time_absolute_path_canonicalized_to_repo_relative(repo):
    """An absolute-path spelling passed to update_decision's source_files must be
    canonicalized to the repo-relative POSIX form _anchor_sources stores — a raw
    absolute path would break _staleness_note's `git diff -- <path>` once the repo
    moves, and would never guard-pair against a staged (relative) path."""
    abs_path = str(Path(repo) / "auth.py")
    stored, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                        source_files=[abs_path])
    assert stored
    entry = _entry(repo)
    assert entry["source_files"] == ["auth.py"]
    assert entry["anchor_commit"]

    # Staleness reads the canonicalized path correctly.
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")
    assert " [may be stale" in store.get_context(repo, query="auth")


def test_approval_time_absolute_path_canonicalized_to_repo_relative(repo):
    """Same canonicalization at the approve_decision(source_files=...) call site."""
    stored, eid = store.update_decision(repo, SUMMARY, "s1", "constraint")
    assert stored
    abs_path = str(Path(repo) / "auth.py")
    ok, msg = store.approve_decision(repo, eid, "approve", source_files=[abs_path])
    assert ok, msg
    entry = _entry(repo)
    assert entry["source_files"] == ["auth.py"]
    assert entry["anchor_commit"]


def test_unresolvable_path_is_dropped_not_stored_raw(repo):
    """A path that fails canonicalization (e.g. an embedded NUL byte) is dropped
    rather than stored verbatim; if it's the only file given, the anchor is a no-op."""
    stored, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                        source_files=["bad\x00path.py"])
    assert stored
    entry = _entry(repo)
    assert "source_files" not in entry and "anchor_commit" not in entry


def test_absolute_path_anchor_pairs_correctly_in_guard(repo):
    """The canonicalized anchor must be exactly what the guard's own _guard_relpath
    produces for a staged file, so a decision captured with an absolute-path spelling
    still pairs at commit time."""
    from contexer import guard_engine

    abs_path = str(Path(repo) / "auth.py")
    stored, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                        source_files=[abs_path], created_by="human")
    assert stored
    entry = _entry(repo)
    assert entry["status"] == "approved"
    assert entry["source_files"] == [guard_engine._guard_relpath(repo, abs_path)]

    Path(repo, "auth.py").write_text("def login(): return 'rewritten'\n", encoding="utf-8")
    subprocess.run(["git", "add", "auth.py"], cwd=repo, check=True)
    result = guard_engine.guard_staged(repo)
    assert any(a["decision_id"] == eid for a in result["advisories"])


def test_outside_repo_absolute_path_dropped_not_stored_as_dotdot(repo):
    """An absolute path outside the repo canonicalizes (via os.path.relpath) to a
    "../"-prefixed string, which can never match a repo-relative staged path (guard
    pairing dead) and which git diff rejects/ignores (staleness dead). It must be
    dropped, not stored — same empty-anchor outcome as an unresolvable path."""
    outside = str(Path(repo).parent / "outside.py")
    stored, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                        source_files=[outside])
    assert stored
    entry = _entry(repo)
    assert "source_files" not in entry and "anchor_commit" not in entry


def test_relative_escape_spelling_dropped_not_stored(repo):
    """A "../escape" relative spelling that resolves outside the repo is dropped the
    same way as an absolute outside-repo path."""
    stored, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                        source_files=["../outside.py"])
    assert stored
    entry = _entry(repo)
    assert "source_files" not in entry and "anchor_commit" not in entry


def test_outside_repo_path_dropped_but_in_repo_siblings_still_anchor(repo):
    """When one source_files entry escapes the repo and another is in-repo, only the
    escaping one is dropped — the in-repo file still anchors normally (mirrors the
    existing "drop unresolvable, keep the rest" behavior)."""
    outside = str(Path(repo).parent / "outside.py")
    stored, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                        source_files=[outside, "auth.py"])
    assert stored
    entry = _entry(repo)
    assert entry["source_files"] == ["auth.py"]
    assert entry["anchor_commit"]


# ── review anchor provenance (interactive review surface) ──────────────────────
# `contexer review` shows which commit a decision was anchored to, so the developer
# can see what work was in flight when it was captured. No PR number is stored
# anywhere — it is read off the anchor commit's own subject, which squash/merge
# commits carry as "(#123)" / "Merge pull request #123".

def test_anchor_note_empty_without_anchor(repo):
    assert store.review_anchor_note(repo, {}) == ""
    assert store.review_anchor_note(repo, {"anchor_commit": ""}) == ""


def test_anchor_note_renders_short_sha_and_subject(repo):
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    entry = _entry(repo)
    note = store.review_anchor_note(repo, entry)
    assert entry["anchor_commit"][:7] in note
    assert "initial" in note


def test_anchor_note_surfaces_pr_number_from_subject(repo):
    _touch_pr = Path(repo, "feature.py")
    _touch_pr.write_text("x = 2\n", encoding="utf-8")
    _commit(repo, "feat: add the thing (#211)")
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    note = store.review_anchor_note(repo, _entry(repo))
    assert "(#211)" in note


def test_anchor_note_failsoft_on_unknown_commit(repo):
    note = store.review_anchor_note(repo, {"anchor_commit": "0" * 40})
    assert note == ""


# ── review metadata rows (need a REAL git repo, hence this module) ─────────────
# The CLI's tmp_repo fixture isn't a git checkout, so source_files/anchor_commit are
# never stamped there and these branches would otherwise go untested.

def _rows(repo, entry):
    from contexer import cli
    return dict(cli._review_metadata(repo, entry))


def test_review_metadata_shows_files_and_staleness_warning(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    entry = _entry(repo)
    assert _rows(repo, entry)["Files"] == "auth.py"
    assert "changed since capture" not in " ".join(_rows(repo, entry).values())

    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")
    warning = [v for k, v in _rows(repo, entry).items() if k == ""]
    assert warning and "auth.py changed since capture" in warning[0]


def test_review_metadata_shows_anchor_commit(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    entry = _entry(repo)
    assert entry["anchor_commit"][:7] in _rows(repo, entry)["Anchor"]


def test_review_metadata_seen_row_only_once_corroborated(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    entry = _entry(repo)
    assert "Seen" not in _rows(repo, entry)          # a single sighting says nothing

    entry["occurrence_count"] = 3
    entry["session_ids"] = ["s1", "s2"]
    assert _rows(repo, entry)["Seen"] == "3 times across 2 sessions"


def test_review_metadata_captured_row_humanises_origin(repo):
    """Origin moved to the shared impact block (Task 07) so all three review surfaces render
    one wording; the metadata row keeps the capture TIME, and the humanised origin is asserted
    where it now lives."""
    from contexer import review_impact

    store.update_decision(repo, SUMMARY, "s1", "architecture")
    entry = _entry(repo)
    assert _rows(repo, entry)["Captured"] == (entry["timestamp"] or "")[:16].replace("T", " ")
    assert "Origin: captured by the assistant" in review_impact.impact_lines(
        review_impact.review_impact(repo, entry))


def test_print_wrapped_preserves_paragraph_breaks(capsys):
    from contexer import cli
    cli._print_wrapped("First paragraph.\n\nSecond paragraph.")
    out = capsys.readouterr().out
    assert "First paragraph." in out and "Second paragraph." in out
    assert "\n\n" in out          # the blank line between them survived


# ── review git budget (Greptile #218) ─────────────────────────────────────────
# The review loop is human-paced (<=2 git calls per screen, not one long stall), but it is
# uncapped, and this repo's 2s git timeouts DO fire under load. Two guards: memoise per
# anchor so a queue captured in one session costs one lookup, and stop spending once a wall
# -clock budget is gone — saying so, rather than rendering a bare row that reads as "no data"
# (the honest-on-exhaustion rule anchors.py already establishes).

def test_review_metadata_memoises_repeated_anchor(repo, monkeypatch):
    from contexer import cli
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    entry = _entry(repo)

    calls = []
    real = store.run_git
    monkeypatch.setattr(store, "run_git", lambda *a, **k: (calls.append(a), real(*a, **k))[1])

    budget = cli._review_git_budget()
    cli._review_metadata(repo, entry, budget)
    first = len(calls)
    assert first >= 1
    cli._review_metadata(repo, entry, budget)      # same anchor + same files
    assert len(calls) == first                      # served entirely from the memo


def test_review_metadata_stops_spending_once_budget_is_gone(repo, monkeypatch):
    from contexer import cli
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    entry = _entry(repo)

    budget = cli._review_git_budget()
    budget["left"] = 0.0                            # pretend git already ate the budget

    called = []
    monkeypatch.setattr(store, "run_git", lambda *a, **k: called.append(a))
    rows = cli._review_metadata(repo, entry, budget)

    assert called == []                             # no further git work
    assert budget["skipped"] is True
    assert any("git is slow" in v for v in dict(rows).values())   # and it SAYS so


def test_review_metadata_without_budget_still_works(repo):
    """A bare call (no budget passed) keeps the simple uncapped behaviour."""
    from contexer import cli
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    rows = dict(cli._review_metadata(repo, _entry(repo)))
    assert "Anchor" in rows and "Files" in rows


def test_fast_git_never_trips_the_budget(repo):
    """A normal warm repo must render every row for a long queue — the failure mode of a
    call-count cap (like _STALENESS_MAX_CHECKS) would be hiding accuracy rows when git is fine."""
    from contexer import cli
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    entry = _entry(repo)
    budget = cli._review_git_budget()
    for _ in range(30):
        entry = dict(entry, anchor_commit=entry["anchor_commit"], id=f"x{_}")
        rows = dict(cli._review_metadata(repo, entry, budget))
    assert budget["skipped"] is False
    assert "Anchor" in rows


def test_review_screen_makes_at_most_two_git_calls(repo, monkeypatch):
    """Decision 9b71289b requires any expansion of the git-in-render exception to be
    re-measured before shipping. Measured on this repo's real store: `git log -1` costs
    p50 12.3ms / p95 14.7ms — the SAME as the sanctioned `git diff --name-only` (12.4ms) —
    so a review screen's worst case is ~29ms, imperceptible against a keypress.

    Pinned by CALL COUNT rather than wall clock deliberately: git subprocess timing is
    exactly what this repo's fail-soft 2s timeouts exist for, so a latency assertion here
    would flake under CI load. Count is deterministic and catches the real regressions —
    a third git call per screen, or a broken memo."""
    from contexer import cli
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    entry = _entry(repo)

    calls = []
    real = store.run_git
    monkeypatch.setattr(store, "run_git", lambda *a, **k: (calls.append(a[1]), real(*a, **k))[1])

    cli._review_metadata(repo, entry, cli._review_git_budget())
    assert len(calls) <= 2, f"one review screen must cost at most 2 git calls, got {calls}"


def _count_git(monkeypatch):
    """Record the git SUBCOMMAND of every call ('log' / 'diff'), passing through to real git."""
    calls = []
    real = store.run_git
    monkeypatch.setattr(store, "run_git", lambda *a, **k: (calls.append(a[1]), real(*a, **k))[1])
    return calls


def _fake_entry(sha, i, files):
    return {"id": f"d{i}", "timestamp": "2026-08-16T10:00:00", "created_by": "ai",
            "anchor_commit": sha, "source_files": files}


def test_memoised_queue_collapses_the_anchor_lookup(repo, monkeypatch):
    """The anchor subject depends only on the sha, so a queue sharing an anchor_commit must
    cost exactly ONE `git log` however many decisions it holds — that is what makes the
    uncapped loop safe.

    Deliberately uses decisions with DIFFERENT source_files: an earlier version of this test
    reused one identical entry, which passed trivially and would have kept passing even if the
    memo were keyed on the whole entry (caught in review on #219). Staleness is NOT collapsed
    across file sets, and must not be — it genuinely differs per file, so each distinct file
    set is its own `git diff`."""
    from contexer import cli
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    sha = _entry(repo)["anchor_commit"]

    calls = _count_git(monkeypatch)
    budget = cli._review_git_budget()
    for i in range(25):
        cli._review_metadata(repo, _fake_entry(sha, i, [f"f{i}.py"]), budget)

    assert calls.count("log") == 1, f"one shared anchor must cost one log, got {calls.count('log')}"
    assert calls.count("diff") == 25          # per-file-set, correctly not collapsed
    assert budget["skipped"] is False         # 26 calls still well inside the time budget


def test_memoised_queue_collapses_both_when_anchor_and_files_match(repo, monkeypatch):
    """The common case — a queue captured in one session over the same files — collapses to
    a single lookup of each kind."""
    from contexer import cli
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    sha = _entry(repo)["anchor_commit"]

    calls = _count_git(monkeypatch)
    budget = cli._review_git_budget()
    for i in range(25):
        cli._review_metadata(repo, _fake_entry(sha, i, ["auth.py"]), budget)

    assert calls.count("log") == 1 and calls.count("diff") == 1
    assert budget["skipped"] is False
