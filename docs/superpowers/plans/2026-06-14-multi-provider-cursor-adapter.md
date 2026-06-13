# Multi-provider adapter + Cursor integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Contexer work with Cursor (and any future AI assistant) behind a clean per-provider adapter seam, while preserving the working Claude Code flow.

**Architecture:** `store.py` keeps all storage/filter/retrieval logic and gains *neutral payload builders* (content only, no envelope). A new `contexer/adapters/` package holds one module per tool (`claude.py`, `cursor.py`) that each formats neutral payloads into that tool's hook-output JSON and owns its install/uninstall/status. `cli.py` becomes a thin dispatcher that detects installed tools (or honors `--target`) and loops over the selected adapters. The three Claude `mcp_tool` hooks become plain `command` hooks (one mechanism for both providers).

**Tech Stack:** Python 3.12+, `mcp` package, `pytest` + `pytest-cov` (coverage gate `--cov-fail-under=85`). No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-06-13-multi-provider-adapter-cursor-design.md`
**Base branch:** `feat/multi-provider-cursor-adapter` (built on `feat/pattern-promotion`).

---

## Conventions for every task

- **Run a single test without tripping the coverage gate:** append `--no-cov`, e.g.
  `uv run pytest tests/test_adapters.py::TestDetect::test_detects_claude --no-cov -v`
- **Run the full suite (enforces coverage):** `uv run pytest`
- **Commit messages** use Conventional Commits and end with the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- Each task ends green (its tests pass) and is committed before the next begins.

---

## File structure (created / modified)

| File | Responsibility | Action |
|---|---|---|
| `contexer/store.py` | storage + **neutral payload builders** + stdin helpers | Modify |
| `contexer/adapters/__init__.py` | adapter registry + `detect()` + `select(targets)` | Create |
| `contexer/adapters/base.py` | the adapter contract (docstring + shared helpers) | Create |
| `contexer/adapters/claude.py` | Claude envelope formatters, hook entrypoints, install/uninstall/status | Create (logic moved from `cli.py`) |
| `contexer/adapters/cursor.py` | Cursor envelope formatters, hook entrypoints, install/uninstall/status | Create |
| `contexer/cli.py` | thin dispatcher over selected adapters; `--target`; shared usage/version/purge | Modify |
| `tests/test_store.py` | add tests for neutral payloads + `session_from_hook_stdin` | Modify |
| `tests/test_adapters.py` | adapter registry, detect, formatters | Create |
| `tests/test_install.py` | update mcp_tool→command assertions; add `--target`/multi-tool cases | Modify |
| `tests/test_cursor_install.py` | Cursor mcp.json + hooks.json install/uninstall | Create |
| `README.md`, `CLAUDE.md` | Cursor install instructions + parity matrix | Modify |

---

# Phase 0 — Adapter package scaffolding

## Task 0.1: Create the adapters package with a registry + detection

**Files:**
- Create: `contexer/adapters/__init__.py`
- Create: `contexer/adapters/base.py`
- Test: `tests/test_adapters.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_adapters.py`:

```python
"""Tests for the multi-provider adapter registry."""
from pathlib import Path

import pytest

from contexer import adapters


class TestRegistry:
    def test_all_returns_known_adapters(self):
        names = {a.NAME for a in adapters.all_adapters()}
        assert names == {"claude", "cursor"}

    def test_get_by_name(self):
        assert adapters.get("claude").NAME == "claude"
        assert adapters.get("cursor").NAME == "cursor"

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError):
            adapters.get("emacs")


class TestDetect:
    def test_detects_claude_when_dot_claude_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude").mkdir()
        detected = {a.NAME for a in adapters.detect()}
        assert detected == {"claude"}

    def test_detects_cursor_when_dot_cursor_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".cursor").mkdir()
        detected = {a.NAME for a in adapters.detect()}
        assert detected == {"cursor"}

    def test_detects_both(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".cursor").mkdir()
        assert {a.NAME for a in adapters.detect()} == {"claude", "cursor"}

    def test_detect_empty_when_none_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert adapters.detect() == []


class TestSelect:
    def test_select_all(self):
        assert {a.NAME for a in adapters.select("all")} == {"claude", "cursor"}

    def test_select_one(self):
        assert [a.NAME for a in adapters.select("cursor")] == ["cursor"]

    def test_select_unknown_raises(self):
        with pytest.raises(KeyError):
            adapters.select("emacs")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_adapters.py --no-cov -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'contexer.adapters'`.

- [ ] **Step 3: Create `contexer/adapters/base.py`**

```python
"""Adapter contract for one AI-assistant integration target.

An adapter is a *module* (duck-typed, no class needed) that exposes:

  NAME: str                       # "claude" | "cursor"
  is_present(home: Path) -> bool  # does this tool look installed for the user?
  install(home: Path) -> list[str]    # wire MCP + hooks; return human-facing log lines
  uninstall(home: Path) -> list[str]  # remove MCP + hooks; return log lines
  status_lines(home: Path) -> list[str]  # diagnostic lines for `contexer status`

Plus hook entrypoints called from the hook command strings, each returning the
JSON string to print on stdout (never raises — hooks must not crash the host).
"""
from pathlib import Path


def home_dir() -> Path:
    return Path.home()
```

- [ ] **Step 4: Create `contexer/adapters/__init__.py`**

```python
"""Registry of integration adapters. Add a module here to support a new tool."""
from pathlib import Path

from contexer.adapters import claude, cursor

_ADAPTERS = {claude.NAME: claude, cursor.NAME: cursor}


def all_adapters() -> list:
    return list(_ADAPTERS.values())


def get(name: str):
    return _ADAPTERS[name]  # raises KeyError on unknown — caller maps to a CLI error


def detect(home: Path | None = None) -> list:
    home = home or Path.home()
    return [a for a in _ADAPTERS.values() if a.is_present(home)]


def select(target: str) -> list:
    """target is 'all', or a single adapter name."""
    if target == "all":
        return all_adapters()
    return [get(target)]  # KeyError on unknown
```

- [ ] **Step 5: Create minimal `contexer/adapters/claude.py` and `cursor.py` stubs so the package imports**

`contexer/adapters/claude.py`:

```python
"""Claude Code integration adapter."""
from pathlib import Path

NAME = "claude"


def is_present(home: Path) -> bool:
    return (home / ".claude").exists() or (home / ".claude.json").exists()
```

`contexer/adapters/cursor.py`:

```python
"""Cursor integration adapter."""
from pathlib import Path

NAME = "cursor"


def is_present(home: Path) -> bool:
    return (home / ".cursor").exists()
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_adapters.py --no-cov -v`
Expected: PASS (all of `TestRegistry`, `TestDetect`, `TestSelect`).

- [ ] **Step 7: Commit**

```bash
git add contexer/adapters tests/test_adapters.py
git commit -m "feat(adapters): scaffold provider adapter registry + detection

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# Phase 1 — Neutral payload builders in `store.py`

The three hook builders bake the Claude envelope into their return value. Extract the *content* into neutral builders so both adapters can reuse it. `get_*` keep returning the identical Claude dict (existing tests guard this).

## Task 1.1: Add `session_from_hook_stdin`

