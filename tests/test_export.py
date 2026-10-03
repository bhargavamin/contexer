import json

import pytest

from contexer import cli, export, lifecycle, revisions, store


@pytest.fixture
def decision_store(tmp_repo):
    first = store._new_decision_entry("Use Postgres for the database.", "s", "architecture", status="approved", title="Choose Postgres")
    revisions.append_revision(first, "Use SQLite for local development.\nKeep production on Postgres.", source="human", normalize=False)
    retired = store._new_decision_entry("Use Redis for the old cache.", "s", "convention", status="approved", title="Choose Redis")
    retired["deleted_at"] = "2026-01-01"
    retired["lifecycle"] = [{"replacement_id": first["id"], "action": "superseded"}]
    suggested = store._new_decision_entry("Use plain functions for tests.", "s", "convention", status="suggested", title="Write plain tests")
    pending = store._new_decision_entry("Switch to MongoDB.", "s", "architecture", status="pending_approval")
    first["source_files"] = ["app/db.py"]
    store.save(tmp_repo, {"entries": [pending, suggested, first]})
    store._save_deleted(tmp_repo, {"entries": [retired]})
    return first, retired, suggested


@pytest.mark.parametrize("format", ["md", "adr"])
def test_export_preserves_current_text_and_is_read_only(tmp_repo, decision_store, format):
    before = {p: p.read_bytes() for p in store.store_dir().rglob("*") if p.is_file()}
    documents = export.render(tmp_repo, format=format, verbatim=True)
    combined = "\n".join(documents.values())
    first, retired, suggested = decision_store
    assert revisions.current_content(first) in combined
    assert revisions.current_content(suggested) in combined
    assert "Use Postgres for the database." not in combined
    assert revisions.current_content(retired) not in combined
    assert "Switch to MongoDB" not in combined
    assert "app/db.py" in combined and "Status: suggested" in combined
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize("format", ["md", "adr"])
def test_order_and_names_do_not_depend_on_store_order(tmp_repo, decision_store, format):
    expected = export.render(tmp_repo, format=format, include_retired=True)
    data = store.load(tmp_repo)
    data["entries"].reverse()
    store.save(tmp_repo, data)
    assert export.render(tmp_repo, format=format, include_retired=True) == expected
    retired = decision_store[1]
    combined = "\n".join(expected.values())
    assert revisions.current_content(retired) in combined
    assert "Status: superseded" in combined
    assert "Replaced by:" in combined


@pytest.mark.parametrize("format", ["md", "adr"])
def test_default_export_redacts_even_when_profile_disables_redaction(tmp_repo, monkeypatch, format):
    secret = "ghp_" + "a" * 36
    entry = store._new_decision_entry("Use token=" + secret, "s", "convention", status="approved", title="Token " + secret)
    store.save(tmp_repo, {"entries": [entry]})
    monkeypatch.setattr(store, "_redaction_enabled", lambda: False)
    assert secret not in json.dumps(export.render(tmp_repo, format=format))
    assert secret in json.dumps(export.render(tmp_repo, format=format, verbatim=True))


def test_output_files_are_private(tmp_repo, tmp_path, decision_store):
    paths = export.write(tmp_repo, tmp_path / "output", format="adr")
    assert len(paths) == 2
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in paths)


def test_cli_exports_and_warns_for_verbatim(tmp_repo, tmp_path, decision_store, monkeypatch, capsys):
    monkeypatch.setattr(store, "git_root", lambda cwd: tmp_repo)
    out = tmp_path / "adrs"
    cli.export_cmd(["--format", "adr", "--out", str(out), "--verbatim"])
    assert "secrets and personal data" in capsys.readouterr().out
    assert len(list(out.glob("adr-*.md"))) == 2


def test_corrupt_source_refuses_export_without_touching_existing_output(tmp_repo, tmp_path):
    source = store._store_path(tmp_repo)
    source.write_text("{bad")
    out = tmp_path / "export"
    out.mkdir()
    existing = out / "decisions.md"
    existing.write_text("previous export")
    with pytest.raises(ValueError):
        export.write(tmp_repo, out)
    assert source.read_text() == "{bad"
    assert existing.read_text() == "previous export"


@pytest.mark.parametrize("format", ["md", "adr"])
def test_real_retirement_links_to_replacement(tmp_repo, format):
    _, old = store.update_decision(tmp_repo, "Use Redis for cache entries.", "s", "convention", created_by="human")
    _, new = store.update_decision(tmp_repo, "Use Memcached for ephemeral values.", "s", "convention", created_by="human")
    assert lifecycle.retire_decision(tmp_repo, old, "replace cache", new)[0]
    text = "\n".join(export.render(tmp_repo, format=format, include_retired=True).values())
    assert f"Replaced by: [{new}]" in text
    assert "Status: superseded" in text


