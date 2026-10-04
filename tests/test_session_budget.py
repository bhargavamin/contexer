import json

from contexer import store, working_set
from contexer.adapters import claude


def test_budget_keeps_constraints_before_large_title_list():
    context = "## Project rules - apply to ALL tasks in this repo:\n"
    context += "\n".join(f"- [convention] [suggested] Convention {i}: " + "é" * 70 + f" (id={i:08x})" for i in range(150))
    constraint = "- [constraint] Use config at call time (id=aaaaaaaa)\n    Never cache configuration at import time."
    context += "\n" + constraint
    result, dropped = claude.budget_session_context(context)
    assert len(result.encode("utf-8")) <= claude.SESSION_CONTEXT_BYTES
    assert len(json.dumps(result).encode()) <= claude.SESSION_CONTEXT_BYTES
    assert constraint in result
    assert "get_context" in result
    assert "aaaaaaaa" not in dropped
    assert dropped


def test_constraint_outranks_earlier_titles_that_would_fill_the_budget():
    """Rendered after the titles and too large for the slack they leave, so only priority
    (not leftover space) can keep it."""
    context = "## Project rules - apply to ALL tasks in this repo:\n"
    context += "\n".join(f"- [convention] Convention {i} " + "x" * 120 + f" (id={i:08x})" for i in range(150))
    constraint = "- [constraint] Keep secrets out of logs (id=aaaaaaaa)\n    " + ("Never log tokens. " * 150).strip()
    result, dropped = claude.budget_session_context(context + "\n" + constraint)
    assert constraint in result
    assert "aaaaaaaa" not in dropped
    assert len(json.dumps(result).encode()) <= claude.SESSION_CONTEXT_BYTES


def test_small_context_is_unchanged():
    assert claude.budget_session_context("Small context") == ("Small context", set())


def test_oversized_decision_is_omitted_whole():
    context = "## Project rules:\n- [constraint] Too big (id=aaaaaaaa)\n    " + "é" * 9000
    result, dropped = claude.budget_session_context(context)
    assert len(result.encode("utf-8")) <= claude.SESSION_CONTEXT_BYTES
    assert "Too big" not in result
    assert dropped == {"aaaaaaaa"}


def test_large_store_credits_only_delivered_rules_and_other_hosts_keep_full_context(tmp_repo):
    entries = [store._new_decision_entry(f"Convention {i}: " + "distinct " * 15, "s", "convention", status="suggested", title=f"Convention {i}: " + "é" * 65) for i in range(150)]
    constraint = store._new_decision_entry("Read configuration at call time because imports otherwise freeze the environment.", "s", "constraint", status="approved", title="Read configuration at call time")
    entries.append(constraint)
    store.save(tmp_repo, {"repo_path": tmp_repo, "entries": entries})
    bounded = store.session_start_payload(tmp_repo, "startup", "claude-s", "claude")
    other = store.session_start_payload(tmp_repo, "startup", "codex-s", "codex")
    assert len(bounded["context"].encode("utf-8")) <= claude.SESSION_CONTEXT_BYTES
    assert len(other["context"].encode("utf-8")) > claude.SESSION_CONTEXT_BYTES
    assert "imports otherwise freeze the environment" in bounded["context"]
    delivered = working_set.records(tmp_repo, "claude-s")
    assert any(r["id"] == constraint["id"] and r["fingerprint"] for r in delivered)
    assert all(r["id"][:8] in bounded["context"] for r in delivered if r["fingerprint"])
    assert "_startup_credit" not in bounded
    envelope = store.get_session_start_context(tmp_repo, "startup", "codex-real", "codex")
    assert len(envelope["hookSpecificOutput"]["additionalContext"].encode()) > claude.SESSION_CONTEXT_BYTES


def test_real_pending_conflict_keeps_resolution_guide_under_pressure(tmp_repo):
    from contexer import conflicts
    entry = store._new_decision_entry("Use PostgreSQL for durable database records.", "s", "constraint", status="approved")
    store.save(tmp_repo, {"entries": [entry]})
    store.update_decision(tmp_repo, "Use SQLite for durable database records.", "s", "constraint", replace_id=entry["id"])
    data = store.load(tmp_repo)
    assert data["entries"][0].get("proposed_revision")
    data["entries"].extend(store._new_decision_entry("Unrelated rule " + str(i), "s", "convention",
        title="Long convention " + "x" * 90, status="suggested") for i in range(150))
    store.save(tmp_repo, data)
    text = store.session_start_payload(tmp_repo, "startup", "conflicted", "claude")["context"]
    assert "Use PostgreSQL" in text and "Use SQLite" in text
    assert conflicts._CONFLICT_GUIDE in text
    assert "If the current task conflicts" in text
    assert len(json.dumps(text).encode()) <= claude.SESSION_CONTEXT_BYTES


