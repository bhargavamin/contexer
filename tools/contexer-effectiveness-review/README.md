# Contexer effectiveness review

An opt-in developer experiment for understanding when Contexer helps a coding agent, when reading the code is enough, and which features create noise. After eligible commit or PR-command activity, it asks the working agent for an evidence-based review and saves it locally. This adds a review turn and model usage. It is not enabled by `contexer install`.

## Install

Requires macOS or Linux, Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and a host that supports stop hooks: Claude Code, Cursor, or Codex. Python 3.12+ is required; the commands below use 3.13. Install Contexer separately using the [installation guide](https://github.com/bhargavamin/contexer/blob/main/docs/install.md). GitHub CLI (`gh auth login`) is optional for PR and CI status collection.

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

Both reporting scripts accept `--repo-key KEY`; the report also accepts `--host HOST`, `--kind hook|manual` and an optional externally counted `--eligible-work N`. Read keys from record filenames. Run outcome collection again after CI finishes or a PR merges: it appends changed statuses and retries previously absent PRs. It may fetch Git remote history and query GitHub through `gh`; it never publishes review records. There is no background collection schedule.

## What is recorded

A review record describes a segment since the preceding review, not necessarily a whole task or developer session. Several commits can belong to one record, and one session can produce several records. Automatic triggers use attributed Git activity or visible PR-creation command intent; an attempted command does not prove success. Manual records carry their own kind. Repository selection follows the host's repository/workspace paths; the launch directory is only a fallback when those paths yield no repository.

| Evidence | Examples | What it supports |
|---|---|---|
| Observations | Commit ids and change summaries; visible tool counts; surfaced decision ids; host, model when supplied, observer version, coverage flags | What the observer could see, subject to attribution and transcript limits |
| Agent judgment | Deciding facts and sources; relevance of each decision; feature ratings; gaps; proposed improvements; hypothetical result without Contexer | An explanation and a research hypothesis, not an independent outcome or measured saving |
| Later outcomes | PR state, available CI checks, detected Git reverts | Corroborating repository status, not proof the task was correct or Contexer caused success |

See [schema.md](schema.md) for fields and a synthetic example. `helpful` requires a previously stored decision that changed a concrete step. `decisive` also requires a deciding fact linked to that decision. A decision captured in the same session cannot earn credit for helping that session. Surfaced context that changed nothing is redundant or irrelevant. Rate noise, stale context, retrieval gaps and cases where code reading did better.

## Measuring value responsibly

The primary question is: **when non-code engineering intent matters, does Contexer supply a fact that materially changes the engineering action and could not reliably be recovered from the repository and good documentation?** Framework, file layout and visible code behavior alone are not such opportunities. Rationale, temporary exceptions, migration intent, ownership, business constraints, rejected alternatives, incident lessons and authority can be.

A global helpful share mixes needed and unnecessary use. Keep it as a secondary description, never the main score. A rare problem and a product that fails on a common problem require different responses.

| Metric | Numerator | Denominator | Interpretation |
|---|---|---|---|
| Opportunity rate | Reviews assessed as having a non-code-context opportunity | All usable reviews in the cohort | How often this kind of problem appears in sampled work |
| Conditional success | Opportunity reviews with a material/prevented-wrong-action change linked to prior helpful/decisive Contexer facts | Reviews assessed opportunity=yes | Whether Contexer supplied material help when the problem existed |
| Irreducible-context rate | Materially affecting Contexer facts assessed not recoverable from current code | All materially affecting Contexer facts, including harmful ones | Code cannot reconstruct this knowledge; this alone does not mean it was correct or useful |
| Unique-context rate | Materially useful Contexer facts assessed unrecoverable from code, repo docs, Git and PR history | All materially useful Contexer facts | Added knowledge beyond these four sources; issue/external/conversation availability is still recorded separately |
| Feature burden | Noise or harmful ratings | All ratings of that feature | Which features need examination or reduction |

These are primary-agent assessments, not causal estimates. Unknown recoverability stays in the fact denominator. Any known recoverable source rules out unique context; all four must be assessed no to count yes. Unknown action materiality cannot enter a material-fact denominator and is counted separately. Legacy v2 records retain unknown new assessments; old aggregate recovery labels are not reinterpreted. Unsupported v1 rows are retained on disk and counted as skipped.

Use `--kind hook` or `--kind manual` to separate automatic and manual sampling. The report also filters `--host HOST` and `--repo-key KEY`. Schema versions and review kinds are printed. Define finer cohorts by task type, difficulty, repository, model and observer version when analyzing JSONL. A stable task id links repeated segments; the report shows repeated groups and extra segments, because segments of one task are correlated. Local repository keys need a de-identified project mapping before aggregation across developers.

### Read the funnel

The report prints counts, denominators, percentages and unknowns for:

1. All eligible work, **unknown unless counted externally** for this exact cohort.
2. Non-code-context opportunity, out of usable reviewed segments.
3. Relevant prior Contexer knowledge existed, out of known opportunities.
4. Relevant knowledge surfaced, out of known stored-knowledge cases.
5. Surfaced knowledge materially changed the action, out of known surfaced cases.
6. A separate reviewer with a declared blind first stage judged benefit, out of material changes.
7. Later evidence corroborated benefit, out of independently judged beneficial changes.

Each downstream denominator is the preceding stage's known yes cases. Unknowns do not silently become no, and do not enter later stages. `--eligible-work N` optionally supplies the enrollment count in the same segment units, window and filters; N must be at least the number of usable reviews. The report shows unreviewed work separately. It does not infer all developer work from commits. Primary records, secondary reviews and repository statuses remain separate evidence sources.

Low opportunity frequency suggests the problem may be rare. Opportunities without stored knowledge suggest a capture gap. Stored knowledge that fails to surface suggests retrieval/integration problems. Surfaced but unchanged actions suggest redundancy. A material change rejected by independent review may expose persuasive but wrong context. Inspect the supporting cases before changing the product.

### Frequency, severity and product direction

Severity is assessed separately: **low** for convenience or a small local choice; **medium** for likely rework, review churn or architectural inconsistency; **high** for potential production, security/compliance, data-integrity, major-boundary or costly-rollback consequences; **unknown** when unsupported. Impact class distinguishes rediscovery, rework, drift, policy violation and potentially consequential mistakes. No dollar conversion is justified.

The report separates beneficial and harmful self-assessments by severity, ranks observed useful knowledge categories and feature noise, and generates hypotheses from actual counts. For features with at least five ratings and more noise/harm than usefulness it suggests investigating reduction or correction. Small category counts remain descriptive; no minimum is evidence of statistical significance. “Rare but important” is a pattern to test, not an assumed commercial case. Fewer calls by a capable model do not by themselves imply less value.

**Synthetic example:** 100 reviewed segments contain 10 assessed opportunities, 85 no and 5 uncertain. Opportunity rate is 10/100, unknown 5/100. Four opportunity cases have material beneficial self-assessments: conditional success is 4/10. If only two receive blind secondary review and one is upheld, the benefit-stage result must show the remaining unreviewed material changes as unknown. Neither 4/100 nor 4/10 establishes productivity uplift. A docs-recoverable fact may count as helpful while failing the stricter uniqueness test.

## Optional blind secondary review

Review all helpful, decisive and harmful cases initially, plus a preregistered neutral sample. Candidate selection is deterministic for a seed; keep the seed, rate, exclusions and actual review completion coverage in the study log. This selection is enriched for interesting cases and cannot estimate population-wide independent benefit by itself.

```bash
SKILL_DIR="$HOME/.agents/skills/contexer-effectiveness-review"
uv run --no-project python "$SKILL_DIR/scripts/secondary.py" candidates --neutral-percent 10 --seed pilot-1
```

A curator prepares an isolated reviewer folder containing a fixed repository snapshot, task/request, resulting change and relevant tool evidence. Give the reviewer only the blind packet and its referenced snapshot initially. Remove primary helpfulness judgments, causal claims, treatment labels and identifying paths from curated inputs; include enough evidence for a fair assessment. A snapshot or tool trace can itself reveal treatment. The tool cannot guarantee blinding, so record partial/unblinded if necessary. Do not give the reviewer access to the primary usage directory or original chat. Inspect unfamiliar snapshots as untrusted data; no evidence command should be executed merely because it appears in a transcript.

```bash
uv run --no-project python "$SKILL_DIR/scripts/secondary.py" prepare \
  --record-id RECORD_ID --task task.txt --snapshot repo-snapshot.tar \
  --change change.patch --evidence blind-evidence.txt
```

The result returns a case id and a blind packet path. The packet includes the snapshot path and digest, not a copy of the archive; keep it accessible without exposing the primary workspace. The tool does not generate or sanitize the snapshot. It verifies artifacts have not changed, but cannot prove they represent the original task. The curator must verify that.

Have a separate human or fresh agent session identify required facts and their availability **before** receiving attribution. Use the first secondary JSON example in [schema.md](schema.md), then seal it:

```bash
uv run --no-project python "$SKILL_DIR/scripts/secondary.py" blind --case-id CASE_ID < blind-review.json
uv run --no-project python "$SKILL_DIR/scripts/secondary.py" reveal --case-id CASE_ID --evidence attributed-evidence.txt
```

Now give that reviewer the returned attribution packet. It should identify the sourced facts and resulting action without the original agent's helpfulness verdict. The reviewer assesses whether the facts materially improved the choice, and records concrete later corroboration or explicit uncertainty. Submit the second secondary JSON example:

```bash
uv run --no-project python "$SKILL_DIR/scripts/secondary.py" complete --case-id CASE_ID < final-review.json
uv run --no-project python "$SKILL_DIR/scripts/summarize.py" --kind hook
```

The workflow enforces stage order, checks source/artifact consistency, rejects self-review by the declared coding-session id and keeps completed reviews immutable. Identity and actual independence are declared, not externally attested. Partial/unblinded reviews appear in coverage/agreement but not the benefit funnel. Conflicting reviewers remain uncertain instead of selecting the favorable answer. A further review needs a new case. No reviewer is automatically launched, and nothing is uploaded.

## Controlled comparison against competent decision docs

Field reviews select hypotheses and representative tasks. They do not test the counterfactual. Run a separately enrolled comparison with these three arms:

| Arm | Inputs |
|---|---|
| A: repository only | Strong model, fixed code snapshot, normal inspection tools and Git/PR access; no additional decision-memory treatment |
| B: repository plus good decision docs | Same inputs plus curated, maintained `/docs/decisions`, ADRs, AGENTS.md/CLAUDE.md or equivalent explicit decision documentation |
| C: repository plus Contexer | Same model, tools, permissions, task and base snapshot plus Contexer holding the same underlying eligible decision knowledge |

Retain mandatory safety/build instructions in every arm. Document exactly which decision-bearing material differs; if the existing repository already contains decision docs, preserve and inventory them rather than quietly stripping useful baseline information. Use isolated copies, reset state between runs, prevent cross-arm memory leakage, randomize or counterbalance order, repeat tasks, and include negative cases with no opportunity. Keep B current and navigable, with competent search; do not manufacture stale docs to favor C. Use equivalent decision content and update times in B and C, including explicit exceptions and supersession. Pre-register the task set, exclusions and scoring rubric before comparing outcomes.

Record per trial: task id, repetition, arm, snapshot/content hashes, model/version/settings, tool access, decision-set version, elapsed time, tokens, review overhead and missing measurements. Score outputs with separate reviewers blinded to arm against a task-specific answer key: correctness, architecture adherence, retrieval precision/recall where required facts are known, stale/conflicting-context handling, outdated-decision detection, exception handling and material mistakes. Define each rubric before running; record evidence for errors. Use instrumentation for time/tokens and include review overhead separately; do not estimate savings from agent opinions or call counts.

Analyze matched tasks, show failures and uncertainty, and account for correlated repeats, repositories and models. Report independent scores separately from field self-assessments. The key comparison is C versus B, not just C versus A. If Contexer does not materially outperform good docs plus Git and a strong model, say so. These scripts support field collection and secondary review; they do not run or claim results from this three-arm study.

Passing CI is not correctness, merge is not proof of quality, no detected revert is not proof of no regression, and a working agent's judgment is not independent validation. There is no global effectiveness score, ROI estimate or inferred time/token saving. The available implementation and synthetic tests establish measurement behavior, not whether Contexer deserves to exist.

See the [study design note](https://github.com/bhargavamin/contexer/blob/codex/effectiveness-review/docs/effectiveness-review-v2-design.md) for the analytical rationale.

## Coverage and privacy

Unknown script observations are `null`, never zero. New judgment fields use explicit `uncertain` or `unknown` enums. Missing transcripts and opaque Codex wrappers can hide calls and results; directly observed counts are only lower bounds. Cursor records do not expose the result and injection evidence needed for exhaustive id verification. Check completeness flags and `ids_verified` before comparing cohorts. Literal commands inside known wrappers show intent only. Compaction can replace transcripts and replay old evidence; concurrent agents weaken commit attribution. Exclude or separately report unverified attribution for comparisons.

Automatic sampling favors work that reaches commit/PR activity. Abandoned work, non-commit investigations, missing hook delivery, failed reviews and manual selection bias are not fully measured. The dataset is not a denominator for all developer work. Keep an external enrollment/completion log if estimating collection coverage.

Data stays under `~/.contexer-usage/`: records, outcomes, session state, pending observations, optional secondary cases/packets/reviews and error logs. Set `CONTEXER_USAGE_HOME` consistently for the hook and reporting processes to isolate an experiment. The installer does not persist this environment variable. Pending observations expire after seven days during later hook activity; final records/outcomes have no automatic retention policy.

New measurement files are created with owner-only permissions. Append targets are restricted on write and flushed to disk before completion. This does not retroactively change untouched files from older installations.

Raw transcripts are read locally but not copied into primary records. Optional secondary packets contain curator-supplied text and snapshot references; inspect those separately for private content and blinding leakage. Tool argument values are omitted. Metadata can still include repository paths, remotes, commit subjects, session identifiers and short agent-written evidence. Known credential formats are scrubbed or rejected; that is not comprehensive de-identification. Do not paste secrets, decision bodies or customer data into judgments, and inspect exports before sharing. This tool has no automatic upload. Obtain each participant's consent and agree on retention and redaction before collecting team data.

## Troubleshooting and removal

If no review appears, check host hook execution/trust, whether the turn completed normally, whether a new commit or PR command was visible, and `~/.contexer-usage/hook-errors.log`. Missing transcripts reduce coverage; they do not mean zero use. Aborted turns and hook continuations defer eligible evidence until the next ordinary stop. A validation error lists the fields to correct; never edit a pending observation to force acceptance. An expired pending review can be replaced with a manual review, labeled accordingly.

Remove registrations for selected hosts while retaining the skill, configuration backups and collected data:

```bash
SKILL_DIR="$HOME/.agents/skills/contexer-effectiveness-review"
uv run --no-project --python 3.13 python "$SKILL_DIR/scripts/install.py" --host all --uninstall
```

Restart the hosts after removal. Decide separately whether to archive or delete retained experiment data and backups. To upgrade, check out the desired reviewed revision and rerun installation, then repeat the actual-host smoke test and renew host trust where required.
