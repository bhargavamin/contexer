Run a guided context setup for this repo.

1. Call `bootstrap_context` with `repo_path=""` to get inferred facts and gap assumptions.
2. Present each item one at a time — state what was detected or assumed, then ask "Correct? (yes / no / your correction)". Wait for the reply before moving on.
3. After each reply, call `update_context` to store the confirmed fact with full reasoning.
4. When all items are done, confirm how many were stored.

Keep it conversational — no upfront lists, one item per turn.
