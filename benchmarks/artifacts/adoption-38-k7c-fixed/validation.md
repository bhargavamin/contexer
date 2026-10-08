## Independent Validation

**Status: PASS** — 0 failure(s), 1 warning(s)

### Warnings

- paired tokens_total (with_vs_docs_indexed) direction (with-better) is driven by a single task 'adopt-k7c-version' — removing it flips/erases the sign

### Recomputed medians (errored rows excluded, 0 excluded)

| metric | claudemd_full | docs_indexed | with |
|---|---|---|---|
| tokens_total | 117330.500 | 48632.500 | 62268 |
| cost_usd | 0.069 | 0.041 | 0.046 |
| turns | 5 | 3 | 4 |
| tool_calls | 7 | 3.500 | 4 |
| duration_ms | 17645 | 9685 | 8561.500 |
| violations | 0 | 0 | 0 |
| rationale | 1 | 1 | 1 |
| success | 0 | 1 | 1 |

### Paired win/loss/tie (with vs claudemd_full, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 9 | 3 | 0 |
| cost_usd | 10 | 2 | 0 |
| turns | 8 | 3 | 1 |
| tool_calls | 8 | 4 | 0 |
| duration_ms | 10 | 2 | 0 |

### Paired win/loss/tie (with vs docs_indexed, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 7 | 5 | 0 |
| cost_usd | 9 | 3 | 0 |
| turns | 8 | 4 | 0 |
| tool_calls | 8 | 4 | 0 |
| duration_ms | 9 | 3 | 0 |

