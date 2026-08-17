# Bootstrap

Bootstrap initialises context for a repo that has no stored decisions. It scans the repo, stores the facts and conventions it can measure, and asks you only about the gaps it cannot infer from code - so future sessions start with a rich baseline instead of nothing.

---

## When it runs

Bootstrap runs automatically the first time you open Claude Code in a repo where no context exists. Claude will pause your original request, run bootstrap, and then answer your question once it is done.

If you want to skip it and come back later, choose `skip` when Claude presents the setup question.

---

## What happens during bootstrap

1. Claude scans your repo - package files, config files, CI, Dockerfile, etc.
2. Detected facts (e.g. `Framework: Next.js`, `Package manager: pnpm`) and measured conventions are stored automatically - there is no per-fact confirmation. Claude reports how many were stored in one line.
3. Claude reads your repo's own written context - `README.md`, `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, `.claude/rules/*.md`, `docs/` - before asking anything. Where one of those already answers a question, you are asked to confirm or correct the line rather than answer from scratch. Only your confirmed answer is stored, never the quoted line: a rules file is evidence for a question, not a decision. Where a rules file contradicts something measured in your code (a doc requiring full type hints next to a measured 61%), Claude asks about that contradiction - only you can say which is the rule.
4. Claude then asks the residual gap questions - the things neither the code nor the docs answer: purpose, team conventions, deployment, constraints.
5. Each gap is one interactive multiple-choice question (Claude Code: `AskUserQuestion`), asked one at a time: `Correct` (with the scan's assumption as its description) appears only where that assumption actually answers the question - some questions, such as what *you* plan to do with the repo, carry no assumption at all and are simply asked openly; at most two middle options offer concrete candidate answers, and only when the gap's hint actually names distinct candidates; the last option is `Skip this one`. Free text is always accepted through the tool's own `Other` choice. Assistants without such a tool print the same options numbered and accept the number or a typed answer.
6. Once the gaps are done, Claude answers your original question.

Bootstrap typically takes 2–5 minutes. The questions are conditional - you will only be asked things relevant to your stack.

---

## Run bootstrap manually

Type `/bootstrap` at any time to trigger a guided setup on demand - even if context already exists. New answers are deduplicated against what is already stored, so it is safe to re-run.

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
