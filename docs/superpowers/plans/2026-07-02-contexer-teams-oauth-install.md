# contexer-teams OAuth Install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On `contexer install`, register a second Claude Code MCP entry `contexer-teams` pointing at the remote Contexer server so the client's native OAuth completes on first use — without touching the local stdio server, hooks, or `~/.contexer` store.

**Architecture:** Purely additive. `contexer/adapters/claude.py` gains a `_teams_url()` selector (prod by default, `localhost` only via `CONTEXER_ENV=local`) and writes `mcpServers["contexer-teams"] = {"type":"http","url":...}` alongside the existing `contexer` stdio entry via the same `base._load`/`_save` helpers. Uninstall pops only that key. `{type:http,url}` is the shape that triggers Claude Code's native OAuth (401 → DCR → browser PKCE → token → silent refresh); no token is ever written.

**Tech Stack:** Python 3.12, `uv`, pytest. No new dependencies.

## Global Constraints

- Default remote endpoint MUST be `https://mcp.dev.contexer.ai/mcp` (HTTPS). Never register localhost for a normal user.
- localhost (`http://localhost:8080/mcp`) only via explicit `CONTEXER_ENV=local`, off by default.
- Never write a token/secret into config; the entry has exactly `{"type","url"}`.
- Idempotent install/uninstall; merge into existing config, never clobber unrelated `mcpServers` keys or the local `contexer` stdio entry.
- Claude Code CLI only this iteration.
- Entry name is exactly `contexer-teams`.
- Prod URL lives in ONE baked-in constant (`CONTEXER_TEAMS_PROD`).
- Follow existing repo style: adapters use `base._load`/`_save`; tests use the `clean_home`/`installed_home` fixtures in `tests/test_install.py`.

---

### Task 1: Endpoint selector `_teams_url()`

**Files:**
- Modify: `contexer/adapters/claude.py` (imports at line 2-6; add module constants + helper after `NAME = "claude"` at line 20)
- Test: `tests/test_install.py`

**Interfaces:**
- Produces: `contexer.adapters.claude._teams_url() -> str` — returns `"https://mcp.dev.contexer.ai/mcp"` unless `CONTEXER_ENV == "local"`, then `"http://localhost:8080/mcp"`. Module constants `CONTEXER_TEAMS_PROD` and `CONTEXER_TEAMS_LOCAL`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_install.py` (the file already imports `json`, `sys`, `Path`, and `from contexer.cli import install, uninstall`; add the adapter import at the top of the file if not present):

```python
from contexer.adapters import claude


class TestTeamsUrl:
    def test_defaults_to_prod(self, monkeypatch):
        monkeypatch.delenv("CONTEXER_ENV", raising=False)
        assert claude._teams_url() == "https://mcp.dev.contexer.ai/mcp"

    def test_local_env_selects_localhost(self, monkeypatch):
        monkeypatch.setenv("CONTEXER_ENV", "local")
        assert claude._teams_url() == "http://localhost:8080/mcp"

    def test_unknown_env_falls_back_to_prod(self, monkeypatch):
        monkeypatch.setenv("CONTEXER_ENV", "staging")
        assert claude._teams_url() == "https://mcp.dev.contexer.ai/mcp"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install.py::TestTeamsUrl -q`
Expected: FAIL — `AttributeError: module 'contexer.adapters.claude' has no attribute '_teams_url'`

- [ ] **Step 3: Add `os` import and the selector**

In `contexer/adapters/claude.py`, add `import os` to the stdlib import block (line 2-6, keep alphabetical: before `import re`):

```python
"""Claude Code integration adapter."""
import json
import os
import re
import shutil
import sys
from pathlib import Path
```

Immediately after `NAME = "claude"` (line 20), add:

```python
# Remote teams MCP endpoint. Prod HTTPS by default; localhost only via the explicit
# CONTEXER_ENV=local developer opt-in (never registered for a normal user).
CONTEXER_TEAMS_PROD = "https://mcp.dev.contexer.ai/mcp"
CONTEXER_TEAMS_LOCAL = "http://localhost:8080/mcp"


def _teams_url() -> str:
    return CONTEXER_TEAMS_LOCAL if os.environ.get("CONTEXER_ENV") == "local" else CONTEXER_TEAMS_PROD
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install.py::TestTeamsUrl -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add contexer/adapters/claude.py tests/test_install.py
git commit -m "feat(claude): add _teams_url endpoint selector (prod default, CONTEXER_ENV=local override)"
```

---

### Task 2: Install registers `contexer-teams`

**Files:**
- Modify: `contexer/adapters/claude.py:217-225` (the `# MCP server (~/.claude.json)` block in `install`)
- Test: `tests/test_install.py`