**Files:**
- Modify: `contexer/store.py` (next to `source_from_hook_stdin`, ~line 830)
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store.py`:

```python
class TestSessionFromHookStdin:
    def test_extracts_session_id(self):
        from contexer import store
        assert store.session_from_hook_stdin('{"session_id": "abc-123"}') == "abc-123"

    def test_missing_session_id_returns_empty(self):
        from contexer import store
        assert store.session_from_hook_stdin('{"prompt": "hi"}') == ""

    def test_malformed_stdin_returns_empty(self):
        from contexer import store
        assert store.session_from_hook_stdin("not json") == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_store.py::TestSessionFromHookStdin --no-cov -v`
Expected: FAIL — `AttributeError: module 'contexer.store' has no attribute 'session_from_hook_stdin'`.

- [ ] **Step 3: Implement**

In `contexer/store.py`, immediately after `source_from_hook_stdin` (ends ~line 837), add:

```python
def session_from_hook_stdin(raw: str) -> str:
    """Extracts the host's session id from a hook's stdin JSON (both Claude Code
    and Cursor provide `session_id`). Used by command-type capture hooks so stored
    entries are grouped by session. Safe on any input."""
    try:
        data = json.loads(raw)
        return data.get("session_id", "") if isinstance(data, dict) else ""
    except Exception:
        return ""
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_store.py::TestSessionFromHookStdin --no-cov -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add contexer/store.py tests/test_store.py
git commit -m "feat(store): add session_from_hook_stdin for command-hook capture

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task 1.2: Extract `session_start_payload` and route `get_session_start_context` through it

**Files:**
- Modify: `contexer/store.py:694-799` (`get_session_start_context`)
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store.py`:

```python
class TestSessionStartPayload:
    def test_empty_repo_payload_has_bootstrap_context(self, tmp_repo):
        from contexer import store
        p = store.session_start_payload(tmp_repo)
        assert "bootstrap" in p["context"].lower()
        assert "no context stored" in p["status"].lower()

    def test_populated_repo_payload_pointer(self, populated_repo):
        from contexer import store
        p = store.session_start_payload(populated_repo)
        assert "get_context" in p["context"]
        assert "on demand" in p["status"]

    def test_resume_with_decisions_has_status_no_context(self, populated_repo):
        from contexer import store
        p = store.session_start_payload(populated_repo, source="resume")
        assert p["context"] == ""
        assert "resumed" in p["status"].lower()

    def test_get_session_start_context_envelope_unchanged(self, tmp_repo):
        # Back-compat: the Claude dict shape is preserved exactly.
        from contexer import store
        result = store.get_session_start_context(tmp_repo)
        assert "no context stored" in result["systemMessage"].lower()
        assert "bootstrap" in result["hookSpecificOutput"]["additionalContext"].lower()
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_store.py::TestSessionStartPayload --no-cov -v`
Expected: FAIL — `AttributeError: ... 'session_start_payload'`.

- [ ] **Step 3: Refactor `get_session_start_context` into payload + envelope**

Replace the whole function body of `get_session_start_context` (currently `contexer/store.py:694-799`) with the two functions below. The content-building logic is moved verbatim into `session_start_payload`; only the *return shape* changes (return `{"status","context"}` instead of the Claude dict). `get_session_start_context` rebuilds the identical Claude dict from the payload.

```python
def session_start_payload(repo_path: str, source: str = "") -> dict:
    """Provider-neutral session-start content. Returns {"status": str, "context": str}:
    `status` is the short human-facing line, `context` is the text to inject into the
    conversation. Empty `context` means "inject nothing". All filtering/promotion logic
    is unchanged from the original get_session_start_context."""
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    global_rules = get_global_decisions()
    resume_flag = STORE_DIR / ".resume_mining"

    if source == "resume":
        if decisions:
            return {
                "status": f"Contexer: session resumed — {_pl(len(decisions), 'decision')} already loaded in conversation",
                "context": "",
            }
        STORE_DIR.mkdir(exist_ok=True)
        resume_flag.write_text(repo_path)
        sys_parts = []
        if global_rules:
            sys_parts.append("## Global rules (apply to ALL repos):")
            sys_parts.extend(f"- [{d.get('subtype', '')}] {d['content']}" for d in global_rules)
            sys_parts.append("")
        sys_parts.extend(_build_resume_mining_context(repo_path))
        return {
            "status": "Contexer: resumed with no stored context — mining this conversation for decisions",
            "context": "\n".join(sys_parts),
        }

    resume_flag.unlink(missing_ok=True)

    if not decisions:
        lines = _build_bootstrap_context(repo_path)
        sys_parts = []
        if global_rules:
            sys_parts.append("## Global rules (apply to ALL repos):")
            for d in global_rules:
                sys_parts.append(f"- [{d.get('subtype', '')}] {d['content']}")
            sys_parts.append("")
        sys_parts.extend(lines)
        global_note = f" ({_pl(len(global_rules), 'global rule')} active)" if global_rules else ""
        return {
            "status": f"Contexer: no context stored{global_note} — setup offer on next prompt",
            "context": "\n".join(sys_parts),
        }

    count = len(decisions)
    pre_loaded = [d for d in decisions if d.get("subtype") in ("convention", "constraint", "pattern")]
    deferred_count = count - len(pre_loaded)

    sys_parts = []
    if global_rules:
        sys_parts.append("## Global rules (apply to ALL repos):")
        for d in global_rules:
            sys_parts.append(f"- [{d.get('subtype', '')}] {d['content']}")
    if pre_loaded:
        sys_parts.append("## Project rules — apply to ALL tasks in this repo:")
        for d in pre_loaded:
            sys_parts.append(f"- [{d.get('subtype', '')}]{_recur_suffix(d)} {d['content']}")
    if deferred_count > 0:
        arch_count = sum(1 for d in decisions if d.get("subtype") == "architecture")
        breakdown = f" ({arch_count} architecture)" if arch_count else ""
        sys_parts.append(
            f"{deferred_count} decision(s) stored{breakdown}. "
            "Call get_context BEFORE reading files for any question about architecture, "
            "design decisions, rationale, or patterns."
        )

    constraints = [d for d in pre_loaded if d.get("subtype") == "constraint"]
    conventions = [d for d in pre_loaded if d.get("subtype") == "convention"]
    patterns = [d for d in pre_loaded if d.get("subtype") == "pattern"]

    loaded_parts = []
    if global_rules:
        loaded_parts.append(_pl(len(global_rules), "global rule"))
    if constraints:
        loaded_parts.append(_pl(len(constraints), "constraint"))
    if conventions:
        loaded_parts.append(_pl(len(conventions), "convention"))
    if patterns:
        loaded_parts.append(_pl(len(patterns), "pattern"))

    sentences = []
    if loaded_parts:
        sentences.append(f"{', '.join(loaded_parts)} loaded")
    if deferred_count > 0:
        sentences.append(f"{_pl(deferred_count, 'architecture decision')} will be loaded on demand")

    status = f"Contexer: {'. '.join(sentences)}." if sentences else "Contexer: active."
    return {"status": status, "context": "\n".join(sys_parts)}


def get_session_start_context(repo_path: str, source: str = "") -> dict:
    """Claude Code SessionStart hook output. Thin envelope over session_start_payload —
    kept for back-compat with installed hooks and the existing test suite."""
    from contexer.adapters import claude
    return claude.format_session_start(session_start_payload(repo_path, source))
```

> Note: the `claude.format_session_start` used here is added in Task 2.1. Until then this import fails — that's expected; Phase 1 and Phase 2.1 are committed close together. If you run the full suite between Task 1.2 and 2.1, `get_session_start_context` tests will error. Run only `tests/test_store.py::TestSessionStartPayload --no-cov` at this step.

- [ ] **Step 4: Run the payload test (not the envelope test yet)**

Run: `uv run pytest tests/test_store.py::TestSessionStartPayload::test_empty_repo_payload_has_bootstrap_context tests/test_store.py::TestSessionStartPayload::test_populated_repo_payload_pointer tests/test_store.py::TestSessionStartPayload::test_resume_with_decisions_has_status_no_context --no-cov -v`
Expected: PASS (these three don't touch the envelope).

- [ ] **Step 5: Commit**

```bash
git add contexer/store.py tests/test_store.py
git commit -m "refactor(store): extract session_start_payload (neutral content)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task 1.3: Extract `bootstrap_prompt_payload` and `post_compact_payload`

