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
