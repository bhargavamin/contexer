"""Tests for the Claude memory-tool → Contexer import path (memory_sync + adapter wiring)."""
import json
import re
from pathlib import Path

import pytest

from contexer import memory_sync, store
from contexer.adapters import claude

FEEDBACK = """---
name: feedback-tooling
description: Use uv (not pip3) for all Python dependency and run commands
metadata:
  node_type: memory
  type: feedback
  originSessionId: abc12345-0000
---

Use `uv` for all package management. Do not use `pip3`.
"""

MULTI = """---
name: project-spec
description: "Full spec for the project"
metadata:
  node_type: memory
  type: project
  originSessionId: def67890-1111
---

## Architecture
We chose an MCP server design instead of a CLI.

## Naming convention
Always use conventional commit format for messages.

## Out of scope
Never commit without approval.
"""

PLAIN = "just a loose note with no frontmatter at all"


def _write_memory(tmp_path: Path, **files: str) -> Path:
    mem = tmp_path / "memory"
    mem.mkdir()
    for name, text in files.items():
        (mem / name).write_text(text)
    return mem


# ── _classify ───────────────────────────────────────────────────────────────────

class TestClassify:
    def test_convention_wins_for_tooling(self):
        assert memory_sync._classify("Use uv not pip3", "feedback") == "convention"

    def test_constraint_for_prohibition(self):
        assert memory_sync._classify("Never commit secrets to the repo", "") == "constraint"

    def test_architecture_for_design_choice(self):
        assert memory_sync._classify("We chose Postgres instead of Mongo", "") == "architecture"

    def test_fm_type_backstop_when_no_keywords(self):
        assert memory_sync._classify("a quiet descriptive sentence here", "project") == "architecture"
        assert memory_sync._classify("a quiet descriptive sentence here", "feedback") == "convention"

    def test_default_is_architecture(self):
        assert memory_sync._classify("a quiet descriptive sentence here", "unknown") == "architecture"


# ── _parse_fact ─────────────────────────────────────────────────────────────────

class TestParseFact:
    def test_extracts_frontmatter_fields(self):
        f = memory_sync._parse_fact(FEEDBACK)
        assert f["name"] == "feedback-tooling"
        assert f["description"].startswith("Use uv")
        assert f["fm_type"] == "feedback"          # node_type must NOT shadow type
        assert f["origin"] == "abc12345-0000"
        assert "package management" in f["body"]

    def test_unquotes_description(self):
        f = memory_sync._parse_fact(MULTI)
        assert f["description"] == "Full spec for the project"

    def test_no_frontmatter_is_tolerated(self):
        f = memory_sync._parse_fact(PLAIN)
        assert f["name"] == "" and f["fm_type"] == ""
        assert f["body"] == PLAIN


# ── _build_entries ──────────────────────────────────────────────────────────────

class TestBuildEntries:
    def test_atomic_fact_is_one_tagged_keyed_entry(self):
        entries = memory_sync._build_entries(memory_sync._parse_fact(FEEDBACK), "feedback_tooling.md")
        assert len(entries) == 1
        content, subtype, key = entries[0]
        assert content.startswith("[memory:feedback-tooling]")
        assert subtype == "convention"
        assert key == "feedback_tooling.md"          # atomic key = source file

    def test_multi_section_splits_per_heading(self):
        entries = memory_sync._build_entries(memory_sync._parse_fact(MULTI), "project_spec.md")
        assert len(entries) == 3
        subtypes = {s for _, s, _ in entries}
        # each section classified on its own — architecture + convention + constraint
        assert "architecture" in subtypes and "convention" in subtypes and "constraint" in subtypes
        assert all(c.startswith("[memory:project-spec]") for c, _, _ in entries)
        keys = {k for _, _, k in entries}
        assert len(keys) == 3                         # distinct per-section keys
        assert all(k.startswith("project_spec.md#") for k in keys)

    def test_empty_body_yields_nothing(self):
        assert memory_sync._build_entries(memory_sync._parse_fact(""), "x.md") == []


# ── import_dir ──────────────────────────────────────────────────────────────────