**Files:**
- Modify: `contexer/store.py` (`get_bootstrap_context_prompt:840-887`, `get_post_compact_context:890-899`)
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store.py`:

```python
class TestBootstrapPromptPayload:
    def test_decisions_present_payload_empty(self, populated_repo):
        from contexer import store
        p = store.bootstrap_prompt_payload(populated_repo, "anything")
        assert p == {"status": "", "context": ""}

    def test_empty_repo_payload_has_context(self, tmp_repo):
        from contexer import store
        p = store.bootstrap_prompt_payload(tmp_repo, "add a feature")
        assert p["status"] == ""
        assert p["context"] != ""


class TestPostCompactPayload:
    def test_empty_repo_reoffers_bootstrap(self, tmp_repo):
        from contexer import store
        p = store.post_compact_payload(tmp_repo)
        assert "bootstrap" in p["context"].lower()

    def test_populated_repo_reloads_context(self, populated_repo):
        from contexer import store
        p = store.post_compact_payload(populated_repo)
        assert "reloaded after compaction" in p["status"].lower()
        assert p["context"] != ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_store.py::TestBootstrapPromptPayload tests/test_store.py::TestPostCompactPayload --no-cov -v`
Expected: FAIL — missing attributes.

- [ ] **Step 3: Refactor both functions**

Replace `get_bootstrap_context_prompt` (`contexer/store.py:840-887`) with:

```python
def bootstrap_prompt_payload(repo_path: str, prompt: str = "") -> dict:
    """Neutral UserPromptSubmit bootstrap-fallback content. {"status": "", "context": str}.
    Empty context => emit nothing. Logic unchanged from get_bootstrap_context_prompt."""
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    if decisions:
        return {"status": "", "context": ""}
    resume_flag = STORE_DIR / ".resume_mining"
    if resume_flag.exists():
        try:
            flagged = resume_flag.read_text().strip()
        except Exception:
            flagged = ""
        if flagged == repo_path:
            resume_flag.unlink(missing_ok=True)
            return {"status": "", "context": ""}
    level, decisive = _detect_insight(repo_path)
    repo_name = Path(repo_path).name if repo_path else ""
    label = f'"{repo_name}"' if repo_name else "this repo"
    if _is_newcomer_question(prompt) and not (decisive and level == "high"):
        lines = [
            "Contexer OVERRIDE — ignore any earlier bootstrap menu instructions for this turn.",
            "The user's first message asks about or wants to summarize this repo. That is"
            " low-insight evidence (matched deterministically). Your ENTIRE response must be ONLY:",
            f"  \"Contexer: you're asking about {label}, so I'll assume you're new here —"
            " I'll scan the code and docs, store what I find for future sessions, then answer"
            " your question. OK? (or: quick / full / skip if you actually know this repo)\"",
            "Then stop and wait. If they confirm (ok / yes / scan) → call bootstrap_context"
            " with insight='low', store each inferred fact directly via update_context"
            " (subtype='architecture'), read the README and docs for the repo's purpose and"
            " store it, ask the single returned goal question and store the answer, then"
            " answer their original question.",
            "If they reply quick / full / skip instead → follow the session-start bootstrap"
            " instructions for that option.",
        ]
    else:
        lines = _build_bootstrap_context(repo_path)
    return {"status": "", "context": "\n".join(lines)}


def get_bootstrap_context_prompt(repo_path: str, prompt: str = "") -> dict:
    """Claude UserPromptSubmit bootstrap-fallback output. Back-compat envelope."""
    from contexer.adapters import claude
    return claude.format_bootstrap_prompt(bootstrap_prompt_payload(repo_path, prompt))
```

Replace `get_post_compact_context` (`contexer/store.py:890-899`) with:

```python
def post_compact_payload(repo_path: str) -> dict:
    """Neutral PostCompact content. {"status": str, "context": str}."""
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    if not decisions:
        return {"status": "", "context": "\n".join(_build_bootstrap_context(repo_path))}
    return {"status": "Contexer: context reloaded after compaction", "context": get_context(repo_path)}


def get_post_compact_context(repo_path: str) -> dict:
    """Claude PostCompact output. Back-compat envelope."""
    from contexer.adapters import claude
    return claude.format_post_compact(post_compact_payload(repo_path))
```

- [ ] **Step 4: Run the payload tests**

Run: `uv run pytest tests/test_store.py::TestBootstrapPromptPayload tests/test_store.py::TestPostCompactPayload --no-cov -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add contexer/store.py tests/test_store.py
git commit -m "refactor(store): extract bootstrap_prompt_payload + post_compact_payload

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# Phase 2 — Claude adapter (formatters + entrypoints + install/uninstall/status)

## Task 2.1: Claude envelope formatters

**Files:**
- Modify: `contexer/adapters/claude.py`
- Test: `tests/test_adapters.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_adapters.py`:

```python
from contexer.adapters import claude


class TestClaudeFormatters:
    def test_session_start_with_context(self):
        d = claude.format_session_start({"status": "S", "context": "C"})
        assert d["systemMessage"] == "S"
        assert d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert d["hookSpecificOutput"]["additionalContext"] == "C"

    def test_session_start_empty_context_omits_injection(self):
        d = claude.format_session_start({"status": "only status", "context": ""})
        assert d == {"systemMessage": "only status"}

    def test_bootstrap_prompt_empty_is_empty_dict(self):
        assert claude.format_bootstrap_prompt({"status": "", "context": ""}) == {}

    def test_bootstrap_prompt_with_context(self):
        d = claude.format_bootstrap_prompt({"status": "", "context": "menu"})
        assert d["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert d["hookSpecificOutput"]["additionalContext"] == "menu"

    def test_post_compact_combines_status_and_context(self):
        d = claude.format_post_compact({"status": "reloaded", "context": "ctx"})
        assert d == {"systemMessage": "reloaded\nctx"}

    def test_post_compact_context_only(self):
        d = claude.format_post_compact({"status": "", "context": "bootstrap lines"})
        assert d == {"systemMessage": "bootstrap lines"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_adapters.py::TestClaudeFormatters --no-cov -v`
Expected: FAIL — `AttributeError: ... 'format_session_start'`.

- [ ] **Step 3: Implement formatters in `contexer/adapters/claude.py`**

Add below the existing `is_present`:

```python
def format_session_start(payload: dict) -> dict:
    """Neutral payload -> Claude SessionStart hook output. Empty context => status only."""
    if not payload.get("context"):
        return {"systemMessage": payload["status"]} if payload.get("status") else {}
    return {
        "systemMessage": payload["status"],
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": payload["context"],
        },
    }


def format_bootstrap_prompt(payload: dict) -> dict:
    """Neutral payload -> Claude UserPromptSubmit output. Empty context => no-op {}."""
    if not payload.get("context"):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": payload["context"],
        }
    }


def format_post_compact(payload: dict) -> dict:
    """Neutral payload -> Claude PostCompact output (injected via systemMessage)."""
    parts = [p for p in (payload.get("status"), payload.get("context")) if p]
    return {"systemMessage": "\n".join(parts)} if parts else {}
```

- [ ] **Step 4: Run formatter tests + the back-compat envelope test from Task 1.2**

Run: `uv run pytest tests/test_adapters.py::TestClaudeFormatters tests/test_store.py::TestSessionStartPayload::test_get_session_start_context_envelope_unchanged --no-cov -v`
Expected: PASS (both formatters and the preserved Claude dict shape).

