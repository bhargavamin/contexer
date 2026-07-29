---
description: Guided Contexer setup — capture this repo's decisions for future sessions.
---

Run a guided context setup for this repo.

1. Call `bootstrap_context` with `repo_path=""` and no `insight` — it auto-detects insight AND stores repo facts + measured conventions automatically (idempotent, do NOT re-store what it returns). If the result has `"decisive": false`, ask the user how well they know the repo, then re-call with `insight` set to `"high"`, `"medium"`, or `"low"` (already-stored items are skipped).
2. Report in one line: N facts/conventions stored, M pending review.
3. Ask each residual `gaps` question following the `how_to_ask` field in the same result — it carries the exact question shape (one at a time, when to offer "Correct", when candidate options are warranted, what to store) and is the single source for it, so follow it verbatim rather than any remembered version.
4. Close with the counts; if anything is pending: "run `contexer review` when convenient" — never block.

Keep it conversational — no upfront lists, one item per turn.

<!-- managed by contexer — reinstall overwrites this file; edits will be lost -->
