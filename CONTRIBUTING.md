# Contributing to Contexer

Thanks for your interest. Contexer is a small, focused tool — three core modules, no framework — and contributions should keep it that way.

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

Smoke-test the MCP server:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
  | uv run python server.py
```

---

## Project structure

Three core modules, intentionally. Do not add a fourth without discussion.

| File | Role |
|---|---|
| `contexer/server.py` | MCP server entry point — defines tools, delegates to `store.py` |
| `contexer/store.py` | All read/write and filtering logic |
| `contexer/cli.py` | The `contexer` console script — runs the server bare; `install` / `uninstall [--purge]` / `reinstall` / `status` / `version` subcommands |

Any logic change belongs in `store.py`. `server.py` is thin by design — it maps MCP calls to store functions and nothing else.

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