- [ ] **Step 5: Run the full pre-existing store hook-builder tests (regression gate)**

Run: `uv run pytest tests/test_store.py --no-cov -v`
Expected: PASS — all original `get_session_start_context` / bootstrap / post-compact tests still pass because the envelope is reproduced exactly.

- [ ] **Step 6: Commit**

```bash
git add contexer/adapters/claude.py tests/test_adapters.py
git commit -m "feat(adapters): claude envelope formatters; route store.get_* through them

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task 2.2: Claude command-hook entrypoints (unify capture path)

The three `mcp_tool` hooks become `command` hooks calling these entrypoints. Each takes `(repo_path, raw_stdin)` and returns the JSON string to print. They never raise.

**Files:**
- Modify: `contexer/adapters/claude.py`
- Test: `tests/test_adapters.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_adapters.py`:

```python
import json as _json


class TestClaudeCaptureEntrypoints:
    def test_capture_task_stores_and_prints_empty(self, tmp_repo, monkeypatch):
        from contexer import store
        raw = _json.dumps({"prompt": "Refactor the auth module to use JWT", "session_id": "s1"})
        out = claude.capture_task(tmp_repo, raw)
        assert out == "{}"
        data = _json.loads((store.STORE_DIR / store._slug(tmp_repo)).with_suffix(".json").read_text()) \
            if False else None  # see helper below
        # task is stored:
        assert "Last task" in store.get_context(tmp_repo)

    def test_capture_task_ignores_question(self, tmp_repo):
        raw = _json.dumps({"prompt": "what is this repo?", "session_id": "s1"})
        assert claude.capture_task(tmp_repo, raw) == "{}"

    def test_capture_constraint_stores_and_acks(self, tmp_repo):
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1"})
        out = _json.loads(claude.capture_constraint(tmp_repo, raw))
        assert "additionalContext" in out["hookSpecificOutput"]
        assert "constraint" in out["hookSpecificOutput"]["additionalContext"].lower()

    def test_capture_constraint_noop_on_plain_prompt(self, tmp_repo):
        raw = _json.dumps({"prompt": "please add a button", "session_id": "s1"})
        assert claude.capture_constraint(tmp_repo, raw) == "{}"

    def test_rationale_injects_when_decisions_match(self, populated_repo):
        raw = _json.dumps({"prompt": "why did we choose this architecture?"})
        out = _json.loads(claude.rationale(populated_repo, raw))
        assert "additionalContext" in out["hookSpecificOutput"]

    def test_rationale_noop_on_plain_prompt(self, populated_repo):
        raw = _json.dumps({"prompt": "add a test"})
        assert claude.rationale(populated_repo, raw) == "{}"

    def test_entrypoints_never_raise_on_bad_stdin(self, tmp_repo):
        assert claude.capture_task(tmp_repo, "garbage") == "{}"
        assert claude.capture_constraint(tmp_repo, "garbage") == "{}"
        assert claude.rationale(tmp_repo, "garbage") == "{}"
```

> Remove the dead `data = ... if False else None` line if your linter objects; it documents that we assert via `get_context`, not by reading the raw file. Use the simpler body:
> ```python
>     def test_capture_task_stores_and_prints_empty(self, tmp_repo):
>         from contexer import store
>         raw = _json.dumps({"prompt": "Refactor the auth module to use JWT", "session_id": "s1"})
>         assert claude.capture_task(tmp_repo, raw) == "{}"
>         assert "Last task" in store.get_context(tmp_repo)
> ```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_adapters.py::TestClaudeCaptureEntrypoints --no-cov -v`
Expected: FAIL — missing `capture_task`/`capture_constraint`/`rationale`.

- [ ] **Step 3: Implement entrypoints in `contexer/adapters/claude.py`**

Add at the top of the module: `import json` and `from contexer import store`. Then:

```python
def capture_task(repo_path: str, raw: str) -> str:
    """UserPromptSubmit (once): store the first prompt as the task. Silent."""
    try:
        repo = store._resolve_repo(repo_path)
        if repo:
            store.capture_task(repo, store.prompt_from_hook_stdin(raw),
                               store.session_from_hook_stdin(raw))
    except Exception:
        pass
    return "{}"


def capture_constraint(repo_path: str, raw: str) -> str:
    """UserPromptSubmit (every prompt): auto-store 'always/never/from now on' directives."""
    try:
        repo = store._resolve_repo(repo_path)
        if not repo:
            return "{}"
        entry_id, content = store.capture_user_constraint(
            repo, store.prompt_from_hook_stdin(raw), store.session_from_hook_stdin(raw))
        if entry_id is None:
            return "{}"
        msg = (f"Auto-stored as constraint: '{content}'. "
               "Acknowledge this briefly to the user — e.g. 'Stored as a constraint in Contexer.'")
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": msg}})
    except Exception:
        return "{}"


def rationale(repo_path: str, raw: str) -> str:
    """UserPromptSubmit (every prompt): inject matching decisions for rationale questions."""
    try:
        repo = store._resolve_repo(repo_path)
        if not repo:
            return "{}"
        ctx = store.get_context_for_prompt(repo, store.prompt_from_hook_stdin(raw))
        if not ctx:
            return "{}"
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": ctx}})
    except Exception:
        return "{}"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_adapters.py::TestClaudeCaptureEntrypoints --no-cov -v`
Expected: PASS.

> If `tmp_repo`/`populated_repo` fixtures are defined only in `tests/test_store.py`, move them to a new `tests/conftest.py` so `tests/test_adapters.py` can use them. Do that as Step 4a:
> - Cut the `@pytest.fixture` definitions for `tmp_repo` and `populated_repo` from `tests/test_store.py` into a new `tests/conftest.py` (with the needed imports). pytest auto-discovers `conftest.py`; no import line needed in test files.
> - Re-run `uv run pytest tests/test_store.py tests/test_adapters.py --no-cov -v` and confirm PASS.

- [ ] **Step 5: Commit**

```bash
git add contexer/adapters/claude.py tests/test_adapters.py tests/conftest.py tests/test_store.py
git commit -m "feat(adapters): claude command-hook capture entrypoints (unify capture path)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task 2.3: Move Claude install/uninstall/status into the adapter; convert mcp_tool hooks to command hooks

**Files:**
- Create logic in: `contexer/adapters/claude.py`
- Modify: `contexer/cli.py` (delegate to adapter)
- Modify: `tests/test_install.py`

- [ ] **Step 1: Update the mcp_tool tests to expect command hooks**

In `tests/test_install.py`, replace `test_capture_context_mcp_tool_registered` (lines 71-80) and the mcp_tool assertions in `test_install_is_idempotent` (lines 96-103) with:

```python
    def test_capture_context_command_hook_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ups = settings["hooks"]["UserPromptSubmit"]
        cmds = [h["command"] for grp in ups for h in grp["hooks"] if "command" in h]
        # capture is now a command hook calling the adapter entrypoint
        assert any("claude.capture_task" in c for c in cmds)
        # and it must not be an mcp_tool anymore
        assert not any(h.get("type") == "mcp_tool" for grp in ups for h in grp["hooks"])

    def test_constraint_and_rationale_command_hooks_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ups = settings["hooks"]["UserPromptSubmit"]
        cmds = [h["command"] for grp in ups for h in grp["hooks"] if "command" in h]
        assert any("claude.capture_constraint" in c for c in cmds)
        assert any("claude.rationale" in c for c in cmds)
