Run a guided context setup for this repo.

1. Call `bootstrap_context` with `repo_path=""` and no `insight` — the server auto-detects how well the user knows the repo from git history.
2. If the result has `"decisive": false`, ask: "How well do you know this repo — wrote/maintain it, work with it but didn't build it, or first time seeing it?" Then re-call `bootstrap_context` with `insight="high"`, `"medium"`, or `"low"` accordingly.
3. High insight: present each inferred item one at a time — state what was detected or assumed, then ask "Correct? (yes / no / your correction)". Wait for the reply before moving on. After each reply, call `update_context` to store the confirmed fact with full reasoning. Then ask each gap question the same way.
4. Medium or low insight: store inferred facts directly via `update_context` — no confirmation, the user can't validate facts about code they didn't write. Read the README and docs to determine the repo's purpose and store it. Ask the returned gap questions (their goal; plus purpose at medium) and store each answer.
5. When all items are done, confirm how many were stored.

Keep it conversational — no upfront lists, one item per turn.
