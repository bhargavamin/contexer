# Contexer A/B Benchmark — model claude-opus-4-8, 187 runs (48 without / 48 claudemd / 46 with / 45 claudemd_with)

| metric | without | claudemd | with | claudemd_with | Δ (with−without) | Δ% (with−without) | Δ (with−claudemd) | Δ% (with−claudemd) | Δ (claudemd_with−claudemd) | Δ% (claudemd_with−claudemd) |
|---|---|---|---|---|---|---|---|---|---|---|
| tokens_total | 144831.500 | 159210.500 | 188340 | 103095 | 43508.500 | +30.0% | 29129.500 | +18.3% | -56115.500 | -35.2% |
| cost_usd | 0.243 | 0.218 | 0.264 | 0.207 | 0.021 | +8.5% | 0.046 | +21.1% | -0.011 | -5.2% |
| turns | 9 | 8 | 9 | 5 | 0 | +0.0% | 1 | +12.5% | -3 | -37.5% |
| tool_calls | 16 | 17.500 | 17.500 | 10 | 1.500 | +9.4% | 0 | +0.0% | -7.500 | -42.9% |
| duration_ms | 54903.500 | 50921 | 55179 | 46789 | 275.500 | +0.5% | 4258 | +8.4% | -4132 | -8.1% |
| violations | 2.500 | 2.500 | 0 | 0 | -2.500 | -100.0% | -2.500 | -100.0% | -2.500 | -100.0% |
| rationale | 1 | 1 | 1 | 1 | 0 | +0.0% | 0 | +0.0% | 0 | +0.0% |
| success | 0 | 1 | 1 | 1 | 1 | n/a | 0 | +0.0% | 0 | +0.0% |

_Note: rationale 0.0 can mean the information was unavailable to that condition, not model failure — see per-condition design._

## Chain: orders

| step | without tokens | without violations | claudemd tokens | claudemd violations | with tokens | with violations | claudemd_with tokens | claudemd_with violations |
|---|---|---|---|---|---|---|---|---|
| 1 | 237209 | 5.500 | 203582 | 6.500 | 242922.500 | 6.500 | 275577 | 7 |
| 2 | 168222 | 8 | 221482.500 | 9 | 261820 | 10 | 176780 | 10 |
| 3 | 287706.500 | 10 | 344521 | 11.500 | 403499 | 13 | 400775 | 12 |

## Independent Validation

**Status: PASS** — 0 failure(s), 11 warning(s)

### Warnings

- cell (chain-1-cache, claudemd_with) has 7 of 8 rows (short)
- cell (chain-2-list, claudemd_with) has 7 of 8 rows (short)
- cell (chain-2-list, with) has 7 of 8 rows (short)
- cell (chain-3-audit, claudemd_with) has 7 of 8 rows (short)
- cell (chain-3-audit, with) has 7 of 8 rows (short)
- paired cost_usd (with_vs_without) direction (with-worse) is driven by a single task 'chain-2-list' — removing it flips/erases the sign
- paired turns (with_vs_without) direction (with-better) is driven by a single task 'rat-errors' — removing it flips/erases the sign
- paired tool_calls (with_vs_without) direction (with-worse) is driven by a single task 'chain-2-list' — removing it flips/erases the sign
- paired duration_ms (with_vs_without) direction (with-worse) is driven by a single task 'chain-2-list' — removing it flips/erases the sign
- paired turns (claudemd_with_vs_claudemd) direction (claudemd_with-worse) is driven by a single task 'chain-1-cache' — removing it flips/erases the sign
- paired duration_ms (claudemd_with_vs_claudemd) direction (claudemd_with-worse) is driven by a single task 'chain-1-cache' — removing it flips/erases the sign

### Recomputed medians (errored rows excluded, 0 excluded)

| metric | without | claudemd | with | claudemd_with |
|---|---|---|---|---|
| tokens_total | 144831.500 | 159210.500 | 188340 | 103095 |
| cost_usd | 0.243 | 0.218 | 0.264 | 0.207 |
| turns | 9 | 8 | 9 | 5 |
| tool_calls | 16 | 17.500 | 17.500 | 10 |
| duration_ms | 54903.500 | 50921 | 55179 | 46789 |
| violations | 2.500 | 2.500 | 0 | 0 |
| rationale | 1 | 1 | 1 | 1 |
| success | 0 | 1 | 1 | 1 |

### Paired win/loss/tie (with vs without, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 28 | 18 | 0 |
| cost_usd | 22 | 24 | 0 |
| turns | 23 | 19 | 4 |
| tool_calls | 20 | 23 | 3 |
| duration_ms | 22 | 24 | 0 |

### Paired win/loss/tie (with vs claudemd, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 7 | 39 | 0 |
| cost_usd | 3 | 43 | 0 |
| turns | 3 | 20 | 23 |
| tool_calls | 3 | 23 | 20 |
| duration_ms | 17 | 29 | 0 |

### Paired win/loss/tie (claudemd vs without, by task+rep; lower is a claudemd-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 32 | 16 | 0 |
| cost_usd | 31 | 17 | 0 |
| turns | 36 | 8 | 4 |
| tool_calls | 30 | 15 | 3 |
| duration_ms | 33 | 15 | 0 |

### Paired win/loss/tie (claudemd_with vs claudemd, by task+rep; lower is a claudemd_with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 9 | 36 | 0 |
| cost_usd | 10 | 35 | 0 |
| turns | 9 | 16 | 20 |
| tool_calls | 10 | 17 | 18 |
| duration_ms | 20 | 25 | 0 |

