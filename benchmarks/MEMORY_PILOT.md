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
  `write_home_settings(memory_enabled)`'s single-purpose contract, and would
  contaminate the comparison by also turning off things the campaign wants
  left on (e.g. any hooks under test).

No settings.json key, documented or undocumented, was found that disables the
memory tool while leaving everything else on.

## (c) Version

`2.1.226 (Claude Code)`.

## Decision: detection-only fallback

Since there is no settings-based disable mechanism, `write_home_settings`
writes `{}` in both the `memory_enabled=True` and `memory_enabled=False`
cases (the boolean is accepted for interface symmetry with the benchmark
harness's "with memory" / "without memory" arms, but it does not change what
gets written). The "without memory" arm of the campaign cannot actually
prevent the tool from writing memory files. Instead, the benchmark's post-run
validator must detect memory files under the run's throwaway HOME
(`memory_files(home, repo)` non-empty) and mark that row `contaminated=true`:
excluded from aggregate scoring, counted and reported separately. This is
honesty over pretense: a fabricated "disabled" setting that does not disable
anything would silently corrupt the comparison instead of flagging it.