**Interfaces:**
- Consumes: `_teams_url()` from Task 1; `base._load`/`_save` (already imported).
- Produces: after `install`, `~/.claude.json` `mcpServers["contexer-teams"] == {"type":"http","url":_teams_url()}`, added alongside the untouched `contexer` stdio entry.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_install.py`:

```python
class TestTeamsRegistration:
    def test_registers_contexer_teams_http_prod(self, clean_home, monkeypatch):
        monkeypatch.delenv("CONTEXER_ENV", raising=False)
        install()
        servers = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        assert servers["contexer-teams"] == {"type": "http", "url": "https://mcp.dev.contexer.ai/mcp"}

    def test_local_env_registers_localhost(self, clean_home, monkeypatch):
        monkeypatch.setenv("CONTEXER_ENV", "local")
        install()
        servers = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        assert servers["contexer-teams"]["url"] == "http://localhost:8080/mcp"

    def test_local_stdio_entry_untouched(self, clean_home):
        install()
        servers = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        assert servers["contexer"]["type"] == "stdio"
        assert "command" in servers["contexer"]

    def test_reinstall_idempotent(self, installed_home):
        install()  # second install
        servers = json.loads((installed_home / ".claude.json").read_text())["mcpServers"]
        assert servers["contexer-teams"] == {"type": "http", "url": "https://mcp.dev.contexer.ai/mcp"}

    def test_preserves_unrelated_servers(self, clean_home):
        cfg = clean_home / ".claude.json"
        cfg.write_text(json.dumps({"mcpServers": {"other": {"type": "stdio", "command": "x"}}}))
        install()
        servers = json.loads(cfg.read_text())["mcpServers"]
        assert servers["other"] == {"type": "stdio", "command": "x"}
        assert "contexer-teams" in servers

    def test_no_token_or_secret_in_entry(self, clean_home):
        install()
        entry = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]["contexer-teams"]
        assert set(entry.keys()) == {"type", "url"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install.py::TestTeamsRegistration -q`
Expected: FAIL — `KeyError: 'contexer-teams'` (entry not written yet)

- [ ] **Step 3: Register the entry in `install`**

In `contexer/adapters/claude.py`, replace the block at lines 220-225:

```python
    claude.setdefault("mcpServers", {})["contexer"] = {
        "type": "stdio",
        "command": contexer_bin,
    }
    _save(claude_json, claude)
    log.append("  ✓ MCP server registered in ~/.claude.json")
```

with:

```python
    claude.setdefault("mcpServers", {})["contexer"] = {
        "type": "stdio",
        "command": contexer_bin,
    }
    # Remote teams MCP server (additive; leaves the local stdio entry above intact).
    # {type:http,url} is the shape that triggers Claude Code's native OAuth on first
    # use (401 → DCR → browser PKCE → token). No token is written here.
    teams_url = _teams_url()
    claude["mcpServers"]["contexer-teams"] = {"type": "http", "url": teams_url}
    _save(claude_json, claude)
    log.append("  ✓ MCP server registered in ~/.claude.json")
    log.append(f"  ✓ contexer-teams (remote) registered → {teams_url}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install.py::TestTeamsRegistration -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add contexer/adapters/claude.py tests/test_install.py
git commit -m "feat(claude): register contexer-teams remote MCP entry on install"
```

---

### Task 3: Uninstall removes only `contexer-teams`

**Files:**
- Modify: `contexer/adapters/claude.py:378-384` (the `mcpServers` pop block in `uninstall`)
- Test: `tests/test_install.py`

**Interfaces:**
- Consumes: install from Task 2.
- Produces: after `uninstall`, `mcpServers` has neither `contexer` nor `contexer-teams`; unrelated servers remain.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_install.py`:

```python
class TestTeamsUninstall:
    def test_uninstall_removes_contexer_teams(self, installed_home):
        uninstall()
        servers = json.loads((installed_home / ".claude.json").read_text()).get("mcpServers", {})
        assert "contexer-teams" not in servers

    def test_uninstall_preserves_unrelated_servers(self, clean_home):
        install()
        cfg = clean_home / ".claude.json"
        data = json.loads(cfg.read_text())
        data["mcpServers"]["other"] = {"type": "stdio", "command": "x"}
        cfg.write_text(json.dumps(data))
        uninstall()
        servers = json.loads(cfg.read_text()).get("mcpServers", {})
        assert servers.get("other") == {"type": "stdio", "command": "x"}
        assert "contexer-teams" not in servers
        assert "contexer" not in servers
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install.py::TestTeamsUninstall -q`
Expected: FAIL — `test_uninstall_removes_contexer_teams` fails (`contexer-teams` still present after uninstall)

- [ ] **Step 3: Pop the entry in `uninstall`**

In `contexer/adapters/claude.py`, replace lines 378-384:

```python
        claude = _load(claude_json)
        removed = claude.get("mcpServers", {}).pop("contexer", None)
        if removed:
            _save(claude_json, claude)
            log.append("  ✓ MCP server removed from ~/.claude.json")
        else:
            log.append("  - No MCP server entry found in ~/.claude.json")
```

with:

```python
        claude = _load(claude_json)
        removed = claude.get("mcpServers", {}).pop("contexer", None)
        removed_teams = claude.get("mcpServers", {}).pop("contexer-teams", None)
        if removed or removed_teams:
            _save(claude_json, claude)
            log.append("  ✓ MCP server removed from ~/.claude.json")
        else:
            log.append("  - No MCP server entry found in ~/.claude.json")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install.py::TestTeamsUninstall -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add contexer/adapters/claude.py tests/test_install.py
git commit -m "feat(claude): remove contexer-teams entry on uninstall"
```

---

### Task 4: `status` reports the teams entry

**Files:**
- Modify: `contexer/adapters/claude.py:464-472` (`status_lines`)
- Test: `tests/test_install.py`

**Interfaces:**
- Consumes: install from Task 2; `base._load_safe` (already imported).
- Produces: `status_lines(home)` includes a `teams (remote): registered → <url>` line when present, `NOT registered` otherwise. `is_installed` is deliberately unchanged (teams is not part of the local-install completeness check).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_install.py`:

```python
class TestTeamsStatus:
    def test_status_shows_teams_registered(self, installed_home):
        joined = "\n".join(claude.status_lines(installed_home))
        assert "teams (remote)" in joined
        assert "mcp.dev.contexer.ai" in joined

    def test_status_shows_teams_not_registered_on_clean(self, clean_home):
        joined = "\n".join(claude.status_lines(clean_home))
        assert "teams (remote)" in joined
        assert "NOT registered" in joined
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install.py::TestTeamsStatus -q`
Expected: FAIL — `test_status_shows_teams_registered` fails (no `teams (remote)` line)

- [ ] **Step 3: Add the status line**

In `contexer/adapters/claude.py`, replace `status_lines` (lines 464-472):

```python
def status_lines(home: Path) -> list[str]:
    """Diagnostic lines for `contexer status`: MCP/hooks state for the Claude target."""
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    mcp_cmd = mcp.get("command", "?") if isinstance(mcp, dict) else "?"
    return [
        "  [claude]",
        f"    MCP server: {'registered → ' + mcp_cmd if mcp else 'NOT registered'}",
        f"    hooks:      {'installed' if hooks_ok else 'missing or partial'}",
    ]
```

with:

```python
def status_lines(home: Path) -> list[str]:
    """Diagnostic lines for `contexer status`: MCP/hooks state for the Claude target."""
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    mcp_cmd = mcp.get("command", "?") if isinstance(mcp, dict) else "?"
    teams = _load_safe(home / ".claude.json").get("mcpServers", {}).get("contexer-teams")
    teams_url = teams.get("url") if isinstance(teams, dict) else None
    return [
        "  [claude]",
        f"    MCP server: {'registered → ' + mcp_cmd if mcp else 'NOT registered'}",
        f"    teams (remote): {'registered → ' + teams_url if teams_url else 'NOT registered'}",
        f"    hooks:      {'installed' if hooks_ok else 'missing or partial'}",
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install.py::TestTeamsStatus -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full suite (no regressions) and commit**

Run: `uv run pytest -q`
Expected: PASS (all tests, coverage gate ≥85% met)

```bash
git add contexer/adapters/claude.py tests/test_install.py
git commit -m "feat(claude): show contexer-teams in status output"
```

---

## Manual E2E verification (after all tasks, not a code step)

Documented for the reviewer; run on a real machine with Claude Code ≥ 2.1.64:

1. Back up `~/.claude.json`. Run `contexer install` (from this branch's built binary).
2. Confirm `~/.claude.json` has both `contexer` (stdio) and `contexer-teams` (`type:http`, prod URL); no token field anywhere.
3. Restart Claude Code; run `/mcp` → `contexer-teams` shows "needs authentication".
4. Authenticate → browser sign-in → returns connected. Confirm no token was written to `~/.claude.json` (it lives in the OS keychain).
5. `contexer uninstall` → `contexer-teams` gone, unrelated servers intact.

Server-side prerequisites to confirm with the teams-AS owner (from the spec): AS accepts `http://localhost:*` loopback redirect, advertises `offline_access`, and `/register` accepts repeated public-client DCR.
