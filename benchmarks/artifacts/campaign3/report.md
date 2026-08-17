# Contexer A/B Benchmark - model claude-sonnet-5, 66 runs (33 without / 33 with)

| metric | without | with | Δ | Δ% |
|---|---|---|---|---|
| tokens_total | 283509 | 332370 | 48861 | +17.2% |
| cost_usd | 0.168 | 0.187 | 0.019 | +11.6% |
| turns | 8 | 9 | 1 | +12.5% |
| tool_calls | 13 | 15 | 2 | +15.4% |
| duration_ms | 45895 | 47614 | 1719 | +3.7% |
| violations | 2 | 0 | -2 | -100.0% |
| rationale | 1 | 1 | 0 | +0.0% |
| success | 1 | 1 | 0 | +0.0% |

## Chain: orders

| step | without tokens | without violations | with tokens | with violations |
|---|---|---|---|---|
| 1 | 451390 | 6 | 422432 | 5 |
| 2 | 568887 | 9 | 377395 | 7 |
| 3 | 789177 | 12 | 600992 | 10 |

## Independent Validation

**Status: PASS** - 0 failure(s), 0 warning(s)

### Recomputed medians (errored rows excluded, 0 excluded)

| metric | without | with |
|---|---|---|
| tokens_total | 283509 | 332370 |
| cost_usd | 0.168 | 0.187 |
| turns | 8 | 9 |
| tool_calls | 13 | 15 |
| duration_ms | 45895 | 47614 |
| violations | 2 | 0 |
| rationale | 1 | 1 |
| success | 1 | 1 |

### Paired win/loss/tie (with vs without, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 20 | 13 | 0 |
| cost_usd | 19 | 14 | 0 |
| turns | 23 | 10 | 0 |
| tool_calls | 18 | 12 | 3 |
| duration_ms | 20 | 13 | 0 |

