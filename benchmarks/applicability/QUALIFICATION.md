# Contract 08 qualification preparation

`qualification_preparation.py` is an offline benchmark tool. It does not call a model,
network service, host launcher, repository setup command, or production hook. Its artifacts
do not approve a campaign or satisfy timing, isolation, budget, or host-receipt gates.

The manifest and every referenced file live below one operator-selected directory. Source
material first enters that directory through the bounded byte-copy command:

```bash
uv run python benchmarks/applicability/qualification_preparation.py import-snapshot \
  --root /explicitly/authorized/source \
  --file path/to/task.txt \
  --file path/to/snapshot.json \
  --output /local/contract08/snapshot-revision-1 \
  --grant-id local-evaluation-grant \
  --consent-record "User designated these files for local evaluation"
```

The structural workflow is:

```bash
uv run python benchmarks/applicability/qualification_preparation.py validate \
  --manifest /local/contract08/qualification-manifest.json
uv run python benchmarks/applicability/qualification_preparation.py review-packet \
  --manifest /local/contract08/qualification-manifest.json \
  --output /local/contract08/review-packet.json
uv run python benchmarks/applicability/qualification_preparation.py review-import \
  --manifest /local/contract08/qualification-manifest.json \
  --attestation /operator/input/review-attestation.json \
  --output /local/contract08/review-attestation.json \
  --manifest-output /local/contract08/qualification-manifest-reviewed.json
uv run python benchmarks/applicability/qualification_preparation.py report \
  --manifest /local/contract08/qualification-manifest.json --format text
```

`review-import` validates an externally supplied record and copies it unchanged. It cannot
create a reviewer identity or adjudication. It writes a new immutable manifest revision with
the content reference, validates that revision, and only then appends the reviewed/frozen
event. Missing review remains `pending`.

Use `measure` on opened development fixtures while implementing the collector:

```bash
uv run python benchmarks/applicability/qualification_preparation.py measure \
  --manifest /local/contract08/development-manifest.json \
  --output /local/contract08/development-observations.json
```

Do not pass `--open-final` during Contract 08. A later operator uses it only after review,
campaign preparation, and final integration binding. The command durably appends opening
intent before retrieval. An interrupted opening is consumed diagnostic evidence and cannot
be relabeled unopened. The exposure log enforces the full transition history; any earlier
opening intent, consumption, or uncertain exposure permanently prevents resealing.

After the later measurement, produce the versioned Contract 07 artifact with:

```bash
uv run python benchmarks/applicability/qualification_preparation.py export \
  --manifest /local/contract08/qualification-manifest.json \
  --observations /local/contract08/qualification-observations.json \
  --output /local/contract08/qualification-evidence.json
```

Contract 07 campaign schema 2 binds this artifact to the exact pilot-core content reference,
candidate source, campaign, and execution source-chain digest. That digest covers the entire
executed `contexer` Python package, including conflict and revision rendering, plus the
collector, formatter, relevance benchmark, and dependency-lock bytes used by collection.
Schema 1 evidence remains diagnostic and cannot pass readiness or authorize a launch. Adapter
emission is recorded separately from host receipt; this offline tool always reports host
receipt as unavailable.