class TestImportDir:
    def test_imports_and_subtypes(self, tmp_repo, tmp_path):
        mem = _write_memory(tmp_path, **{"feedback_tooling.md": FEEDBACK, "project_spec.md": MULTI})
        n = memory_sync.import_dir(mem, tmp_repo)
        assert n == 4  # 1 atomic + 3 sections
        data = json.loads(store._store_path(tmp_repo).read_text())
        assert all(e["subtype"] for e in data["entries"])           # every entry subtyped
        assert all(e["type"] == "decision" for e in data["entries"])

    def test_skips_memory_index(self, tmp_repo, tmp_path):
        mem = _write_memory(tmp_path, **{"MEMORY.md": "- [x](x.md) index", "feedback_tooling.md": FEEDBACK})
        memory_sync.import_dir(mem, tmp_repo)
        data = json.loads(store._store_path(tmp_repo).read_text())
        assert not any("index" in e["content"] for e in data["entries"])

    def test_reimport_is_deduped(self, tmp_repo, tmp_path):
        mem = _write_memory(tmp_path, **{"feedback_tooling.md": FEEDBACK})
        assert memory_sync.import_dir(mem, tmp_repo) == 1
        assert memory_sync.import_dir(mem, tmp_repo) == 0

    def test_origin_session_is_provenance(self, tmp_repo, tmp_path):
        mem = _write_memory(tmp_path, **{"feedback_tooling.md": FEEDBACK})
        memory_sync.import_dir(mem, tmp_repo)
        data = json.loads(store._store_path(tmp_repo).read_text())
        assert data["entries"][0]["session_id"] == "abc12345-0000"

    def test_bad_file_is_skipped_not_raised(self, tmp_repo, tmp_path):
        mem = _write_memory(tmp_path, **{"good.md": FEEDBACK})
        (mem / "bad.md").write_bytes(b"\xff\xfe invalid")   # undecodable
        assert memory_sync.import_dir(mem, tmp_repo) == 1   # good one still imported


def _md(desc: str, body: str, name: str = "rule") -> str:
    return (f"---\nname: {name}\ndescription: {desc}\n"
            f"metadata:\n  type: feedback\n  originSessionId: s1\n---\n\n{body}\n")


# ── update-in-place (keyed upsert) ──────────────────────────────────────────────

class TestUpdateInPlace:
    def _count(self, repo):
        return len(json.loads(store._store_path(repo).read_text())["entries"])

    def test_keyed_lifecycle_created_unchanged_updated(self, tmp_repo):
        r1 = store.upsert_memory_decision(tmp_repo, "v1 text here", "s", "convention", "k1")
        r2 = store.upsert_memory_decision(tmp_repo, "v1 text here", "s", "convention", "k1")
        r3 = store.upsert_memory_decision(tmp_repo, "completely different v2 prose", "s", "architecture", "k1")
        assert (r1, r2, r3) == ("created", "unchanged", "updated")
        assert self._count(tmp_repo) == 1                       # never duplicated
        entry = json.loads(store._store_path(tmp_repo).read_text())["entries"][0]
        assert entry["content"] == "completely different v2 prose"
        assert entry["subtype"] == "architecture"              # subtype refreshed too

    def test_reworded_fact_updates_in_place_via_import(self, tmp_repo, tmp_path):
        mem = tmp_path / "memory"; mem.mkdir()
        f = mem / "rule.md"
        f.write_text(_md("Use ruff", "Always run ruff before commit."))
        assert memory_sync.import_dir(mem, tmp_repo) == 1
        # large rewrite — previously this added a near-duplicate; now it updates in place
        f.write_text(_md("Switch to black", "We now use black not ruff for all formatting and import sorting."))
        assert memory_sync.import_dir(mem, tmp_repo) == 0       # updated, not created
        data = json.loads(store._store_path(tmp_repo).read_text())
        assert len(data["entries"]) == 1                        # no duplicate accrued
        assert "black" in data["entries"][0]["content"]

    def test_legacy_keyless_entry_is_adopted(self, tmp_repo):
        # an entry imported before memory_key existed (no key, default subtype)
        store.update_decision(tmp_repo, "[memory:x] legacy content", "s")
        r = store.upsert_memory_decision(tmp_repo, "[memory:x] legacy content", "s", "convention", "kx")
        assert r == "updated"                                   # adopted + subtype set
        assert self._count(tmp_repo) == 1                       # not duplicated
        assert json.loads(store._store_path(tmp_repo).read_text())["entries"][0]["memory_key"] == "kx"

    def test_first_import_still_dedups_against_manual_decision(self, tmp_repo):
        store.update_decision(tmp_repo, "never store plaintext passwords always bcrypt", "s")
        r = store.upsert_memory_decision(tmp_repo, "never store plaintext passwords always bcrypt",
                                         "s2", "constraint", "k9")
        assert r == "skipped"                                   # not a cross-system dup
        assert self._count(tmp_repo) == 1

    def test_dedup_skip_does_not_inflate_recurrence(self, tmp_repo):
        # Regression: a novelty-deduped memory fact must not bump the matched
        # decision's occurrence_count on every re-sync (it leaves it untouched).
        store.update_decision(tmp_repo, "never store plaintext passwords always bcrypt", "s")
        for _ in range(5):
            store.upsert_memory_decision(tmp_repo, "never store plaintext passwords always bcrypt",
                                         "s2", "constraint", "k9")
        entry = json.loads(store._store_path(tmp_repo).read_text())["entries"][0]
        assert entry["occurrence_count"] == 1                   # not inflated by re-imports


