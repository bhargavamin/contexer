## Independent Validation

**Status: PASS** — 0 failure(s), 0 warning(s)

### Recomputed medians (errored rows excluded, 0 excluded)

| metric | without | claudemd_full | docs_indexed | with |
|---|---|---|---|---|
| tokens_total | 72412 | 142986 | 144010 | 127692.500 |
| cost_usd | 0.045 | 0.079 | 0.075 | 0.066 |
| turns | 5 | 7 | 8 | 6 |
| tool_calls | 5 | 8 | 10 | 8 |
| duration_ms | 11289.500 | 19830 | 21296 | 17579 |
| violations | 0 | 0 | 0 | 0 |
| rationale | 1 | 1 | 1 | 1 |
| success | 1 | 1 | 1 | 1 |

### Paired win/loss/tie (with vs without, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 8 | 106 | 0 |
| cost_usd | 3 | 111 | 0 |
| turns | 17 | 67 | 30 |
| tool_calls | 10 | 91 | 13 |
| duration_ms | 7 | 107 | 0 |

### Paired win/loss/tie (with vs claudemd_full, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 71 | 43 | 0 |
| cost_usd | 95 | 19 | 0 |
| turns | 59 | 36 | 19 |
| tool_calls | 51 | 41 | 22 |
| duration_ms | 73 | 41 | 0 |

### Paired win/loss/tie (with vs docs_indexed, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 76 | 38 | 0 |
| cost_usd | 77 | 37 | 0 |
| turns | 77 | 22 | 15 |
| tool_calls | 81 | 27 | 6 |
| duration_ms | 83 | 31 | 0 |

### Paired win/loss/tie (claudemd_full vs without, by task+rep; lower is a claudemd_full-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 5 | 109 | 0 |
| cost_usd | 3 | 111 | 0 |
| turns | 9 | 94 | 11 |
| tool_calls | 6 | 105 | 3 |
| duration_ms | 5 | 109 | 0 |

### Paired win/loss/tie (docs_indexed vs without, by task+rep; lower is a docs_indexed-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 8 | 106 | 0 |
| cost_usd | 5 | 109 | 0 |
| turns | 7 | 100 | 7 |
| tool_calls | 8 | 103 | 3 |
| duration_ms | 6 | 108 | 0 |

