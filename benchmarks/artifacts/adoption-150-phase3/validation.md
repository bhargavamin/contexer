## Independent Validation

**Status: PASS** — 0 failure(s), 0 warning(s)

### Recomputed medians (errored rows excluded, 0 excluded)

| metric | claudemd_full | docs_indexed | with |
|---|---|---|---|
| tokens_total | 194203 | 196740 | 80939.500 |
| cost_usd | 0.110 | 0.104 | 0.050 |
| turns | 7 | 8 | 4 |
| tool_calls | 8 | 11 | 4 |
| duration_ms | 21385 | 23144.500 | 10740 |
| violations | 0 | 0 | 0 |
| rationale | 1 | 1 | 1 |
| success | 1 | 1 | 1 |

### Paired win/loss/tie (with vs claudemd_full, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 112 | 2 | 0 |
| cost_usd | 114 | 0 | 0 |
| turns | 94 | 9 | 11 |
| tool_calls | 100 | 11 | 3 |
| duration_ms | 109 | 5 | 0 |

### Paired win/loss/tie (with vs docs_indexed, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 109 | 5 | 0 |
| cost_usd | 113 | 1 | 0 |
| turns | 105 | 7 | 2 |
| tool_calls | 106 | 7 | 1 |
| duration_ms | 110 | 4 | 0 |