class TestDuplicateHeadings:
    def test_repeated_heading_does_not_overwrite(self, tmp_repo, tmp_path):
        # Regression: two `## Notes` sections must become two distinct entries,
        # not collapse to one via a colliding memory_key.
        doc = ("---\nname: spec\ndescription: d\nmetadata:\n  type: project\n"
               "  originSessionId: s1\n---\n\n"
               "## Notes\nfirst note about alpha.\n\n## Notes\nsecond note about beta.\n")
        mem = tmp_path / "memory"; mem.mkdir()
        (mem / "spec.md").write_text(doc)
        assert memory_sync.import_dir(mem, tmp_repo) == 2
        entries = json.loads(store._store_path(tmp_repo).read_text())["entries"]
        keys = {e["memory_key"] for e in entries}
        assert len(keys) == 2                                  # distinct keys
        blob = " ".join(e["content"] for e in entries)
        assert "alpha" in blob and "beta" in blob              # neither section lost


class TestBatchWrite:
    def test_multi_entry_import_does_one_save(self, tmp_repo, tmp_path, monkeypatch):
        # Regression: import_dir must commit in a single store write, not one per
        # entry (which put O(entries × facts) rewrites on the SessionStart path).
        mem = _write_memory(tmp_path, **{"feedback_tooling.md": FEEDBACK, "project_spec.md": MULTI})
        calls = {"n": 0}
        real_save = store._save
        monkeypatch.setattr(store, "_save", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real_save(*a, **k))[1])
        n = memory_sync.import_dir(mem, tmp_repo)
        assert n == 4
        assert calls["n"] == 1                                 # exactly one save for the whole batch


# ── dir_fingerprint ─────────────────────────────────────────────────────────────

class TestFingerprint:
    def test_stable_then_changes(self, tmp_path):
        mem = _write_memory(tmp_path, **{"a.md": FEEDBACK})
        fp1 = memory_sync.dir_fingerprint(mem)
        assert fp1 == memory_sync.dir_fingerprint(mem)
        (mem / "b.md").write_text(MULTI)
        assert memory_sync.dir_fingerprint(mem) != fp1


# ── adapter: _memory_dir + sync_memory ──────────────────────────────────────────

class TestAdapterSync:
    def test_memory_dir_derivation(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = "/Users/me/repos/proj"
        target = tmp_path / ".claude" / "projects" / "-Users-me-repos-proj" / "memory"
        assert claude._memory_dir(repo) is None      # absent
        target.mkdir(parents=True)
        assert claude._memory_dir(repo) == target     # present

    def test_sync_memory_imports_then_skips_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
        repo = str(tmp_path / "repo")
        mem = tmp_path / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", repo) / "memory"
        mem.mkdir(parents=True)
        (mem / "feedback_tooling.md").write_text(FEEDBACK)
        assert claude.sync_memory(repo) == 1          # imported
        assert claude.sync_memory(repo) == 0          # fingerprint marker → skip

    def test_sync_memory_noop_without_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
        assert claude.sync_memory(str(tmp_path / "repo")) == 0
