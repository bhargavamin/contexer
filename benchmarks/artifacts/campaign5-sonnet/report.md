# Contexer A/B Benchmark — model claude-sonnet-5, 18 runs (4 without / 4 agentsmd / 4 claudemd / 3 claudemd_agentsmd / 3 with)

| metric | without | agentsmd | claudemd | claudemd_agentsmd | with | Δ (with−without) | Δ% (with−without) | Δ (with−claudemd) | Δ% (with−claudemd) | Δ (agentsmd−claudemd) | Δ% (agentsmd−claudemd) | Δ (claudemd_agentsmd−claudemd) | Δ% (claudemd_agentsmd−claudemd) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| tokens_total | 167762.500 | 114647.500 | 65647.500 | 32445 | 32789 | -134973.500 | -80.5% | -32858.500 | -50.1% | 49000 | +74.6% | -33202.500 | -50.6% |
| cost_usd | 0.140 | 0.081 | 0.057 | 0.043 | 0.043 | -0.097 | -69.4% | -0.014 | -25.2% | 0.024 | +42.3% | -0.014 | -24.8% |
| turns | 6 | 4.500 | 2 | 1 | 1 | -5 | -83.3% | -1 | -50.0% | 2.500 | +125.0% | -1 | -50.0% |
| tool_calls | 10 | 6.500 | 2 | 0 | 0 | -10 | -100.0% | -2 | -100.0% | 4.500 | +225.0% | -2 | -100.0% |
| duration_ms | 28595.500 | 19502.500 | 10514 | 6094 | 3915 | -24680.500 | -86.3% | -6599 | -62.8% | 8988.500 | +85.5% | -4420 | -42.0% |
| violations | 0 | 0 | 0 | 0 | 0 | 0 | n/a | 0 | n/a | 0 | n/a | 0 | n/a |
| rationale | 0 | 1 | 1 | 1 | 1 | 1 | n/a | 0 | +0.0% | 0 | +0.0% | 0 | +0.0% |
| success | 1 | 1 | 1 | 1 | 1 | 0 | +0.0% | 0 | +0.0% | 0 | +0.0% | 0 | +0.0% |

_Note: rationale 0.0 can mean the information was unavailable to that condition, not model failure — see per-condition design._

## Independent Validation

**Status: PASS** — 0 failure(s), 29 warning(s)

### Warnings

- cell (cont-logging, agentsmd) has 1 of 3 rows (short)
- cell (cont-logging, claudemd) has 1 of 3 rows (short)
- cell (cont-logging, claudemd_agentsmd) has 1 of 3 rows (short)
- cell (cont-logging, with) has 1 of 3 rows (short)
- cell (cont-logging, without) has 1 of 3 rows (short)
- cell (rat-errors, agentsmd) has 1 of 3 rows (short)
- cell (rat-errors, claudemd) has 1 of 3 rows (short)
- cell (rat-errors, claudemd_agentsmd) has 1 of 3 rows (short)
- cell (rat-errors, with) has 1 of 3 rows (short)
- cell (rat-errors, without) has 1 of 3 rows (short)
- cell (rat-storage, agentsmd) has 1 of 3 rows (short)
- cell (rat-storage, claudemd) has 1 of 3 rows (short)
- cell (rat-storage, claudemd_agentsmd) has 1 of 3 rows (short)
- cell (rat-storage, with) has 1 of 3 rows (short)
- cell (rat-storage, without) has 1 of 3 rows (short)
- cell (rat-storage-p1, agentsmd) has 1 of 3 rows (short)
- cell (rat-storage-p1, claudemd) has 1 of 3 rows (short)
- cell (rat-storage-p1, claudemd_agentsmd) has 0 of 3 rows (short)
- cell (rat-storage-p1, with) has 0 of 3 rows (short)
- cell (rat-storage-p1, without) has 1 of 3 rows (short)
- paired tokens_total (with_vs_claudemd) direction (with-worse) is driven by a single task 'rat-errors' — removing it flips/erases the sign
- paired turns (with_vs_claudemd) direction (with-better) is driven by a single task 'cont-logging' — removing it flips/erases the sign
- paired tool_calls (with_vs_claudemd) direction (with-better) is driven by a single task 'cont-logging' — removing it flips/erases the sign
- paired turns (claudemd_vs_without) direction (claudemd-better) is driven by a single task 'rat-storage' — removing it flips/erases the sign
- paired tokens_total (claudemd_agentsmd_vs_claudemd) direction (claudemd_agentsmd-worse) is driven by a single task 'cont-logging' — removing it flips/erases the sign
- paired cost_usd (claudemd_agentsmd_vs_claudemd) direction (claudemd_agentsmd-better) is driven by a single task 'rat-errors' — removing it flips/erases the sign
- paired turns (claudemd_agentsmd_vs_claudemd) direction (claudemd_agentsmd-worse) is driven by a single task 'cont-logging' — removing it flips/erases the sign
- paired tool_calls (claudemd_agentsmd_vs_claudemd) direction (claudemd_agentsmd-worse) is driven by a single task 'cont-logging' — removing it flips/erases the sign
- paired duration_ms (claudemd_agentsmd_vs_claudemd) direction (claudemd_agentsmd-worse) is driven by a single task 'cont-logging' — removing it flips/erases the sign

### Recomputed medians (errored rows excluded, 0 excluded)

| metric | without | agentsmd | claudemd | claudemd_agentsmd | with |
|---|---|---|---|---|---|
| tokens_total | 167762.500 | 114647.500 | 65647.500 | 32445 | 32789 |
| cost_usd | 0.140 | 0.081 | 0.057 | 0.043 | 0.043 |
| turns | 6 | 4.500 | 2 | 1 | 1 |
| tool_calls | 10 | 6.500 | 2 | 0 | 0 |
| duration_ms | 28595.500 | 19502.500 | 10514 | 6094 | 3915 |
| violations | 0 | 0 | 0 | 0 | 0 |
| rationale | 0 | 1 | 1 | 1 | 1 |
| success | 1 | 1 | 1 | 1 | 1 |

### Paired win/loss/tie (with vs without, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 3 | 0 | 0 |
| cost_usd | 3 | 0 | 0 |
| turns | 2 | 0 | 1 |
| tool_calls | 3 | 0 | 0 |
| duration_ms | 3 | 0 | 0 |

### Paired win/loss/tie (with vs claudemd, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 1 | 2 | 0 |
| cost_usd | 3 | 0 | 0 |
| turns | 1 | 0 | 2 |
| tool_calls | 1 | 0 | 2 |
| duration_ms | 3 | 0 | 0 |

### Paired win/loss/tie (claudemd vs without, by task+rep; lower is a claudemd-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 3 | 1 | 0 |
| cost_usd | 4 | 0 | 0 |
| turns | 2 | 1 | 1 |
| tool_calls | 3 | 1 | 0 |
| duration_ms | 3 | 1 | 0 |

### Paired win/loss/tie (agentsmd vs claudemd, by task+rep; lower is a agentsmd-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 1 | 3 | 0 |
| cost_usd | 0 | 4 | 0 |
| turns | 0 | 4 | 0 |
| tool_calls | 0 | 4 | 0 |
| duration_ms | 0 | 4 | 0 |

### Paired win/loss/tie (claudemd_agentsmd vs claudemd, by task+rep; lower is a claudemd_agentsmd-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 1 | 2 | 0 |
| cost_usd | 2 | 1 | 0 |
| turns | 0 | 1 | 2 |
| tool_calls | 0 | 1 | 2 |
| duration_ms | 1 | 2 | 0 |

