# Contexer A/B Benchmark — model claude-sonnet-5, 183 runs (48 without / 45 claudemd / 45 with / 45 claudemd_with)

| metric | without | claudemd | with | claudemd_with | Δ (with−without) | Δ% (with−without) | Δ (with−claudemd) | Δ% (with−claudemd) | Δ (claudemd_with−claudemd) | Δ% (claudemd_with−claudemd) |
|---|---|---|---|---|---|---|---|---|---|---|
| tokens_total | 250439.500 | 287863 | 104304 | 219168 | -146135.500 | -58.4% | -183559 | -63.8% | -68695 | -23.9% |
| cost_usd | 0.164 | 0.172 | 0.092 | 0.170 | -0.072 | -43.9% | -0.080 | -46.7% | -0.003 | -1.5% |
| turns | 8.500 | 8 | 3 | 6 | -5.500 | -64.7% | -5 | -62.5% | -2 | -25.0% |
| tool_calls | 16 | 14 | 5 | 11 | -11 | -68.8% | -9 | -64.3% | -3 | -21.4% |
| duration_ms | 44783 | 47799 | 20892 | 46002 | -23891 | -53.3% | -26907 | -56.3% | -1797 | -3.8% |
| violations | 3.500 | 0 | 0 | 0 | -3.500 | -100.0% | 0 | n/a | 0 | n/a |
| rationale | 1 | 1 | 1 | 1 | 0 | +0.0% | 0 | +0.0% | 0 | +0.0% |
| success | 0 | 1 | 1 | 1 | 1 | n/a | 0 | +0.0% | 0 | +0.0% |

_Note: rationale 0.0 can mean the information was unavailable to that condition, not model failure — see per-condition design._

## Chain: orders

| step | without tokens | without violations | claudemd tokens | claudemd violations | with tokens | with violations | claudemd_with tokens | claudemd_with violations |
|---|---|---|---|---|---|---|---|---|
| 1 | 449346.500 | 5 | 454201 | 6 | 506805 | 6 | 422596 | 5 |
| 2 | 566277.500 | 8 | 406569 | 8 | 465484 | 9 | 509137 | 8 |
| 3 | 718931.500 | 10 | 485882 | 10 | 578000 | 12 | 612304 | 11 |

## Independent Validation

**Status: PASS** — 0 failure(s), 15 warning(s)

### Warnings

- cell (chain-1-cache, claudemd) has 7 of 8 rows (short)
- cell (chain-1-cache, claudemd_with) has 7 of 8 rows (short)
- cell (chain-1-cache, with) has 7 of 8 rows (short)
- cell (chain-2-list, claudemd) has 7 of 8 rows (short)
- cell (chain-2-list, claudemd_with) has 7 of 8 rows (short)
- cell (chain-2-list, with) has 7 of 8 rows (short)
- cell (chain-3-audit, claudemd) has 7 of 8 rows (short)
- cell (chain-3-audit, claudemd_with) has 7 of 8 rows (short)
- cell (chain-3-audit, with) has 7 of 8 rows (short)
- paired turns (with_vs_claudemd) direction (with-worse) is driven by a single task 'chain-3-audit' — removing it flips/erases the sign
- paired tool_calls (with_vs_claudemd) direction (with-worse) is driven by a single task 'chain-3-audit' — removing it flips/erases the sign
- paired duration_ms (with_vs_claudemd) direction (with-worse) is driven by a single task 'chain-2-list' — removing it flips/erases the sign
- paired turns (claudemd_with_vs_claudemd) direction (claudemd_with-worse) is driven by a single task 'chain-3-audit' — removing it flips/erases the sign
- paired tool_calls (claudemd_with_vs_claudemd) direction (claudemd_with-worse) is driven by a single task 'chain-3-audit' — removing it flips/erases the sign
- paired duration_ms (claudemd_with_vs_claudemd) direction (claudemd_with-better) is driven by a single task 'rat-errors' — removing it flips/erases the sign

### Recomputed medians (errored rows excluded, 0 excluded)

| metric | without | claudemd | with | claudemd_with |
|---|---|---|---|---|
| tokens_total | 250439.500 | 287863 | 104304 | 219168 |
| cost_usd | 0.164 | 0.172 | 0.092 | 0.170 |
| turns | 8.500 | 8 | 3 | 6 |
| tool_calls | 16 | 14 | 5 | 11 |
| duration_ms | 44783 | 47799 | 20892 | 46002 |
| violations | 3.500 | 0 | 0 | 0 |
| rationale | 1 | 1 | 1 | 1 |
| success | 0 | 1 | 1 | 1 |

### Paired win/loss/tie (with vs without, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 36 | 9 | 0 |
| cost_usd | 34 | 11 | 0 |
| turns | 34 | 10 | 1 |
| tool_calls | 32 | 10 | 3 |
| duration_ms | 34 | 11 | 0 |

### Paired win/loss/tie (with vs claudemd, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 15 | 30 | 0 |
| cost_usd | 16 | 29 | 0 |
| turns | 11 | 13 | 21 |
| tool_calls | 11 | 17 | 17 |
| duration_ms | 20 | 25 | 0 |

### Paired win/loss/tie (claudemd vs without, by task+rep; lower is a claudemd-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 35 | 10 | 0 |
| cost_usd | 38 | 7 | 0 |
| turns | 35 | 8 | 2 |
| tool_calls | 33 | 11 | 1 |
| duration_ms | 38 | 7 | 0 |

### Paired win/loss/tie (claudemd_with vs claudemd, by task+rep; lower is a claudemd_with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 13 | 32 | 0 |
| cost_usd | 12 | 33 | 0 |
| turns | 12 | 16 | 17 |
| tool_calls | 10 | 15 | 20 |
| duration_ms | 23 | 22 | 0 |

