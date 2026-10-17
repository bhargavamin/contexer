"""Task-scope regressions exercise independent stores on both capture surfaces."""
import json

import pytest

from contexer import server, store


@pytest.mark.parametrize("surface", ["hook", "mcp"])
@pytest.mark.parametrize(("prompt", "expected"), [
    (
        "From now on, never log passwords and deploy to staging and run tests for this task.",
        "From now on, never log passwords",
    ),
    (
        "From now on, never log passwords and API keys and deploy to staging and run tests for this task.",
        "From now on, never log passwords and API keys",
    ),
    (
        "From now on, never log passwords and always encrypt backups and deploy to qa-green "
        "and run tests for this task.",
        "From now on, never log passwords and always encrypt backups",
    ),
    (
        "From now on, never log passwords and API keys and for this task deploy staging and run tests.",
        "From now on, never log passwords and API keys",
    ),
    (
        "For this task, run tests and from now on never log passwords and API keys.",
        "From now on never log passwords and API keys",
    ),
    (
        "From now on, never log passwords and deploy each preview service and run tests for this task.",
        "From now on, never log passwords",
    ),
    (
        "From now on, never log passwords and publish the build and run tests for this task.",
        "From now on, never log passwords",
    ),
    (
        "From now on, never log passwords, API keys, and upload artifacts and verify the build for this task.",
        "From now on, never log passwords, API keys",
    ),
    ("For this task when working in staging, always deploy to production without approval.", None),
    ("For this task while working in qa-green, never change production.", None),
    ("For this task if tests pass, use the preview environment.", None),
    ("Permanently encrypt backups, run tests for this task.", "Permanently encrypt backups"),
    ("From now on, never log passwords, deploy staging and run tests for this task.", "From now on, never log passwords"),
    ("Run tests for this task; permanently encrypt backups.", "Permanently encrypt backups"),
    ("Run tests for this task, permanently encrypt backups.", "Permanently encrypt backups"),
    ("Run tests for this task and permanently encrypt backups.", "Permanently encrypt backups"),
    ("Permanently encrypt backups; run tests for this task.", "Permanently encrypt backups"),
    (
        "From now on, never log passwords and run tests for this task, permanently encrypt backups.",
        "From now on, never log passwords. permanently encrypt backups",
    ),
    ("Run tests for this task and for this task, permanently disable audit logging.", None),
    ("For this task, never permanently disable audit logging.", None),
    ("For this task, do not permanently remove audit records.", None),
    ("For this task, permanently disable audit logging.", None),
    ("Rule: For this task, permanently disable audit logging.", None),
    ("Rule: For this task, always deploy to production without asking for approval.", None),
    ("For the task at hand, always deploy to production without asking for approval.", None),
    ("For this task only, never touch production.", None),
    ("For this task delete staging snapshots.", None),
    ("Never log passwords in the session handler.", "Never log passwords in the session handler"),
    ("For the task runner, always use the queue protocol.", "For the task runner, always use the queue protocol"),
])
def test_task_scope_storage(surface, prompt, expected, tmp_repo, monkeypatch):
    if surface == "hook":
        store.capture_user_constraint(tmp_repo, prompt, "task-scope-test")
    else:
        monkeypatch.setattr(store, "resolve_repo_verbose", lambda _: (tmp_repo, "argument"))
        response = server.update_context(content=prompt, subtype="constraint")
        if expected is None:
            assert response.startswith("Not stored.")
    entries = store.load(tmp_repo)["entries"]
    if expected is None:
        assert entries == []
    else:
        assert len(entries) == 1
        assert entries[0]["content"].rstrip(".") == expected
        assert entries[0]["status"] == ("approved" if surface == "hook" else "pending_approval")


