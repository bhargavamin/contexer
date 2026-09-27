# Effectiveness review: opportunity, material intervention and independent evidence

This is the second study design, represented by judgment schema `contexer-effectiveness/v3`.
The schema version differs because the existing experiment already used v2 records.
For installation, collection and staged-review commands, see the
[developer guide](../tools/contexer-effectiveness-review/README.md).

## Why change the primary question?

“How often did Contexer help?” mixes work needing non-code intent with work a capable model
can solve by reading the implementation. A low global helpful share cannot distinguish a rare
problem, poor capture, failed retrieval, redundant knowledge or harmful influence. Conversely,
a high share could reflect convenient repetition of facts already documented in the repo.

The primary question is whether Contexer supplies engineering knowledge that materially changes
an action when non-code intent matters, beyond what a capable agent can recover from current
code and good decision documentation. The experiment must support negative conclusions and
feature removal as readily as positive conclusions.

## Opportunity and conditional success

A non-code-context opportunity exists when a correct choice plausibly depends on knowledge
not safely inferred from current code alone: rationale, a temporary exception, migration intent,
ownership, business constraints, rejected alternatives, incident lessons, authority or lifecycle.
Framework choice, file layout, dependencies and observable behavior are ordinary code facts.
Docs-recoverable rationale can be an opportunity without being unique Contexer knowledge.

Opportunity rate is assessed yes opportunities / all usable reviews. Conditional success is
opportunity cases with a material or prevented-wrong-action change linked to previously stored,
helpful/decisive Contexer facts / all assessed yes opportunities. Convenience, calls and new
captures do not qualify as material success. A helpful descriptive verdict can therefore
coexist with a minor action change and fail the conditional-success criterion.

Both are self-assessed frequencies. Unknown opportunity assessments remain in the first
denominator, visible as unknown; downstream analysis conditions only on known yes. Missing
required fields are invalid, while explicit uncertainty is valid data. Legacy records are
retained without guessing new labels. Repeated segments use a stable task identifier and
are reported as correlated observations, not independent trials.

## Recoverability and irreducible context

Each deciding fact records its actual source, knowledge category, Contexer-attributed action
change, plausible alternative sources and separate reliable-recovery judgments for code,
repo docs, Git, PR and external docs. An expanded recovery label distinguishes code proof
from code suggestion, docs/history, external information and organizational-only knowledge.

Irreducible-context rate is facts assessed not recoverable from current code / all materially
affecting Contexer facts, including harmful influence. Irreducible does not mean true or useful.
Unique-context rate is useful material facts assessed not recoverable from code, docs, Git or
PR history / all materially useful Contexer facts. One known recoverable source rules out
uniqueness; all four must be no to count yes. Uncertain recoverability remains visible within
these denominators. Facts with unknown materiality cannot enter them and are counted separately.
These are fact exposures within records, not counts of unique stored decisions.

Category counts guide hypotheses about capture priorities. A concentration in exceptions or
migration intent would justify testing focused capture, not declaring generic memory valuable.
Noise/harmful feature ratings suggest correction or reduction. The report derives these
hypotheses from actual records; no example percentages or preferred conclusion are embedded.

## Funnel and failure location

| Stage | Denominator | What a gap may mean |
|---|---|---|
| All eligible work | Externally enrolled segments | Collection coverage; unknown without an enrollment log |
| Non-code opportunity | All usable reviews | The problem may be rare |
| Relevant stored knowledge | Known opportunity=yes cases | Capture may be insufficient |
| Knowledge surfaced | Known stored-knowledge=yes cases | Retrieval/integration may fail |
| Material action change | Known surfaced=yes cases | Knowledge may be redundant |
| Blind secondary benefit judgment | Materially changed cases | Context may persuade without improving the decision |
| Later corroboration | Cases independently judged beneficial | Evidence may still be immature or contradict the claim |

Every row prints count, denominator, percentage and unknown/missing count. The opportunity
row uses reviewed work, not the externally enrolled total; unreviewed work is shown separately.
Unknown earlier stages never silently become no or flow into later denominators. Stored
knowledge and action change remain primary assessments. CI, merge and revert observations
never fill the independent-benefit stages automatically.

## Blind secondary review

Start with helpful, decisive and harmful records plus a deterministic, preregistered neutral
sample. Report selection and completion coverage; this enriched sample is not representative
of all work. A curator supplies a fixed snapshot, task, change and relevant evidence without
the primary verdict or causal claim, ideally without treatment identity. Snapshot authenticity
and leakage inspection are curator responsibilities.

A separate human or fresh agent first identifies required facts, where they were available
and whether the current repository could supply them. Seal that assessment before revealing
attributed tool evidence. Then assess material benefit and later corroboration independently.
Completed reviews remain immutable. Conflicting judgments stay uncertain. Declared partial or
unblinded reviews remain visible but do not qualify for the blind-benefit funnel.

The workflow checks stage order and artifact consistency and rejects an identical declared
coding/reviewer session id. It does not attest reviewer identity, prevent all information
leakage or prove actual independence. Those limitations belong beside the results. No automatic
reviewer is launched; the operating procedure and schema support an explicitly arranged review.

## Controlled baseline

The real competitor is good decision docs plus Git and a strong model. Run isolated matched
trials with A: repository only, B: repository plus maintained ADRs/decision docs/AGENTS/CLAUDE,
and C: repository plus Contexer. Keep model, task, base state, tools and permissions constant.
Preserve mandatory safety instructions and inventory existing decision docs in all arms.
Provide equivalent eligible decision content in B and C; do not weaken or stale the Markdown
baseline. Include exceptions, supersession, conflicts, outdated decisions and no-opportunity tasks.

Pre-register task-specific answer keys and scoring criteria. Randomize/counterbalance order,
repeat trials without cross-run memory leakage, and use separate scorers blinded to arm.
Compare correctness, architecture adherence, retrieval accuracy, stale/conflict handling,
exception handling, outdated-decision identification and material mistakes. Instrument elapsed
time, tokens and review overhead separately; missing instrumentation is not zero. Analyze
matched tasks and correlated repeats, and report uncertainty and failures.

C versus B is the decisive product comparison. If Contexer does not materially outperform
competent docs plus Git, report that result plainly. The field collector and secondary-review
workflow do not execute this controlled study or establish its results.

## Rare but high-impact interventions

Separate opportunity frequency, assessed severity and intervention class. Low severity covers
convenience and small local choices; medium covers likely rework/churn/inconsistency; high
covers potential production, security/compliance, data integrity, major architecture or costly
rollback consequences. Unsupported severity stays unknown. Show harmful as well as beneficial
material changes. A rare/high-impact pattern can be studied without assuming commercial value
or converting assessments to dollars. Lower call volume is not itself a negative outcome.

## Limits and what remains unanswered

Self-reported usefulness is not causal proof. Passing CI is not correctness; merge is not
quality; absence of a detected revert is not absence of regression. Automatic commit/PR-command
sampling excludes some failed, abandoned and non-commit work. Host/transcript gaps, uncertain
attribution, missing reviews and repeated tasks affect coverage and inference. Local repository
keys require mapping across developers. Keep raw personal evidence private and review exports.

No global effectiveness score, ROI, productivity uplift or inferred time saving is produced.
The existing small, legacy dataset cannot distinguish rare opportunity from unnecessary product.
The new measurements make those hypotheses testable; only prospective data and a competent
controlled comparison can answer whether Contexer provides unique, useful engineering intent.