@pytest.mark.parametrize("format", ["md", "adr"])
def test_default_scrubs_subtype_and_plain_password(tmp_repo, format):
    secret = "ghp_" + "a" * 36
    entry = store._new_decision_entry("Use password: monkey for testing.", "s", secret, status="approved")
    store.save(tmp_repo, {"entries": [entry]})
    result = "\n".join(export.render(tmp_repo, format=format).values())
    assert "monkey" not in result and secret not in result


def test_export_refresh_removes_only_owned_unedited_adr_files(tmp_repo, tmp_path):
    _, did = store.update_decision(tmp_repo, "Use Postgres for the database.", "s", "convention")
    out = tmp_path / "export"
    [path] = export.write(tmp_repo, out, format="adr", verbatim=True)
    other = out / "adr-personal-note.md"
    other.write_text("Keep this user document")
    lifecycle.retire_decision(tmp_repo, did, "obsolete")
    export.write(tmp_repo, out, format="adr")
    assert not path.exists() and other.read_text() == "Keep this user document"


def test_restored_decision_has_no_stale_replacement(tmp_repo):
    _, first = store.update_decision(tmp_repo, "Use Postgres for the database.", "s", "convention")
    _, second = store.update_decision(tmp_repo, "Use Redis for the cache.", "s", "convention")
    lifecycle.retire_decision(tmp_repo, first, "replaced", replacement_id=second)
    lifecycle.restore_decision(tmp_repo, first)
    result = "\n".join(export.render(tmp_repo).values())
    assert "superseded" not in result and "Replaced by" not in result


def test_ignored_decision_is_not_retired_history(tmp_repo):
    entry = store._new_decision_entry("Withheld bootstrap convention", "s", "convention", status="ignored")
    store.save(tmp_repo, {"entries": [entry]})
    assert "Withheld bootstrap" not in "\n".join(export.render(tmp_repo, include_retired=True).values())


def test_cli_outside_repo_never_uses_last_repo_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "git_root", lambda path: None)
    monkeypatch.setattr(store, "resolve_repo", lambda path: "/different/repo")
    with pytest.raises(SystemExit):
        cli.export_cmd(["--out", str(tmp_path)])


@pytest.mark.parametrize("format", ["md", "adr"])
@pytest.mark.parametrize("owned", [False, True])
def test_refresh_refuses_edited_or_unowned_current_outputs(tmp_repo, tmp_path, format, owned):
    store.update_decision(tmp_repo, "Use Postgres for the database.", "s", "convention")
    out = tmp_path / "export"
    if owned:
        [path] = export.write(tmp_repo, out, format=format)
    else:
        out.mkdir()
        path = out / next(iter(export.render(tmp_repo, format=format)))
    path.write_text("My manually edited decision", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    with pytest.raises(ValueError, match="edited|not owned"):
        export.write(tmp_repo, out, format=format)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


@pytest.mark.parametrize("format", ["md", "adr"])
def test_default_preserves_auth_policy_words_while_scrubbing_password(tmp_repo, format):
    entry = store._new_decision_entry("Use auth: required and token = short-lived; password=monkey.",
                                    "s", "constraint", status="approved")
    store.save(tmp_repo, {"entries": [entry]})
    result = "\n".join(export.render(tmp_repo, format=format).values())
    assert "auth: required" in result and "token = short-lived" in result
    assert "monkey" not in result


@pytest.mark.parametrize("format", ["md", "adr"])
def test_default_redacts_short_lowercase_tokens(tmp_repo, format):
    entry = store._new_decision_entry("Use token=admin and auth=abc; password=monkey.", "s", "constraint", status="approved")
    store.save(tmp_repo, {"entries": [entry]})
    output = "\n".join(export.render(tmp_repo, format=format).values())
    assert "admin" not in output and "abc" not in output and "monkey" not in output


def test_interrupted_export_resumes_without_overwriting_user_edits(tmp_repo, tmp_path, monkeypatch):
    entry = store._new_decision_entry("Use durable records.", "s", "constraint", status="approved")
    store.save(tmp_repo, {"entries": [entry]})
    out = tmp_path / "output"
    export.write(tmp_repo, out)
    revisions.append_revision(entry, "Use revised durable records.", source="human", normalize=False)
    store.save(tmp_repo, {"entries": [entry]})
    original = store.atomic_write
    calls = []
    def interrupted(path, content, **kwargs):
        calls.append(path.name)
        if len(calls) == 3:
            raise OSError("interrupted before final manifest")
        return original(path, content, **kwargs)
    monkeypatch.setattr(store, "atomic_write", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        export.write(tmp_repo, out)
    assert "revised durable records" in (out / "decisions.md").read_text()
    monkeypatch.setattr(store, "atomic_write", original)
    export.write(tmp_repo, out)
    assert not any(isinstance(v, list) for v in json.loads((out / ".contexer-export.json").read_text()).values())
    (out / "decisions.md").write_text("My own edit")
    with pytest.raises(ValueError, match="edited"):
        export.write(tmp_repo, out)