```

And in `test_install_is_idempotent` replace the mcp_tool count block with:

```python
        ups = settings["hooks"]["UserPromptSubmit"]
        cmds = [h.get("command", "") for grp in ups for h in grp["hooks"]]
        assert sum("claude.capture_task" in c for c in cmds) == 1, \
            "capture_task hook must not be duplicated"
```

(Permissions still apply — leave the `allow.count(...)` assertion in `test_install_is_idempotent`, but note `mcp__contexer__capture_context` permission is no longer added; change it to `mcp__contexer__update_context`):

```python
        allow = settings["permissions"]["allow"]
        assert allow.count("mcp__contexer__update_context") == 1
```

Also update `test_permissions_added` (lines 82-87): drop `mcp__contexer__capture_context` from the asserted list (it's no longer an invoked tool via hook, though it remains a registered MCP tool — keep it in the allow-list since the adapter still grants it; **decision:** keep granting all `mcp__contexer__*` permissions, so this test is unchanged). Leave `test_permissions_added` as-is.

- [ ] **Step 2: Run to verify the updated tests fail**

Run: `uv run pytest tests/test_install.py::TestInstall::test_capture_context_command_hook_registered tests/test_install.py::TestInstall::test_constraint_and_rationale_command_hooks_registered --no-cov -v`
Expected: FAIL — current install still writes mcp_tool hooks.

- [ ] **Step 3: Implement `claude.install(home)` / `uninstall(home)` / `status_lines(home)`**

Move the body of the current `contexer/cli.py` `install()` (lines 134-288) into `contexer/adapters/claude.py` as `def install(home: Path) -> list[str]:` with these changes:

1. Accept `home: Path` as a parameter (delete the `home = Path.home()` line).
2. Collect log strings into a `log: list[str] = []` and `return log` instead of `print(...)` (the CLI prints them).
3. Keep the SessionStart / PreCompact / PostCompact / anchor / bootstrap command hooks **exactly as they are** (they already call `store.get_session_start_context` etc., which now delegate to the adapter — output unchanged).
4. **Replace the three `mcp_tool` blocks** (current `cli.py:243-259`) with command-hook blocks:

```python
    cap_task = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
                f'"{python}" -c "from contexer.adapters import claude; import sys; '
                'print(claude.capture_task(sys.argv[1], sys.stdin.read()))" "$REPO"')
    cap_con = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
               f'"{python}" -c "from contexer.adapters import claude; import sys; '
               'print(claude.capture_constraint(sys.argv[1], sys.stdin.read()))" "$REPO"')
    cap_rat = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
               f'"{python}" -c "from contexer.adapters import claude; import sys; '
               'print(claude.rationale(sys.argv[1], sys.stdin.read()))" "$REPO"')

    if not _in_groups(ups, "claude.capture_task"):
        ups.append({"hooks": [{"type": "command", "once": True,
            "statusMessage": "Capturing task...", "command": cap_task}]})
    if not _in_groups(ups, "claude.capture_constraint"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for constraint directives...", "command": cap_con}]})
    if not _in_groups(ups, "claude.rationale"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for relevant decisions...", "command": cap_rat}]})
```

5. **Add a migration** right after the anchor-hook migration block (current `cli.py:224-231`) to strip any old `mcp_tool` contexer hooks so reinstall converts them:

```python
    # Migrate: old capture hooks used the mcp_tool type; replace with command hooks
    if any(h.get("type") == "mcp_tool" and h.get("server") == "contexer"
           for grp in ups for h in grp.get("hooks", [])):
        ups = [grp for grp in ups if not any(
            h.get("type") == "mcp_tool" and h.get("server") == "contexer"
            for h in grp.get("hooks", []))]
        hooks["UserPromptSubmit"] = ups
```

6. The `_py`, `_in_groups`, `_has_mcp_tool`, `_filter_groups`, `_load`, `_save`, `_bootstrap_command_text`, `_BOOTSTRAP_CMD_MARKER` helpers are used by this code. **Move them into `contexer/adapters/claude.py`** (or a shared `contexer/adapters/base.py`) and import where needed. To avoid a large move, put the shared JSON/marker helpers in `base.py` and import them in `claude.py`:
   - Move to `base.py`: `_load`, `_load_safe`, `_is_corrupt`, `_save`, `_hooks_of`, `_in_groups`, `_filter_groups`, `_has_mcp_tool`, `_BOOTSTRAP_CMD_MARKER`, `_bootstrap_command_text`.
   - `claude.py` and `cli.py` import them from `contexer.adapters.base`.

Then move `uninstall()` (current `cli.py:291-352`, the Claude-config portions only — **not** the `--purge` store deletion, which stays in `cli.py`) into `claude.uninstall(home) -> list[str]`, returning log lines. Move the Claude-config portion of `status()` (current `cli.py:382-453`) into `claude.status_lines(home) -> list[str]` (version/update-check stays in `cli.py`; the adapter returns the MCP/hooks/bootstrap lines).

- [ ] **Step 4: Make `cli.py` delegate (single Claude target for now)**

In `contexer/cli.py`, replace the bodies of `install`, `uninstall`, `status` so they call the Claude adapter (Phase 4 generalizes target selection):

```python
from contexer.adapters import claude

def install() -> None:
    home = Path.home()
    (home / ".contexer").mkdir(exist_ok=True)
    for line in claude.install(home):
        print(line)
    print("\nDone. Restart your AI assistant and open any git repo to activate Contexer.")
```

(Mirror for `uninstall(purge)` — call `claude.uninstall(home)`, then keep the existing `--purge` store-removal block; and `status()` — keep the version/update-check header, then print `claude.status_lines(home)`.)

- [ ] **Step 5: Run install + adapter tests**

Run: `uv run pytest tests/test_install.py tests/test_adapters.py --no-cov -v`
Expected: PASS — including the new command-hook assertions and idempotency.

- [ ] **Step 6: Run the FULL suite (coverage gate + regression)**

Run: `uv run pytest`
Expected: PASS, coverage ≥ 85%. This is the regression gate the spec requires for the capture-path change.

- [ ] **Step 7: Manual smoke — the SessionStart hook still emits valid JSON**

Run:
```bash
echo '{"source":"startup"}' | uv run python -c "from contexer.adapters import claude; import sys; print(claude.capture_task('$PWD', sys.stdin.read()))"
```
Expected: prints `{}` (no crash).

- [ ] **Step 8: Commit**

```bash
git add contexer/adapters tests/test_install.py contexer/cli.py
git commit -m "refactor(cli): move claude install/uninstall/status into adapter; command-hook capture

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# Phase 3 — Cursor adapter

## Task 3.1: Cursor envelope formatters + behavioral nudge

**Files:**
- Modify: `contexer/adapters/cursor.py`
- Test: `tests/test_adapters.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_adapters.py`:

```python
from contexer.adapters import cursor


class TestCursorFormatters:
    def test_session_start_injects_additional_context_with_nudge(self):
        d = cursor.format_session_start({"status": "ignored on cursor", "context": "RULES"})
        assert d["additional_context"].startswith("RULES")
        assert "get_context" in d["additional_context"]   # behavioral nudge appended
        assert "update_context" in d["additional_context"]
        assert "systemMessage" not in d  # cursor has no systemMessage channel

    def test_session_start_empty_context_still_emits_nudge_only(self):
        # Even with no stored rules, the nudge alone is worth injecting once.
        d = cursor.format_session_start({"status": "", "context": ""})
        assert "get_context" in d["additional_context"]

    def test_prompt_passthrough_continue_true(self):
        assert cursor.format_prompt_passthrough() == {"continue": True}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_adapters.py::TestCursorFormatters --no-cov -v`
Expected: FAIL — missing `format_session_start` / `format_prompt_passthrough`.

- [ ] **Step 3: Implement in `contexer/adapters/cursor.py`**

Add below `is_present`:

```python
# Claude delivers these reminders via per-prompt hooks; Cursor can only inject at
# session start, so the nudge rides in additional_context (see the spec's parity matrix).
_NUDGE = (
    "Contexer is active. Call get_context BEFORE reading files for any question about "
    "architecture, design decisions, rationale, patterns, or constraints. Call "
    "update_context after any significant decision, established pattern, or stated "
    "constraint (the server deduplicates, so err on the side of calling it)."
)


def format_session_start(payload: dict) -> dict:
    """Neutral payload -> Cursor sessionStart output. Cursor has no systemMessage
    channel and cannot inject per-prompt, so the behavioral nudge is appended here."""
    ctx = payload.get("context") or ""
    combined = f"{ctx}\n\n{_NUDGE}" if ctx else _NUDGE
    return {"additional_context": combined}


def format_prompt_passthrough() -> dict:
    """beforeSubmitPrompt output: allow the prompt. Cursor cannot inject context here,
    so capture hooks are write-only side effects that return this pass-through."""
    return {"continue": True}
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_adapters.py::TestCursorFormatters --no-cov -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add contexer/adapters/cursor.py tests/test_adapters.py
git commit -m "feat(adapters): cursor envelope formatters + session-start nudge

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task 3.2: Cursor hook entrypoints

**Files:**
- Modify: `contexer/adapters/cursor.py`
- Test: `tests/test_adapters.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_adapters.py`:

```python
class TestCursorEntrypoints:
    def test_session_start_writes_current_repo_from_workspace_roots(self, tmp_repo, monkeypatch):
        from contexer import store
        raw = _json.dumps({"workspace_roots": [tmp_repo], "session_id": "s1"})
        out = _json.loads(cursor.session_start("", raw))
        assert "additional_context" in out
        assert (store.STORE_DIR / ".current_repo").read_text() == tmp_repo

    def test_capture_task_writes_and_passes_through(self, tmp_repo):
        from contexer import store
        raw = _json.dumps({"prompt": "Refactor auth to use JWT tokens", "session_id": "s1",
                           "workspace_roots": [tmp_repo]})
        assert _json.loads(cursor.capture_task(tmp_repo, raw)) == {"continue": True}
        assert "Last task" in store.get_context(tmp_repo)

    def test_capture_constraint_writes_and_passes_through(self, tmp_repo):
        from contexer import store
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1"})
        assert _json.loads(cursor.capture_constraint(tmp_repo, raw)) == {"continue": True}
        assert "conventional commits" in store.get_context(tmp_repo, entry_type="convention").lower() \
            or "conventional commits" in store.get_context(tmp_repo, entry_type="constraint").lower()

    def test_entrypoints_never_raise(self, tmp_repo):
        assert _json.loads(cursor.capture_task(tmp_repo, "garbage")) == {"continue": True}
        assert _json.loads(cursor.session_start("", "garbage"))  # returns dict, no raise
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_adapters.py::TestCursorEntrypoints --no-cov -v`
Expected: FAIL — missing entrypoints.

- [ ] **Step 3: Implement in `contexer/adapters/cursor.py`**

Add `import json` and `from contexer import store` at the top, then:

```python
def _repo_from(raw: str, repo_path: str) -> str:
    """Cursor sessionStart provides workspace_roots[]; fall back to .current_repo."""
    if repo_path:
        return store._resolve_repo(repo_path)
    try:
        roots = json.loads(raw).get("workspace_roots") or []
        if roots:
            return store._resolve_repo(roots[0])
    except Exception:
        pass
    return store._resolve_repo("")


def session_start(repo_path: str, raw: str) -> str:
    """Cursor sessionStart: write .current_repo, inject rules + nudge. Never raises."""
    try:
        repo = _repo_from(raw, repo_path)
        if repo:
            store.STORE_DIR.mkdir(exist_ok=True)
            (store.STORE_DIR / ".current_repo").write_text(repo)
            payload = store.session_start_payload(repo)
        else:
            payload = {"status": "", "context": ""}
        return json.dumps(format_session_start(payload))
    except Exception:
        return json.dumps({"additional_context": _NUDGE})


def capture_task(repo_path: str, raw: str) -> str:
    """beforeSubmitPrompt: store the prompt as the task (write-only)."""
    try:
        repo = _repo_from(raw, repo_path)
        if repo:
            store.capture_task(repo, store.prompt_from_hook_stdin(raw),
                               store.session_from_hook_stdin(raw))
    except Exception:
        pass
    return json.dumps(format_prompt_passthrough())


def capture_constraint(repo_path: str, raw: str) -> str:
    """beforeSubmitPrompt: auto-store 'always/never' directives (write-only; no ack)."""
    try:
        repo = _repo_from(raw, repo_path)
        if repo:
            store.capture_user_constraint(repo, store.prompt_from_hook_stdin(raw),
                                          store.session_from_hook_stdin(raw))
    except Exception:
        pass
    return json.dumps(format_prompt_passthrough())
```

> **Known v1 difference (documented):** Cursor's `beforeSubmitPrompt` has no "once" semantics, so `capture_task` runs every prompt and `store.capture_task` replaces the task entry each time — Cursor tracks the *latest* prompt-as-task, not the first. Acceptable for v1; noted in the spec parity section.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_adapters.py::TestCursorEntrypoints --no-cov -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add contexer/adapters/cursor.py tests/test_adapters.py
git commit -m "feat(adapters): cursor hook entrypoints (sessionStart + capture)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task 3.3: Cursor install / uninstall / status

Writes `~/.cursor/mcp.json` (MCP server) and `~/.cursor/hooks.json` (sessionStart + two beforeSubmitPrompt command hooks). Reuses `base._load`/`base._save`.

**Files:**
- Modify: `contexer/adapters/cursor.py`
- Test: `tests/test_cursor_install.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cursor_install.py`:

```python
"""Tests for the Cursor adapter install/uninstall."""
import json
import sys
from pathlib import Path

import pytest

from contexer.adapters import cursor


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestCursorInstall:
    def test_writes_mcp_json(self, home):
        cursor.install(home)
        mcp = json.loads((home / ".cursor" / "mcp.json").read_text())
        entry = mcp["mcpServers"]["contexer"]
        assert "contexer" in entry["command"]

    def test_writes_hooks_json_version_1(self, home):
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        assert hooks["version"] == 1

    def test_session_start_hook_calls_adapter(self, home):
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for h in hooks["hooks"]["sessionStart"]]
        assert any("cursor.session_start" in c for c in cmds)

    def test_before_submit_prompt_hooks_registered(self, home):
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for h in hooks["hooks"]["beforeSubmitPrompt"]]
        assert any("cursor.capture_task" in c for c in cmds)
        assert any("cursor.capture_constraint" in c for c in cmds)

    def test_uses_current_python(self, home):
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for h in hooks["hooks"]["sessionStart"]]
        assert any(sys.executable in c for c in cmds)

    def test_install_idempotent(self, home):
        cursor.install(home)
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for h in hooks["hooks"]["beforeSubmitPrompt"]]
        assert sum("cursor.capture_task" in c for c in cmds) == 1

    def test_preserves_existing_cursor_config(self, home):
        mcp_path = home / ".cursor" / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        mcp_path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
        cursor.install(home)
        mcp = json.loads(mcp_path.read_text())
        assert "other" in mcp["mcpServers"]
        assert "contexer" in mcp["mcpServers"]