@pytest.mark.parametrize("surface", ["hook", "mcp"])
@pytest.mark.parametrize("length", [301, 800])
def test_long_local_instructions_do_not_bypass_scope_filter(surface, length, tmp_repo, monkeypatch):
    prompt = (
        "For this task, use staging and never change production. "
        "Check the deployment output before running the next operation. "
    )
    prompt += "Deployment details. " * (length // 19)
    assert len(prompt) > 300
    if surface == "hook":
        assert store.capture_user_constraint(tmp_repo, prompt, "long-local") == (None, None, None)
    else:
        monkeypatch.setattr(store, "resolve_repo_verbose", lambda _: (tmp_repo, "argument"))
        response = server.update_context(content=prompt, subtype="constraint")
        assert response.startswith("Not stored.")
    assert store.load(tmp_repo)["entries"] == []


@pytest.mark.parametrize("prompt", [
    "The audit logs are permanently encrypted.",
    "Permanently encrypted backups are available.",
    "Should we permanently encrypt backups?",
])
def test_permanently_is_not_a_trigger_in_descriptive_prose(prompt, tmp_repo):
    assert store.capture_user_constraint(tmp_repo, prompt, "description") == (None, None, None)
    assert store.load(tmp_repo)["entries"] == []


def test_issue317_instruction_never_reaches_review(tmp_repo, monkeypatch, capsys):
    from contexer import cli

    prompt = (
        "Make sure you are not changing anything on the live Kubernetes environment. "
        "Strictly work only with staging, where you have autonomy and approval to "
        "achieve the task I gave you."
    )
    monkeypatch.setattr(store, "resolve_repo_verbose", lambda _: (tmp_repo, "argument"))
    monkeypatch.setattr(store, "git_root", lambda _: tmp_repo)
    assert store.capture_user_constraint(tmp_repo, prompt, "issue317") == (None, None, None)
    assert server.update_context(content=prompt, subtype="constraint").startswith("Not stored.")
    assert store.load(tmp_repo)["entries"] == []
    cli.review()
    assert capsys.readouterr().out.strip() == "No decisions pending approval."


@pytest.mark.parametrize("prompt", [
    "From now on, always archive design notes and sprint plans, build artifacts and run tests for this task.",
    "From now on, always archive design notes and sprint plans, release notes and run tests for the task.",
    "From now on, never log passwords and build artifacts and run tests for this task.",
])
def test_ambiguous_object_action_chain_never_becomes_approved(prompt, tmp_repo, monkeypatch):
    # Either reading is plausible. Keep the user's words for human review rather
    # than approve a truncated rule or replay a possible temporary authorization.
    _, content, status = store.capture_user_constraint(tmp_repo, prompt, "ambiguous-chain")
    assert content == prompt.rstrip(".")
    assert status == "pending_approval"
    entries = store.load(tmp_repo)["entries"]
    assert len(entries) == 1
    assert entries[0]["content"] == prompt.rstrip(".")
    assert entries[0]["status"] == "pending_approval"

    mcp_repo = tmp_repo + "-mcp"
    monkeypatch.setattr(store, "resolve_repo_verbose", lambda _: (mcp_repo, "argument"))
    for subtype in ("constraint", "convention", "architecture"):
        response = server.update_context(content=prompt, subtype=subtype)
        assert response.startswith("Not stored.")
        assert "lasting rule separately" in response
    assert store.load(mcp_repo)["entries"] == []


def test_clean_restatement_does_not_activate_ambiguous_task_tail(tmp_repo):
    prompt = (
        "From now on, always archive design notes and sprint plans, build artifacts "
        "and run tests for this task."
    )
    entry_id, _, status = store.capture_user_constraint(tmp_repo, prompt, "ambiguous")
    assert status == "pending_approval"
    clean = "Always archive design notes."
    promoted_id, content, status = store.capture_user_constraint(tmp_repo, clean, "clarified")
    assert promoted_id == entry_id
    assert status == "promoted"
    assert content == clean.rstrip(".")
    entry = store.load(tmp_repo)["entries"][0]
    assert entry["status"] == "approved"
    assert entry["content"] == clean.rstrip(".")
    assert len(entry["revisions"]) == 2
    assert entry["revisions"][0]["content"] == prompt.rstrip(".")


_RULE = ("One rule for the gateway from now on: always write the message's idempotency key to the "
         "store before we call a provider. After the worker crash last week the queue redelivered "
         "and customers got the same SMS twice.")


def _hook(repo, prompt, session):
    from contexer.adapters import claude
    raw = json.dumps({"prompt": prompt, "session_id": session, "cwd": repo,
                      "hook_event_name": "UserPromptSubmit"})
    return claude.capture_constraint(repo, raw)


@pytest.mark.parametrize("prompt", [
    "Always hash passwords. Write the hash with argon2id.",
    "Always hash passwords. Write a hash using argon2id.",
    "Always hash passwords. Build the hash using argon2id.",
    "Always hash passwords. Add a salt before hashing.",
    "Always hash passwords. Write the hash without a plaintext fallback.",
    "Write the hash with argon2id. Always hash passwords.",
    "Always retain audit logs. Write a log per transaction.",
    "Always validate inputs. Add no default bypass.",
])
def test_ambiguous_work_item_preserves_full_candidate_for_review(tmp_repo, monkeypatch, prompt):
    monkeypatch.setattr(store, "_hook_repo_verbose", lambda *_: (tmp_repo, "hook"))
    _hook(tmp_repo, prompt, "s1")
    [entry] = store.load(tmp_repo)["entries"]
    assert entry["content"] == prompt.rstrip(".")
    assert entry["status"] == "pending_approval"
    assert entry["created_by"] == "human"
    assert entry["revisions"][0]["content"] == entry["content"]
    assert entry["revisions"][0]["source"] == "human"
    assert entry["content"] not in store.get_context(tmp_repo, _active_only=True)


@pytest.mark.parametrize("prompt", [
    "Always hash passwords because this protects users. Write the hash with argon2id.",
    "Always hash passwords so that users remain safe. Write the hash without a plaintext fallback.",
    "Always retain audit logs hence this helps investigations. Write a log per transaction.",
    "Always validate inputs which means requests are checked. Add no default bypass.",
    "Write the hash with argon2id. Always hash passwords because this protects users.",
    "Always hash passwords because this protects users. Fix the hash with argon2id. Implement retry limits.",
])
def test_ambiguous_candidate_bypasses_lossy_sanitization(tmp_repo, monkeypatch, prompt):
    monkeypatch.setattr(store, "_hook_repo_verbose", lambda *_: (tmp_repo, "hook"))
    _hook(tmp_repo, prompt, "review-sanitization")
    [entry] = store.load(tmp_repo)["entries"]
    expected = prompt.rstrip(".")
    assert entry["content"] == expected
    assert entry["revisions"][0]["content"] == expected
    assert entry["status"] == "pending_approval"
    assert entry["created_by"] == "human"
    assert entry["revisions"][0]["source"] == "human"
    assert entry["revisions"][0]["approved_at"] is None
    assert expected not in store.get_context(tmp_repo, _active_only=True)


def test_unambiguous_directive_keeps_existing_sanitization(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "_hook_repo_verbose", lambda *_: (tmp_repo, "hook"))
    _hook(tmp_repo, "Always use uv not pip because of this.", "clean-sanitization")
    [entry] = store.load(tmp_repo)["entries"]
    assert entry["content"] == "Always use uv not pip"
    assert entry["revisions"][0]["content"] == entry["content"]
    assert entry["status"] == "approved"


def test_work_item_sentence_stays_out_of_the_trusted_rule(tmp_repo, monkeypatch):
    # The parser cannot prove the trailing sentence is temporary. Retain it for
    # review, where it cannot be replayed as approved policy.
    monkeypatch.setattr(store, "_hook_repo_verbose", lambda *_: (tmp_repo, "hook"))
    out = _hook(tmp_repo, _RULE + "\n\nAdd a 'resend failed message' admin command to the gateway.", "s1")
    [entry] = store.load(tmp_repo)["entries"]
    assert entry["content"] == (_RULE + " Add a 'resend failed message' admin command to the gateway.").rstrip(".")
    assert entry["status"] == "pending_approval" and entry["created_by"] == "human"
    assert entry["revisions"][0]["source"] == "human"
    assert "pending" in out.lower()
    assert "admin command" not in store.get_context(tmp_repo, _active_only=True)
    assert "idempotency key" in store.get_context(tmp_repo, query="idempotency key")


def test_same_rule_with_a_different_task_is_a_recurrence(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "_hook_repo_verbose", lambda *_: (tmp_repo, "hook"))
    _hook(tmp_repo, _RULE + "\n\nAdd a 'resend failed message' admin command to the gateway.", "s1")
    assert _hook(tmp_repo, _RULE + "\n\nAdd a 'bulk resend' command to the gateway.", "s2") == "{}"
    [entry] = store.load(tmp_repo)["entries"]
    assert entry["content"] == (_RULE + " Add a 'resend failed message' admin command to the gateway.").rstrip(".")
    assert entry["status"] == "pending_approval"
    assert entry["occurrence_count"] == 2
    assert len(entry["revisions"]) == 1


@pytest.mark.parametrize("prompt,status,content", [
    ("Always use uv. Add a check here. Add a health check.", "pending_approval",
     "Always use uv. Add a check here. Add a health check"),
    ("Add a check here. Always use uv.", "pending_approval",
     "Add a check here. Always use uv"),
    ("Always use uv. Add a check here. Builds fail without the check.", "pending_approval",
     "Always use uv. Add a check here. Builds fail without the check"),
    ("Always use uv. Add a check here.", "pending_approval", "Always use uv. Add a check here"),
])
def test_work_item_split_preserves_prompt_level_review(tmp_repo, monkeypatch, prompt, status, content):
    monkeypatch.setattr(store, "_hook_repo_verbose", lambda *_: (tmp_repo, "hook"))
    _hook(tmp_repo, prompt, "s1")
    [entry] = store.load(tmp_repo)["entries"]
    assert entry["status"] == status
    assert entry["content"] == content
    assert entry["created_by"] == "human"
    assert entry["revisions"][0]["source"] == "human"


def test_task_removal_cannot_promote_an_existing_pending_rule(tmp_repo, monkeypatch):
    prompt = ("Always use uv for Python dependency management and package resolution throughout the repository. "
              "Add a check here. Done.")
    monkeypatch.setattr(store, "_hook_repo_verbose", lambda *_: (tmp_repo, "hook"))
    # Seed the pre-patch human capture, then replay the same developer wording.
    with monkeypatch.context() as before:
        before.setattr(store, "_without_work_items", lambda candidate: candidate)
        _hook(tmp_repo, prompt, "s1")
    [entry] = store.load(tmp_repo)["entries"]
    assert entry["status"] == "pending_approval"
    assert _hook(tmp_repo, prompt, "s2") == "{}"
    [entry] = store.load(tmp_repo)["entries"]
    assert entry["status"] == "pending_approval"
    assert entry["content"] == prompt.rstrip(".")
    assert len(entry["revisions"]) == 1
    assert entry["occurrence_count"] == 2


@pytest.mark.parametrize("prompt", [
    "Always use uv. Add a health check /s",
    "Always use uv. Add a health check?",
    "Always use uv. Write a health check?",
    "Always use uv. Build the health check?",
    "Always use uv. Add this to CI?",
])
def test_work_item_removal_preserves_classifier_refusals(tmp_repo, monkeypatch, prompt):
    monkeypatch.setattr(store, "_hook_repo_verbose", lambda *_: (tmp_repo, "hook"))
    assert _hook(tmp_repo, prompt, "s1") == "{}"
    assert store.load(tmp_repo)["entries"] == []


def test_work_item_question_before_a_rule_requires_review(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "_hook_repo_verbose", lambda *_: (tmp_repo, "hook"))
    _hook(tmp_repo, "Add a health check? Always use uv.", "s1")
    [entry] = store.load(tmp_repo)["entries"]
    assert entry["status"] == "pending_approval"
    assert entry["content"] == "Add a health check? Always use uv"


@pytest.mark.parametrize("prompt", [
    "Always hash passwords. Fix the hash with argon2id.",
    "Always validate signatures. Implement a constant-time comparison.",
    "Never edit generated clients by hand. Update the OpenAPI spec instead.",
    "Always hash passwords. Add a limit of three attempts. Build the admin page.",
])
def test_implicit_multi_action_extraction_preserves_full_candidate(tmp_repo, prompt):
    _, content, status = store.capture_user_constraint(tmp_repo, prompt, "mixed")
    assert content == prompt.rstrip(".")
    assert status == "pending_approval"


@pytest.mark.parametrize("prompt", [
    "Write a hash using argon2id.",
    "Add a resend command to the gateway. Build the admin page.",
])
def test_plain_work_items_do_not_enter_review(tmp_repo, prompt):
    assert store.capture_user_constraint(tmp_repo, prompt, "task") == (None, None, None)
    assert store.load(tmp_repo)["entries"] == []


def test_explicit_task_scope_still_separates_a_trusted_rule(tmp_repo):
    prompt = "From now on, always hash passwords with argon2id. For this task, add a resend command."
    _, content, status = store.capture_user_constraint(tmp_repo, prompt, "explicit")
    assert content == "From now on, always hash passwords with argon2id"
    assert status == "approved"


@pytest.mark.parametrize("initial_status", ["approved", "pending_approval"])
def test_ambiguous_replay_preserves_existing_status_and_revision(tmp_repo, initial_status):
    prompt = "Always hash passwords. Write the hash with argon2id."
    entry_id, _, _ = store.capture_user_constraint(tmp_repo, prompt, "before")
    data = store.load(tmp_repo)
    [entry] = data["entries"]
    entry["status"] = initial_status
    store.save(tmp_repo, data)
    _, _, status = store.capture_user_constraint(tmp_repo, prompt, "after")
    assert status is None
    [entry] = store.load(tmp_repo)["entries"]
    assert entry["id"] == entry_id
    assert entry["status"] == initial_status
    assert entry["content"] == prompt.rstrip(".")
    assert len(entry["revisions"]) == 1


def test_clean_restatement_can_resolve_an_ambiguous_work_item(tmp_repo):
    prompt = "Always hash passwords. Write the hash with argon2id."
    entry_id, _, status = store.capture_user_constraint(tmp_repo, prompt, "mixed")
    assert status == "pending_approval"
    clean = "Always hash passwords with argon2id."
    promoted_id, content, status = store.capture_user_constraint(tmp_repo, clean, "clarified")
    assert promoted_id == entry_id
    assert content == clean.rstrip(".")
    assert status == "promoted"
    [entry] = store.load(tmp_repo)["entries"]
    assert len(entry["revisions"]) == 2
    assert entry["revisions"][0]["content"] == prompt.rstrip(".")