def test_budget_preserves_section_scope_and_authority():
    context = ("## Global rules (apply to ALL repos):\n- [convention] Global rule (id=aaaaaaaa)\n"
               "## Project rules - apply to ALL tasks in this repo:\n"
               "- [constraint] Project constraint (id=bbbbbbbb)\n"
               "- [convention] Project convention (id=cccccccc)\n"
               "## Observed / AI-inferred context (not human-approved policy):\n"
               "- Observation (id=dddddddd)\n## Team context (synced)\n"
               "- [scope=team] [constraint] Team constraint (id=eeeeeeee)\n")
    context += "\n".join(f"- [scope=team] Other {i} " + "x" * 150 for i in range(100))
    text, _ = claude.budget_session_context(context)
    project = text.split("## Project rules", 1)[1].split("## Observed", 1)[0]
    assert "Project constraint" in project and "Project convention" in project
    assert text.index("## Team context") < text.index("Team constraint")
    assert text.count("## Project rules") == 1


def test_team_multiline_exception_is_one_budget_block(tmp_repo, monkeypatch):
    from contexer import team_context
    cache = {"decisions": [{"id": "aaaaaaaa", "type": "constraint", "scope": "team",
              "title": "Keep retries bounded", "content": "Always retry requests.\n- Except non-idempotent writes."}]}
    monkeypatch.setattr(team_context, "_load_cache", lambda repo: cache)
    team = team_context.format_team_section(tmp_repo)
    assert "\n- Except" not in team and "Except non-idempotent writes." in team
    text, dropped = claude.budget_session_context(team + "\n" + "\n".join(
        f"- [scope=team] Other {i} " + "x" * 200 for i in range(100)))
    assert "aaaaaaaa" not in dropped
    assert "Always retry requests." in text and "Except non-idempotent writes." in text


def test_active_bootstrap_survives_large_global_store(tmp_repo):
    entries = [store._new_decision_entry("Global rule " + str(i), "s", "constraint",
               title="Global rule " + "x" * 90, status="approved") for i in range(150)]
    store.save_global({"entries": entries})
    text = store.session_start_payload(tmp_repo, "startup", "new-project", "claude")["context"]
    assert "bootstrap_context" in text and "interpretation" in text.lower()
    assert len(json.dumps(text).encode()) <= claude.SESSION_CONTEXT_BYTES


def test_legacy_claude_envelope_does_not_claim_claude_capture(tmp_repo, monkeypatch):
    from contexer import reconcile
    seen = []
    credited = []
    original = working_set.record_deliveries
    def record(repo, session, records):
        credited.append(records)
        return original(repo, session, records)
    monkeypatch.setattr(working_set, "record_deliveries", record)
    monkeypatch.setattr(reconcile, "reconcile_session", lambda *a, **kw: seen.append(kw["host"]) or {})
    store.save(tmp_repo, {"entries": [store._new_decision_entry("Rule " + str(i) + " " + "detail " * 80, "s", "constraint",
               title="Constraint " + "x" * 100, status="approved") for i in range(150)]})
    envelope = store.get_session_start_context(tmp_repo, "startup", "legacy")
    text = envelope["hookSpecificOutput"]["additionalContext"]
    assert seen == [""]
    assert len(json.dumps(text).encode()) <= claude.SESSION_CONTEXT_BYTES
    assert len(credited) == 1 and credited[0]
    assert len(credited[0]) < 150
    assert all(r["id"][:8] in text for r in credited[0])
    assert all(r["id"][:8] in text for r in working_set.records(tmp_repo, "legacy"))


def test_rehydrated_task_context_precedes_title_overflow():
    text = "## Project rules:\n" + "\n".join(
        f"- [convention] Other {i} " + "x" * 140 for i in range(150))
    text += "\n## Rehydrated working context:\n- [architecture] Current task (id=aaaaaaaa)\n    Full current reasoning."
    rendered, dropped = claude.budget_session_context(text)
    assert "Full current reasoning." in rendered and "aaaaaaaa" not in dropped


def test_team_shown_count_uses_budgeted_output(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "_local_session_start_payload", lambda *a, **kw: {"status": "loaded", "context": ""})
    team = "## Team context\n" + "\n".join(f"- [scope=team] Rule {i} " + "x" * 200 for i in range(50))
    monkeypatch.setattr(store, "_team_section_with_counts", lambda repo: (team, 50, 0))
    payload = store.session_start_payload(tmp_repo, host="claude")
    shown = sum(line.startswith("- [scope=team]") for line in payload["context"].splitlines())
    assert 0 < shown < 50
    assert f"team: 50 synced ({shown} shown)" in payload["status"]