class TestCursorUninstall:
    def test_removes_mcp_entry(self, home):
        cursor.install(home)
        cursor.uninstall(home)
        mcp = json.loads((home / ".cursor" / "mcp.json").read_text())
        assert "contexer" not in mcp.get("mcpServers", {})

    def test_removes_contexer_hooks_only(self, home):
        hooks_path = home / ".cursor" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps(
            {"version": 1, "hooks": {"afterFileEdit": [{"command": "./mine.sh"}]}}))
        cursor.install(home)
        cursor.uninstall(home)
        hooks = json.loads(hooks_path.read_text())
        # user's own hook survives; contexer's sessionStart is gone
        assert hooks["hooks"]["afterFileEdit"][0]["command"] == "./mine.sh"
        assert not hooks["hooks"].get("sessionStart")

    def test_uninstall_idempotent(self, home):
        cursor.install(home)
        cursor.uninstall(home)
        cursor.uninstall(home)  # must not raise
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cursor_install.py --no-cov -v`
Expected: FAIL — missing `cursor.install`/`uninstall`.

- [ ] **Step 3: Implement install/uninstall/status in `contexer/adapters/cursor.py`**

Add `import shutil` and `import sys`, `from contexer.adapters import base`, then:

```python
_HOOK_MARKER_TASK = "cursor.capture_task"
_HOOK_MARKER_CON = "cursor.capture_constraint"
_HOOK_MARKER_SS = "cursor.session_start"


