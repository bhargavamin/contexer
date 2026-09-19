# Decision-impact pilot

The decision-impact workflow is a new, local, opt-in pilot. It can answer a narrow question:

> Did this exact file snapshot satisfy or violate this specific armed decision condition?

It does not prove that an agent read or obeyed guidance, that a whole task was correct, or that
Contexer improved the result. Ordinary edits, prompts, decision lookups, diagnostics consent, and
workspace grants do not run checks. A check runs only when you request it, or when you separately
opt into a workflow with a defined trigger and disable path.

## One-time setup

The two permissions are independent and both default off. Add them to
`~/.contexer/config.toml` only for repositories you intend to use in the pilot:

```toml
[diagnostics]
decision_impact = true

[policy]
artifact_read_roots = ["/absolute/canonical/path/to/project"]
```

`decision_impact` permits bounded local metadata receipts for repositories used by this
installation. It never stores prompts, conversations, decision bodies, regular-expression text,
file contents, or match snippets. `artifact_read_roots` separately permits an explicitly requested
evaluation to read one repository-relative text file. Each physical worktree needs its own exact
grant; parent directories, home/config directories, symlinks, and shared current-repository pointer
fallback are refused.

The decision must already be approved, trusted, and explicitly armed with a supported condition.
The first pilot supports only a literal ASCII regex, optionally line-anchored, with no flags and no
regex operators. Existing Guard rules are not changed or armed automatically.

## What to ask in an agent session

Use an explicit request such as:

> Retrieve the decision about forbidden direct HTTP imports. After I finish this change, use
> Contexer's requested file check on `src/client.py`, link the guidance receipt, and show me the
> retained decision-impact result.

The visible flow is:

1. `get_context` returns the relevant decision. When diagnostics collection succeeds, its normal
   answer ends with an opaque `Contexer decision-impact receipt`.
2. On your requested check, `evaluate_policy` reads the granted file itself through
   `artifact_path`. It reports the ordinary advisory verdict, exact file digest/provenance, and an
   evaluation receipt. Unsupported rules, unreadable/changed files, budget exhaustion, and missing
   identity remain unverified rather than becoming passes.
3. `get_decision_impact` reads that receipt without rereading the file or running another check. It
   names the exact decision revision, rule digest, file snapshot, condition result, and whether the
   caller linked it to the earlier guidance receipt.

A safe snapshot is reported as a specific condition satisfied. A match is reported as a violation
observed. Neither claim extends beyond that condition and snapshot. A missing or invalid guidance
reference changes only attribution; it cannot change the check verdict.

## Local CLI and console

The same report is available without asking an agent:

```bash
contexer status --impact
contexer status --impact --json
contexer status --impact --receipt-id <receipt>
```

Use `--cursor <next_cursor>` to continue a retained-history page and `--file <repo-relative-path>`
to filter metadata. The repository dashboard in `contexer ui` shows the five newest observations.
Opening either report is read-only and never runs a policy evaluation.

Clear only the current repository's impact history with an explicit confirmation:

```bash
contexer status --impact --clear --confirm
```

Disabling `[diagnostics]` stops new receipt collection but retains existing history. Removing a root
from `[policy].artifact_read_roots` revokes server-read file access on the next requested evaluation.
History is local and bounded to 256 records, 256 KiB, and seven days per canonical repository.
