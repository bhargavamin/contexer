# MCP tool description compatibility

Claude Code 2.1.296's default mode truncates MCP tool descriptions after 4,096
characters and appends a truncation marker. `update_context` keeps its conditional
pending-review and correction-refusal instructions near the beginning and stays
at or below 3,800 served characters, leaving room for maintenance. Capture,
revision, approval, anchoring, and applicability behavior are unchanged.

The limit applies to the description returned by MCP `tools/list`. Python 3.13+
normalizes docstring indentation differently from Python 3.12, so raw source or
`__doc__` length alone is insufficient. Run the served-contract regression tests:

```bash
uv run --frozen pytest tests/test_mcp_descriptions.py --no-cov -n0
```

These tests check all registered tool lengths, complete visible contract clauses,
and compatibility with the baseline schemas, defaults, metadata, other tool
descriptions, and server instructions. Negative controls modify the registered
description before serialization and verify that missing or hidden instructions
and oversized text fail the gates. Clause matching normalizes whitespace;
length checks do not. The boundary projection is regression coverage, not proof
of what a host sends. When changing this contract, also inspect a real host's
model request using a local API stand-in and isolated configuration.

## Claude Code `--bare`

Local request captures from Claude Code 2.1.296 show that `--bare` sends only the
first paragraph of each Contexer tool description and omits Contexer's server
instructions. For `update_context`, this is the unchanged 97-character opening
paragraph; pending-review, correction-refusal, anchoring, and applicability
guidance are absent. Shortening the full description does not resolve this mode.
Use default mode when the complete Contexer guidance is required. This observation
is version-specific; other host versions and SDK configurations need independent
verification. This change does not establish whether the host behavior is intended
or a regression. Exposing the full contract in `--bare` needs a separate host
compatibility investigation.

## Capture-instruction comparisons

For issue #390, rebuild both arms on the same corrected baseline. Keep the shipped
applicability paragraph in Arm A; apply only the intended applicability treatment
in Arm B. Refreeze source and host-visible description hashes, and verify both
complete descriptions are untruncated in the chosen host mode. Tool schemas,
other descriptions, server instructions, and model configuration must match.
The observed `--bare` mode cannot establish an exposed description treatment.
This compatibility requirement does not execute or modify the experiment.
