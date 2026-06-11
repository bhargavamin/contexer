Run a guided context setup for this repo.

1. Ask first: "Are you a developer/maintainer of this repo, or exploring it for the first time?" Wait for the reply.
2. Call `bootstrap_context` with `repo_path=""` and `audience="developer"` or `audience="explorer"` based on the answer.
3. Developer: present each item one at a time — state what was detected or assumed, then ask "Correct? (yes / no / your correction)". Wait for the reply before moving on. After each reply, call `update_context` to store the confirmed fact with full reasoning.
4. Explorer: store inferred facts directly via `update_context` — no confirmation, the user can't validate facts about code they didn't write. Read the README and docs to determine the repo's purpose and store it. Ask only the one gap question returned (what they plan to do here) and store the answer.
5. When all items are done, confirm how many were stored.

Keep it conversational — no upfront lists, one item per turn.