def _cmd(entry: str) -> str:
    """A Cursor command hook: pass repo via "" (session_start reads workspace_roots from
    stdin); read stdin for prompt/session. Cursor runs hooks from the project root."""
    python = sys.executable
    return (f'"{python}" -c "from contexer.adapters import cursor; import sys; '
            f'print(cursor.{entry}(\'\', sys.stdin.read()))"')


def _has(hook_list: list, marker: str) -> bool:
    return any(marker in h.get("command", "") for h in hook_list)


def install(home: Path) -> list[str]:
    log: list[str] = []
    cursor_dir = home / ".cursor"
    contexer_bin = shutil.which("contexer") or "contexer"

    mcp_path = cursor_dir / "mcp.json"
    mcp = base._load(mcp_path)
    mcp.setdefault("mcpServers", {})["contexer"] = {"command": contexer_bin}
    base._save(mcp_path, mcp)
    log.append("  ✓ MCP server registered in ~/.cursor/mcp.json")

    hooks_path = cursor_dir / "hooks.json"
    cfg = base._load(hooks_path)
    cfg["version"] = 1
    hk = cfg.setdefault("hooks", {})

    ss = hk.setdefault("sessionStart", [])
    if not _has(ss, _HOOK_MARKER_SS):
        ss.append({"type": "command", "command": _cmd("session_start")})

    bsp = hk.setdefault("beforeSubmitPrompt", [])
    if not _has(bsp, _HOOK_MARKER_TASK):
        bsp.append({"type": "command", "command": _cmd("capture_task")})
    if not _has(bsp, _HOOK_MARKER_CON):
        bsp.append({"type": "command", "command": _cmd("capture_constraint")})

    base._save(hooks_path, cfg)
    log.append("  ✓ Hooks registered in ~/.cursor/hooks.json")
    log.append("  ℹ Cursor hooks require Cursor 1.7+.")
    return log


def uninstall(home: Path) -> list[str]:
    log: list[str] = []
    cursor_dir = home / ".cursor"

    mcp_path = cursor_dir / "mcp.json"
    if mcp_path.exists():
        mcp = base._load(mcp_path)
        if mcp.get("mcpServers", {}).pop("contexer", None):
            base._save(mcp_path, mcp)
            log.append("  ✓ MCP server removed from ~/.cursor/mcp.json")

    hooks_path = cursor_dir / "hooks.json"
    if hooks_path.exists():
        cfg = base._load(hooks_path)
        hk = cfg.get("hooks", {})
        changed = False
        for event, markers in {
            "sessionStart": [_HOOK_MARKER_SS],
            "beforeSubmitPrompt": [_HOOK_MARKER_TASK, _HOOK_MARKER_CON],
        }.items():
            before = hk.get(event, [])
            after = [h for h in before
                     if not any(m in h.get("command", "") for m in markers)]
            if after != before:
                changed = True
                if after:
                    hk[event] = after
                else:
                    hk.pop(event, None)
        if changed:
            base._save(hooks_path, cfg)
            log.append("  ✓ Hooks removed from ~/.cursor/hooks.json")
    return log


def status_lines(home: Path) -> list[str]:
    cursor_dir = home / ".cursor"
    mcp = base._load_safe(cursor_dir / "mcp.json").get("mcpServers", {}).get("contexer")
    hk = base._load_safe(cursor_dir / "hooks.json").get("hooks", {})
    ss = hk.get("sessionStart", []) if isinstance(hk, dict) else []
    hooks_ok = any(_HOOK_MARKER_SS in h.get("command", "") for h in ss)
    return [
        "  [cursor]",
        f"    MCP server: {'registered' if mcp else 'NOT registered'}",
        f"    hooks:      {'installed' if hooks_ok else 'missing or partial'}",
    ]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_cursor_install.py --no-cov -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add contexer/adapters/cursor.py tests/test_cursor_install.py
git commit -m "feat(adapters): cursor install/uninstall/status (mcp.json + hooks.json)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# Phase 4 — CLI auto-detect + `--target`

## Task 4.1: Target selection in the CLI

**Files:**
- Modify: `contexer/cli.py`
- Test: `tests/test_install.py`, `tests/test_cli_commands.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install.py`:

```python
class TestTargetSelection:
    def test_install_target_cursor_only(self, clean_home, monkeypatch):
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install", "--target", "cursor"])
        cli.main()
        assert (clean_home / ".cursor" / "mcp.json").exists()
        assert not (clean_home / ".claude.json").exists()

    def test_install_target_all(self, clean_home, monkeypatch):
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install", "--target", "all"])
        cli.main()
        assert (clean_home / ".cursor" / "mcp.json").exists()
        assert (clean_home / ".claude.json").exists()

    def test_install_autodetects_present_tools(self, clean_home, monkeypatch):
        # Only ~/.cursor present -> only Cursor wired.
        (clean_home / ".cursor").mkdir()
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install"])
        cli.main()
        assert (clean_home / ".cursor" / "mcp.json").exists()
        assert not (clean_home / ".claude.json").exists()

    def test_install_defaults_to_claude_when_none_detected(self, clean_home, monkeypatch):
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install"])
        cli.main()
        assert (clean_home / ".claude.json").exists()

    def test_install_unknown_target_exits_1(self, clean_home, monkeypatch, capsys):
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install", "--target", "emacs"])
        with pytest.raises(SystemExit) as e:
            cli.main()
        assert e.value.code == 1
        assert "unknown target" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_install.py::TestTargetSelection --no-cov -v`
Expected: FAIL — `--target` not parsed; bare install only does Claude.

- [ ] **Step 3: Implement target selection in `contexer/cli.py`**

Add a resolver and route `install`/`uninstall`/`status` through it:

```python
from contexer import adapters


def _resolve_targets(rest: list) -> list:
    """--target claude|cursor|all; default = auto-detect, falling back to claude."""
    target = None
    if "--target" in rest:
        i = rest.index("--target")
        if i + 1 < len(rest):
            target = rest[i + 1]
    if target:
        try:
            return adapters.select(target)
        except KeyError:
            print(f"Unknown target: {target} (choose claude, cursor, or all)",
                  file=sys.stderr)
            sys.exit(1)
    detected = adapters.detect()
    return detected or [adapters.get("claude")]


def install(rest: list | None = None) -> None:
    home = Path.home()
    (home / ".contexer").mkdir(exist_ok=True)
    for adapter in _resolve_targets(rest or []):
        print(f"Installing for {adapter.NAME}...")
        for line in adapter.install(home):
            print(line)
    print("\nDone. Restart your AI assistant and open any git repo to activate Contexer.")
```

