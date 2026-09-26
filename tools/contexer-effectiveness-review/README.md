# Contexer effectiveness review

An opt-in developer experiment for understanding when Contexer helps a coding agent, when reading the code is enough, and which features create noise. After eligible commit or PR-command activity, it asks the working agent for an evidence-based review and saves it locally. This adds a review turn and model usage. It is not enabled by `contexer install`.

## Install

Requires macOS or Linux, Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and a host that supports stop hooks: Claude Code, Cursor, or Codex. Python 3.12+ is required; the commands below use 3.13. Install Contexer separately using the [installation guide](../../docs/install.md). GitHub CLI (`gh auth login`) is optional for PR and CI status collection.

From a new checkout:

```bash
git clone --branch codex/effectiveness-review https://github.com/bhargavamin/contexer.git contexer-review
cd contexer-review
uv run --no-project --python 3.13 python tools/contexer-effectiveness-review/scripts/install.py --host codex --dry-run
uv run --no-project --python 3.13 python tools/contexer-effectiveness-review/scripts/install.py --host codex
```

Replace `codex` with `claude` or `cursor`, repeat `--host` to select several, or use `--host all`. The installer copies the skill and its tests to `~/.agents/skills/contexer-effectiveness-review/`. It registers a **user-wide** hook in the selected host, so it applies across that user's repositories. It preserves unrelated settings, backs up replaced files, and can be run again to update an installation. Malformed or symlinked configuration is refused without overwriting it.

| Host | Configuration changed | Activate and verify |
|---|---|---|
| Codex | `~/.codex/hooks.json` | Restart. In Codex CLI, review and trust the hook via `/hooks`; changed hooks need review again. Check your desktop version's hook support if using the app. |
| Claude Code | `~/.claude/settings.json` | Restart and inspect the Stop entry via `/hooks`. |
| Cursor | `~/.cursor/hooks.json` | Restart Cursor and verify the stop hook in its hook settings/logs. |

