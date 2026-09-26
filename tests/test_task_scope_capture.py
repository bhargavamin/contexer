"""Task-scope regressions exercise independent stores on both capture surfaces."""
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
