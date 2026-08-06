# Contributing to Contexer

Thanks for your interest. Contexer is a small, focused tool — no framework, one store, thin layers over it — and contributions should keep it that way.

---

## Development setup

**Prerequisites:** [uv](https://docs.astral.sh/uv/getting-started/installation/) on your `PATH`.

```bash
git clone https://github.com/bhargavamin/contexer.git
cd contexer
uv sync
```

Run the test suite:

```bash
uv run pytest tests/
```

Running a subset (one file, `-k`, one class) exits non-zero on coverage even when
every test passes: the ≥85% floor in `pyproject.toml`'s `addopts` judges whole-package
coverage, and a single file naturally covers ~10%. That flag must stay in `addopts`
(the convention miner reads it — see the comment there), so pass `--no-cov` instead:

```bash
uv run pytest tests/test_miner.py --no-cov
```


Lint (CI runs the same pinned version; keep the two in step when bumping it):

```bash
uvx ruff@0.15.4 check .
uvx ruff@0.15.4 check --fix .    # apply the safe fixes
```

Rules live in `pyproject.toml` under `[tool.ruff.lint]`. Ruff is intentionally not a dev-group
dependency — it is a binary we never import, so pinning it here and in the workflow keeps
`uv.lock` out of the loop.

Smoke-test the MCP server:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
  | uv run python server.py
```

---

## Project structure

**One write path: every read and write of a decision goes through `store.py`.** That is the rule
to protect. Everything else is a thin layer over it — a protocol surface, a host integration, a
transport, a presentation — and none of them may open a store file or reimplement store logic. A
new module is fine when it is genuinely a new *layer*; a new module that knows the entry schema is
not.

| Module | Role |
|---|---|
| `contexer/store.py` | The store: all decision read/write, dedup/novelty filtering, revisions, tombstones, session-start and bootstrap payloads. Every logic change belongs here |
| `contexer/server.py` | MCP server entry point — maps MCP tool calls to store functions and nothing else |
| `contexer/cli.py` | The `contexer` console script — `install` / `uninstall [--purge]` / `reinstall` / `review` / `ui` / `share` / `login` / `logout` / `status` / `version`, and the bare MCP server with no args |
| `contexer/config.py` | Loader and writer for `~/.contexer/config.toml` (`Profile` + the `[ui]` table). At the bottom of the import graph — it imports nothing of ours |
| `contexer/adapters/` | One module per host (`claude`, `cursor`, `codex`, `gemini`) behind the duck-typed contract in `base.py`: install/uninstall wiring plus the hook entrypoints. Host-specific JSON shapes stop here |
| `contexer/ui/` | The local console: `daemon.py` (processes and sockets only), `server.py` (HTTP only), `api.py` (the one module that knows both HTTP and the store), `assets/` (hand-written HTML/CSS/JS, no build step) |
| `contexer/auth.py` | Contexer Teams OAuth (`contexer login` / `logout`) and credential storage |
| `contexer/remote.py` | MCP client to the Teams endpoint — the sync transport for push and pull |
| `contexer/share.py` | Explicit push of local decisions upward |
| `contexer/team_context.py` | Team-context cache: refresh, staleness, merge at read time. Team rows are never written into the local store |
| `contexer/memory_sync.py` | Imports Claude Code memory-tool facts into the store |
| `contexer/miner.py` | Deterministic convention mining over a repo (no model in the loop). A leaf — imports nothing of ours |
| `contexer/redact.py` | Secret scrubbing at capture and at egress. A leaf |
| `contexer/repo_key.py` | Canonical repo-key derivation, kept byte-compatible with the TypeScript sibling |

Dependency direction is worth preserving: `adapters`, `server`, `cli` and `ui` all sit above
`store`, and `store` sits above the leaves (`miner`, `redact`, `repo_key`, `config`). There is one
deliberate upward edge — `store` calls `ui.daemon` from inside a function to append the console URL
at session start. It cannot cycle, because `daemon` imports nothing of ours at module scope, and
that import budget is enforced by a test.

---

## Code style

- **No comments that explain what the code does.** Only add a comment when the WHY is non-obvious: a hidden constraint, a workaround, a subtle invariant.
- **No premature abstractions.** Three similar lines is better than an abstraction that anticipates a hypothetical future.
- **No error handling for impossible cases.** Only validate at system boundaries (user input, file I/O, external calls).
- **Single responsibility per function.** Clean inputs and outputs — plain dicts and strings, no shared mutable state.
- **Silent operation is a hard constraint.** `update_context` must never log or print filtered decisions. Discard silently, always.

---

## Writing tests

Tests live in `tests/`. Every change to `store.py` or `server.py` requires a corresponding test.

Cover:
- The happy path
- The most likely failure modes (empty store, duplicate content, corrupt file)
- Boundary conditions (store at capacity, keyword length edge cases)

Use `tmp_path` / `tmp_path_factory` for all file I/O — never write to the real `~/.contexer/` in tests. Monkeypatch `store.STORE_DIR` at the function scope (not module scope) when each test needs an isolated directory.

Run the full suite before submitting a PR:

```bash
uv run pytest tests/ -v
```

---

## Commit format

All commits must follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): short description

Optional body with reasoning when non-obvious.
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `chore`, `test`

**Scope:** optional; use it when the change is scoped to a specific file or subsystem — `fix(store):`, `docs(readme):`, `feat(cli):`.

Examples:

```
feat(store): add word-boundary matching to get_context query filter
fix(store): strip punctuation before tokenising in novelty filter
docs(readme): add token cost section with per-rule breakdown
test(benchmark): add display cap boundary tests
chore(release): bump version to v0.1.4
```

---

## Pull request process

1. Fork the repo and create a branch from `main`.
2. Make your change. Keep the diff small and focused — one thing per PR.
3. Add or update tests. All existing tests must pass.
4. Update `README.md` and `docs/` if the change affects user-visible behaviour.
5. Open a PR. Fill in the PR template.

PRs that add new configuration, classes, or abstraction layers need a clear explanation of why the existing structure cannot handle the use case.

---

## What belongs here

Good contributions:

- Bug fixes in the novelty filter, keyword extraction, or query matching
- Accuracy improvements with benchmark evidence
- Documentation improvements
- Test coverage for untested edge cases
- Security fixes

Out of scope for this repo:

- Team sync / multi-user features (planned for a future paid version)
- New MCP tools beyond the current set (eight: capture, constraint capture, update, get, prompt-triggered get, bootstrap, and the two global-store tools)
- Alternative storage backends
- Language support beyond Python

If you're unsure whether something fits, open an issue before writing code.

---

## Reporting bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md). Include the output of `contexer --version` and your Claude Code version.

## Community

Questions, ideas, or feedback? Join us on [Discord](https://discord.gg/Fk6JSaW4p) or browse [contexer.ai](https://contexer.ai).