Do not add duplicate registrations in project settings or other host configuration files. The installer pins the Python executable it used; reinstall if that interpreter is moved or removed. Host documentation: [Codex](https://learn.chatgpt.com/docs/hooks), [Claude Code](https://code.claude.com/docs/en/hooks), [Cursor](https://prod.cursor.com/docs/hooks). Host versions and policies can disable execution even with valid configuration.

## Test before collecting data

Run the isolated suite from the checkout. It creates temporary repositories and usage directories, and covers the hook → review validation → outcome collection → report flow for all three host formats. GitHub responses in these tests are simulated.

```bash
uv run --no-project --python 3.13 --with pytest pytest -c tools/contexer-effectiveness-review/pytest.ini tools/contexer-effectiveness-review/tests -q --tb=short
uvx ruff@0.15.4 check tools/contexer-effectiveness-review
```

Exercise the installer without changing your own settings:

```bash
REVIEW_TEST_HOME="$(mktemp -d)"
uv run --no-project --python 3.13 python tools/contexer-effectiveness-review/scripts/install.py --home "$REVIEW_TEST_HOME" --host all
uv run --no-project --python 3.13 python tools/contexer-effectiveness-review/scripts/install.py --home "$REVIEW_TEST_HOME" --host all --uninstall
```

Then test **actual host delivery** in a disposable repository:

1. Activate the hook in your selected host. Open a scratch Git repository with an initial commit, with Contexer enabled for the session.
2. Ask the agent to make a small change, test it, and commit it. Let the turn finish normally.
3. Expect one “Contexer effectiveness review due” follow-up. The agent reads the installed skill, writes its assessment, validates it, and logs it. The review continuation should not repeatedly trigger itself.
4. Verify that a new record appears under `~/.contexer-usage/records/` and run the report below. A neutral result is a successful collection test; usefulness is not required.
5. Repeat for each host/version you intend to use. Automated format tests do not establish that every host build delivers hooks or complete transcripts.

For a manual review, ask the agent to follow the installed `SKILL.md`; it documents how to create a review token using a session id, transcript path and repository path. Analyze manual records separately from automatically sampled activity. Do not invent a helpful judgment just to pass validation.

## Collect outcomes and read the report

```bash
SKILL_DIR="$HOME/.agents/skills/contexer-effectiveness-review"
uv run --no-project --python 3.13 python "$SKILL_DIR/scripts/outcomes.py"
uv run --no-project --python 3.13 python "$SKILL_DIR/scripts/summarize.py"
uv run --no-project --python 3.13 python "$SKILL_DIR/scripts/summarize.py" --host codex
```

Both reporting scripts accept `--repo-key KEY`; the report also accepts `--host HOST`. Read keys from record filenames. Run outcome collection again after CI finishes or a PR merges: it appends changed statuses and retries previously absent PRs. It may fetch Git remote history and query GitHub through `gh`; it never publishes review records. There is no background collection schedule.

## What is recorded

A review record describes a segment since the preceding review, not necessarily a whole task or developer session. Several commits can belong to one record, and one session can produce several records. Automatic triggers use attributed Git activity or visible PR-creation command intent; an attempted command does not prove success. Manual records carry their own kind. Repository selection follows the host's repository/workspace paths; the launch directory is only a fallback when those paths yield no repository.

| Evidence | Examples | What it supports |
|---|---|---|
| Observations | Commit ids and change summaries; visible tool counts; surfaced decision ids; host, model when supplied, observer version, coverage flags | What the observer could see, subject to attribution and transcript limits |
| Agent judgment | Deciding facts and sources; relevance of each decision; feature ratings; gaps; proposed improvements; hypothetical result without Contexer | An explanation and a research hypothesis, not an independent outcome or measured saving |
| Later outcomes | PR state, available CI checks, detected Git reverts | Corroborating repository status, not proof the task was correct or Contexer caused success |

See [schema.md](schema.md) for fields and a synthetic example. `helpful` requires a previously stored decision that changed a concrete step. `decisive` also requires a deciding fact linked to that decision. A decision captured in the same session cannot earn credit for helping that session. Surfaced context that changed nothing is redundant or irrelevant. Rate noise, stale context, retrieval gaps and cases where code reading did better.

## Measuring value responsibly

Start with descriptive questions: Which kinds of work benefit? Which knowledge is hard to reconstruct from code? Which features waste attention? Which mistakes recur? Keep the sample size and denominator beside every share.

- **Credited review share:** `(helpful + decisive reviews) / usable reviews` in a defined cohort. This is self-reported usefulness. Include neutral, harmful and not-used records in that denominator.
- **Decision usefulness:** `(helpful + decisive items) / rated items`, excluding same-session captures. Items are repeated exposures, not necessarily distinct stored decisions.
- **Hard-to-reconstruct knowledge:** Contexer-sourced key facts marked `code_would_reveal=no` divided by all Contexer-sourced key facts. The field includes whether docs or Git history could have supplied the fact. This remains the agent's assessment.
- **Feature burden:** noise or harmful ratings divided by all ratings of that feature. Compare with useful ratings, gaps and evidence-backed improvement ideas. Missing ratings are not approval.
- **Outcome association:** compare credited and non-credited records with known outcome data. Show missing PR, CI and revert statuses separately. A merge or passing CI is not a correctness score; no detected revert is not proof of no regression.

The report provides verdict distributions, task breakdowns, fact sources, relevance, feature ratings, improvement ideas, visible tool counts and outcome summaries. It ranks groups only with at least five records and warns below twenty total records; neither threshold establishes statistical significance. It does not calculate causal uplift, time saved, token savings, dollars saved or confidence intervals.

Before combining data, define cohorts by task category/difficulty, question type, knowledge location, scope, repository, host, model and observer/schema version. The built-in report only filters by host and repository; finer cohort analysis requires processing the JSONL separately. Repository keys are based on local identity: the same project can have different keys on different machines. Agree on a de-identified project mapping before multi-developer aggregation, and avoid treating multiple segments from one task as independent experiments.

A defensible causal study needs an additional controlled comparison: fix the repository snapshot and task, hold model/tools/permissions constant, compare Contexer on/off and an appropriate documentation baseline, randomize order, repeat trials, and score task correctness independently of the working agent. Measure elapsed time and tokens separately, include review overhead, and prevent context leakage between runs. Report failures and uncertainty, accounting for repeated tasks and repositories. Use this field dataset to select representative tasks and hypotheses; use the controlled experiment to test them.

## Coverage and privacy

Unknown values are `null`, never zero. Missing transcripts and opaque Codex wrappers can hide calls and results; directly observed counts are only lower bounds. Cursor records do not expose the result and injection evidence needed for exhaustive id verification. Check completeness flags and `ids_verified` before comparing cohorts. Literal commands inside known wrappers show intent only. Compaction can replace transcripts and replay old evidence; concurrent agents weaken commit attribution. Exclude or separately report unverified attribution for comparisons.

Automatic sampling favors work that reaches commit/PR activity. Abandoned work, non-commit investigations, missing hook delivery, failed reviews and manual selection bias are not fully measured. The dataset is not a denominator for all developer work. Keep an external enrollment/completion log if estimating collection coverage.

Data stays under `~/.contexer-usage/`: records, outcomes, session state, pending observations and error logs. Set `CONTEXER_USAGE_HOME` consistently for the hook and reporting processes to isolate an experiment. The installer does not persist this environment variable. Pending observations expire after seven days during later hook activity; final records/outcomes have no automatic retention policy.

Raw transcripts are read locally but not copied into records. Tool argument values are omitted. Metadata can still include repository paths, remotes, commit subjects, session identifiers and short agent-written evidence. Known credential formats are scrubbed or rejected; that is not comprehensive de-identification. Do not paste secrets, decision bodies or customer data into judgments, and inspect exports before sharing. This tool has no automatic upload. Obtain each participant's consent and agree on retention and redaction before collecting team data.

## Troubleshooting and removal

If no review appears, check host hook execution/trust, whether the turn completed normally, whether a new commit or PR command was visible, and `~/.contexer-usage/hook-errors.log`. Missing transcripts reduce coverage; they do not mean zero use. Aborted turns and hook continuations defer eligible evidence until the next ordinary stop. A validation error lists the fields to correct; never edit a pending observation to force acceptance. An expired pending review can be replaced with a manual review, labeled accordingly.

Remove registrations for selected hosts while retaining the skill, configuration backups and collected data:

```bash
SKILL_DIR="$HOME/.agents/skills/contexer-effectiveness-review"
uv run --no-project --python 3.13 python "$SKILL_DIR/scripts/install.py" --host all --uninstall
```

Restart the hosts after removal. Decide separately whether to archive or delete retained experiment data and backups. To upgrade, check out the desired reviewed revision and rerun installation, then repeat the actual-host smoke test and renew host trust where required.
