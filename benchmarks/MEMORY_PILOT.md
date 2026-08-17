# Memory tool pilot findings

Recorded 2026-08-10, `claude --version` = `2.1.226 (Claude Code)`.

## What was run

Per the Task 1 brief's runbook, with one adjustment: `ANTHROPIC_API_KEY` is set
in this environment, so overriding `HOME` alone worked (no auth error, no need
for the scratch-cwd-without-HOME-override fallback described in the task
context).

```bash
PILOT=$(mktemp -d); mkdir -p $PILOT/home $PILOT/repo && git -C $PILOT/repo init -q
HOME=$PILOT/home claude -p "Remember this for future sessions: we deploy on Fridays only." \
  --model claude-sonnet-5 --output-format json --dangerously-skip-permissions > $PILOT/out.json
find $PILOT/home/.claude/projects -name "*.md"
claude --version
```

Note: the command was run from this repo's cwd (not `$PILOT/repo`), matching
the brief's literal runbook (it never `cd`s into `$PILOT/repo`). This confirms
the memory project dir is keyed off the process's cwd at invocation time, not
an argument, which is exactly what `memory_dir(home, repo)` needs to reproduce
by taking `repo` as an explicit path rather than reading cwd.

## (a) Default-on with no settings written

Yes. With `$PILOT/home` completely empty (no `.claude/settings.json` at all),
the run still produced:

```
<home>/.claude/projects/-Users-bhargavamin-repos-personal-contexer/memory/MEMORY.md
<home>/.claude/projects/-Users-bhargavamin-repos-personal-contexer/memory/deploy_schedule.md
```

`MEMORY.md` (the index) contained one line pointing at the fact file;
`deploy_schedule.md` carried YAML frontmatter (`name`, `description`,
`metadata.node_type: memory`, `metadata.type: project`,
`metadata.originSessionId`, `metadata.modified`) plus a body restating the
constraint and how to apply it. This matches the shape `contexer/memory_sync.py`
already parses. No `settings.json` was created by the run either, before or
after: the feature needs zero configuration to activate.

## (b) Disable key

None found. Checked:

- `claude --help`: no `config` subcommand is listed under `Commands:` in this
  version (running `claude config list --global` did not error into a usage
  message, it fell through to being treated as a chat prompt, since `config`
  is not a registered subcommand in 2.1.226; `--global` is also not a
  recognized top-level option). There is no settings-schema equivalent
  reachable through the CLI in this way.
- `claude project --help`: only a `purge` subcommand (deletes all state for a
  project), nothing memory-specific.
- Full `--help` text grepped for "memory": the only hits are `--bare`
  ("skip hooks, LSP, plugin sync, attribution, **auto-memory**, background
  prefetches, keychain reads, and CLAUDE.md auto-discovery") and
  `--exclude-dynamic-system-prompt-sections` (moves "memory paths" out of the
  system prompt, unrelated to enabling/disabling the feature itself).
- `--bare` is the only documented lever, and it is not a `settings.json` key:
  it is a CLI invocation flag that disables far more than memory (hooks,
  plugins, skills sync, keychain auth) so it does not fit
  `write_home_settings`'s single-purpose contract, and would
  contaminate the comparison by also turning off things the campaign wants
  left on (e.g. any hooks under test).

No settings.json key, documented or undocumented, was found that disables the
memory tool while leaving everything else on.

## (c) Version

`2.1.226 (Claude Code)`.

## Decision: sweep-between-sessions, not disable

Since there is no settings-based disable mechanism, `write_home_settings`
writes `{}` for both the "with memory" and "without memory" arms. It originally
took a `memory_enabled` boolean for interface symmetry with those two arms, but
the flag changed nothing about what got written and was never read, so it has
since been removed rather than left advertising a lever this function does not
have. It is therefore a no-op, and **`memory_campaign.py` no longer
calls it at all**. The function is kept here because it documents this finding
and is covered by tests, not because the runner needs it.

Two consequences the runner acts on:

1. **Never write settings after `contexer install`.** In the `with` arm,
   `_condition_b_setup` runs the real installer, which writes five hook events
   into `<home>/.claude/settings.json`. The earlier code then called
   `write_home_settings(home, memory_enabled=False)`, which wrote `{}` over
   that exact path and deleted every hook. The `with` arm was measuring
   Contexer with its entire deterministic mechanism switched off. Any future
   settings write in that arm must happen **before** the installer, never
   after, and `tests/test_bench_memory_campaign.py::test_with_arm_setup_keeps_contexer_hooks`
   fails if that ordering is broken again.

2. **The `with` arm sweeps memory artifacts between sessions.** Because memory
   is default-on and cannot be turned off, the `with` HOME has both systems
   live. The explicit-tier teaching prompts literally say "Remember this for
   future sessions", so memory files are a certainty, not a risk: they would
   land in the post-teaching snapshot, be restored before every measured
   session, and (worse) be imported into the Contexer store by Contexer's own
   `SessionStart` -> `sync_memory` hook, feeding the `with` arm with the
   opponent's captures. So after every `with`-arm session (teaching and
   measured alike) the harness counts the memory files into the row's
   `memory_leak_files` field and then deletes the memory directory, before the
   next session and before the snapshot is taken. Within-session writes cannot
   help the session that made them; deletion kills the two vectors that matter
   (cross-session leakage and `sync_memory` ingestion).

With the sweep in place, `contaminated` becomes a genuine tripwire again
rather than a field that is always true: for `with` rows it is measured
**before** the session runs, so it flags only memory files that survived a
sweep or the snapshot. For `memory` rows it stays a post-run check for a
Contexer store appearing where Contexer was never installed. `validate.py`'s
`_check_memory_isolation` fails the campaign on any contaminated measured row.

**Residual asymmetry, to be disclosed in any published claim:** the `with` arm
could not disable the opponent's mechanism, only delete its artifacts between
sessions. This is honesty over pretense: a fabricated "disabled" setting that
disabled nothing would silently corrupt the comparison instead of flagging it,
and `memory_leak_files` keeps the size of what was swept on the record instead
of hiding it.