Update `uninstall`/`status` similarly to iterate `_resolve_targets(rest)`; keep the shared `--purge` store deletion in `cli.uninstall` (run once after the loop). Update `main()` to pass `rest` into `install`/`uninstall`/`status`:

```python
    elif cmd == "install":
        _run_guarded(lambda: install(rest))
    elif cmd == "uninstall":
        _run_guarded(lambda: uninstall(rest))
    elif cmd == "status":
        status(rest)
```

> Note: the `installed_home` fixture in `tests/test_install.py` calls `install()` with no args — keep `install`'s signature defaulting `rest=None` so existing tests pass unchanged.

- [ ] **Step 4: Run target + existing install/cli tests**

Run: `uv run pytest tests/test_install.py tests/test_cli_commands.py --no-cov -v`
Expected: PASS (new target tests + all existing).

- [ ] **Step 5: Run the FULL suite**

Run: `uv run pytest`
Expected: PASS, coverage ≥ 85%.

- [ ] **Step 6: Commit**

```bash
git add contexer/cli.py tests/test_install.py tests/test_cli_commands.py
git commit -m "feat(cli): auto-detect targets + --target claude|cursor|all

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task 4.2: Update `status` to show all installed targets

**Files:**
- Modify: `contexer/cli.py` (`status`)
- Test: `tests/test_cli_commands.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_commands.py` (mirror the existing no-network `status` fixtures at the top of that file):

```python
class TestStatusMultiTarget:
    def test_status_shows_cursor_when_installed(self, clean_home, monkeypatch, capsys):
        import sys as _sys
        import contexer.cli as cli
        monkeypatch.setattr(_sys, "argv", ["contexer", "install", "--target", "cursor"])
        cli.main()
        capsys.readouterr()
        cli.status(["--target", "cursor"])
        out = capsys.readouterr().out
        assert "[cursor]" in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cli_commands.py::TestStatusMultiTarget --no-cov -v`
Expected: FAIL — `status` doesn't print cursor lines.

- [ ] **Step 3: Implement**

In `contexer/cli.py` `status(rest)`, after printing the version/binary/update-check header, loop:

```python
    for adapter in _resolve_targets(rest):
        for line in adapter.status_lines(home):
            print(line)
```

(Keep the existing store-summary + corrupt-config warnings, which are shared/global.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_cli_commands.py --no-cov -v`
Expected: PASS.

- [ ] **Step 5: Run the FULL suite**

Run: `uv run pytest`
Expected: PASS, coverage ≥ 85%.

- [ ] **Step 6: Commit**

```bash
git add contexer/cli.py tests/test_cli_commands.py
git commit -m "feat(cli): status reports per-target install state

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# Phase 5 — Docs

## Task 5.1: README + CLAUDE.md — Cursor install + parity matrix

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `README.md`**

Add a "Use with Cursor" section after the existing Claude install instructions:

```markdown
## Use with Cursor (1.7+)

```bash
contexer install --target cursor   # or: contexer install (auto-detects ~/.cursor)
```

This registers Contexer's MCP server in `~/.cursor/mcp.json` and wires two Cursor
hooks in `~/.cursor/hooks.json`:

- `sessionStart` — injects your stored project rules + a usage nudge into the session.
- `beforeSubmitPrompt` — silently captures your task and any "always/never" directives.

**Parity note:** Cursor can only inject context at session start (not per-prompt) and
exposes no compaction hooks. So per-prompt rationale injection and post-edit reminders
are delivered as a one-time session-start nudge instead. The core value — automatic
session-start injection of your stored rules — works identically to Claude Code.
```

Also update any "Install" heading to mention `--target claude|cursor|all` and auto-detect.

- [ ] **Step 2: Update `CLAUDE.md`**

In the Architecture section, add a bullet documenting the adapter seam:

```markdown
- **`contexer/adapters/`** — one module per AI-assistant target. `base.py` holds shared
  config-file helpers (`_load`/`_save`/marker checks). `claude.py` and `cursor.py` each
  own that tool's MCP registration, hook wiring, install/uninstall/status, and the
  formatters that turn `store.py`'s neutral payloads (`session_start_payload`,
  `bootstrap_prompt_payload`, `post_compact_payload`) into that tool's hook-output JSON.
  `__init__.py` is the registry (`detect()` / `select()`). Add a tool = add a module here;
  `store.py` never changes. Cursor parity is capped by its platform: it injects context
  only at `sessionStart`, so per-prompt rationale injection and post-edit reminders
  degrade to a session-start nudge, and `PreCompact`/`PostCompact` are dropped.
```

In the MCP integration section, note `~/.cursor/mcp.json` + `~/.cursor/hooks.json` as the Cursor equivalents of `~/.claude.json` + `~/.claude/settings.json`.

- [ ] **Step 3: Verify the full suite is still green**

Run: `uv run pytest`
Expected: PASS, coverage ≥ 85%.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document Cursor install + multi-provider adapter seam

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# Final verification

- [ ] **Run the full suite with coverage:** `uv run pytest` → all pass, coverage ≥ 85%.
- [ ] **Claude smoke test (regression):**
  ```bash
  echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | uv run python server.py
  ```
  Expected: a valid MCP `initialize` response (server still works).
- [ ] **Claude install round-trip in a temp HOME:**
  ```bash
  HOME=$(mktemp -d) uv run contexer install && HOME=$(mktemp -d) uv run contexer install --target all && echo OK
  ```
- [ ] **Cursor JSON validity:**
  ```bash
  H=$(mktemp -d); HOME=$H uv run contexer install --target cursor; python -c "import json; json.load(open('$H/.cursor/mcp.json')); json.load(open('$H/.cursor/hooks.json')); print('valid')"
  ```
- [ ] **(Optional, if a Cursor 1.7+ install is available)** Manually point Cursor at this checkout, open a repo with seeded context, and confirm the session-start injection appears and `update_context`/`get_context` are callable.

---

# Self-Review (completed by planner)

**Spec coverage:**
- Neutral payloads (store.py) → Tasks 1.2, 1.3. ✅
- `contexer/adapters/` with claude + cursor over one contract → Phase 0, 2, 3. ✅
- Session-start parity on Cursor → Task 3.1 (`format_session_start`), 3.2 (`session_start`). ✅
- Capture-path unification onto command hooks (both providers) → Tasks 2.2, 2.3, 3.2. ✅
- Auto-detect + `--target` → Task 4.1. ✅
- Per-target status/uninstall → Tasks 3.3, 4.1, 4.2. ✅
- Behavioral nudge in `sessionStart.additional_context` → Task 3.1. ✅
- Benchmark/regression gate for the Claude capture change → Task 2.3 Step 6 + Final verification. ✅
- `--with-rules` rules-file → **intentionally omitted** (deferred per spec). ✅
- Known gaps (no PreCompact/PostCompact, latest-vs-first task, no resume-awareness on Cursor) → documented in Task 3.2 note + spec. ✅

**Placeholder scan:** No TBD/TODO; every code step shows full code; commands have expected output. ✅

**Type/name consistency:** Adapter modules expose `NAME`, `is_present`, `install`, `uninstall`, `status_lines`, plus entrypoints (`session_start`, `capture_task`, `capture_constraint`, `rationale` [claude only], formatters). `_resolve_targets` used consistently in `install`/`uninstall`/`status`. Neutral payload keys `{"status","context"}` consistent across `store.*_payload` and both adapters' `format_session_start`. ✅
