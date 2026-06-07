# Bootstrap

Bootstrap initialises context for a repo that has no stored decisions. It scans the repo, surfaces detected facts and assumptions, and guides you through confirming each one — so future sessions start with a rich baseline instead of nothing.

---

## When it runs

Bootstrap runs automatically the first time you open Claude Code in a repo where no context exists. Claude will pause your original request, run bootstrap, and then answer your question once it is done.

If you want to skip it and come back later, type `skip` when Claude presents the first item.

---

## What happens during bootstrap

1. Claude scans your repo — package files, config files, CI, Dockerfile, etc.
2. For each detected fact (e.g. `Framework: Next.js`, `Package manager: pnpm`), Claude states it and asks: `Correct? yes / no / [your correction]`
3. You reply. Claude stores the confirmed or corrected fact and moves to the next item.
4. After facts, Claude asks a small set of questions about things it cannot infer from code — purpose, team conventions, deployment, constraints.
5. Once all items are done, Claude answers your original question.

Bootstrap typically takes 2–5 minutes. The questions are conditional — you will only be asked things relevant to your stack.

---

## Run bootstrap manually

Type `/bootstrap` at any time to trigger a guided setup on demand — even if context already exists. New answers are deduplicated against what is already stored, so it is safe to re-run.

```
/bootstrap
```

Use this when:
- The automatic bootstrap was interrupted mid-way
- You want to re-confirm assumptions after major changes to the stack
- You want to seed context for a repo that has some entries but is missing key facts

---

## Reset bootstrap for a repo

To trigger automatic bootstrap again, clear the context store for that repo:

```bash
ls ~/.contexer/
echo '{"entries":[]}' > ~/.contexer/<repo_slug>.json
```

The next session will detect no decisions and run bootstrap automatically.
