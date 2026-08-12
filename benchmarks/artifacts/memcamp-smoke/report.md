# Memory-vs-Contexer Benchmark — model claude-sonnet-5, 66 rows (without, memory, with)


## sup-current

| arm | implicit | explicit | pooled (headline) |
|---|---|---|---|
| without | 0/0 (0.00-0.00), 2 review | 0/0 (0.00-0.00), 1 review | 0/0 (0.00-0.00), 3 review |
| memory | 2/2 (0.34-1.00) | 1/1 (0.21-1.00) | 3/3 (0.44-1.00) |
| with | 2/2 (0.34-1.00) | 0/1 (0.00-0.79) | 2/3 (0.21-0.94) |

_Pooled cell = both phrasing tiers combined, with its own Wilson interval; it is the pre-registered headline number. Per-tier cells are half the reps each and exist to show phrasing sensitivity._

## cont-log

| arm | implicit | explicit | pooled (headline) |
|---|---|---|---|
| without | 2/2 (0.34-1.00) | 1/1 (0.21-1.00) | 3/3 (0.44-1.00) |
| memory | 2/2 (0.34-1.00) | 1/1 (0.21-1.00) | 3/3 (0.44-1.00) |
| with | 2/2 (0.34-1.00) | 1/1 (0.21-1.00) | 3/3 (0.44-1.00) |

_Pooled cell = both phrasing tiers combined, with its own Wilson interval; it is the pre-registered headline number. Per-tier cells are half the reps each and exist to show phrasing sensitivity._

## Capture rate (post-teaching)

| arm | tier | n | median memory_files | median contexer_entries |
|---|---|---|---|---|
| without | implicit | 2 | 0 | 0 |
| without | explicit | 1 | 0 | 0 |
| memory | implicit | 2 | 3 | 0 |
| memory | explicit | 1 | 4 | 0 |
| with | implicit | 2 | 0 | 13.5 |
| with | explicit | 1 | 0 | 13 |

## Cost of capture (teach-phase tokens)

- memory: median 169698 tokens across 15 teach session(s)
- with: median 105808 tokens across 15 teach session(s)

## Mechanism demonstration (enf-commit)

- without (implicit, rep 0): no mechanism
- memory (implicit, rep 0): no mechanism
- with (implicit, rep 0): no violating change attempted
- with (explicit, rep 1): no violating change attempted
- memory (explicit, rep 1): no mechanism
- without (explicit, rep 1): no mechanism
- without (implicit, rep 2): no mechanism
- memory (implicit, rep 2): no mechanism
- with (implicit, rep 2): no violating change attempted

## rat-mem

- without: median 66976 tokens, success 0/3
- memory: median 170231 tokens, success 1/3
- with: median 34516 tokens, success 2/3
## Independent Validation

**Status: PASS** — 0 failure(s), 8 warning(s)

### Warnings

- duration outlier: rat-mem/with/rep2 31335ms > 5x cell median 4590ms
- paired tokens_total (with_vs_memory) direction (with-better) is driven by a single task 'enf-commit' — removing it flips/erases the sign
- tier imbalance for sup-current/without: implicit=2 explicit=1
- tier imbalance for cont-log/without: implicit=2 explicit=1
- tier imbalance for sup-current/memory: implicit=2 explicit=1
- tier imbalance for cont-log/memory: implicit=2 explicit=1
- tier imbalance for sup-current/with: implicit=2 explicit=1
- tier imbalance for cont-log/with: implicit=2 explicit=1

### Recomputed medians (errored rows excluded, 0 excluded)

| metric | without | memory | with |
|---|---|---|---|
| tokens_total | 208653 | 170231 | 106736 |
| cost_usd | 0.167 | 0.077 | 0.072 |
| turns | 6 | 5 | 3 |
| tool_calls | 11 | 6 | 4 |
| duration_ms | 36093 | 18731 | 27143 |
| violations | 0 | 0 | 0 |
| rationale | 0 | 0 | 0 |
| success | 0 | 1 | 1 |

### Paired win/loss/tie (with vs without, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 8 | 4 | 0 |
| cost_usd | 10 | 2 | 0 |
| turns | 10 | 2 | 0 |
| tool_calls | 11 | 1 | 0 |
| duration_ms | 9 | 3 | 0 |

### Paired win/loss/tie (with vs memory, by task+rep; lower is a with-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 14 | 13 | 0 |
| cost_usd | 16 | 11 | 0 |
| turns | 18 | 5 | 4 |
| tool_calls | 16 | 6 | 5 |
| duration_ms | 19 | 8 | 0 |

### Paired win/loss/tie (memory vs without, by task+rep; lower is a memory-win)

| metric | wins | losses | ties |
|---|---|---|---|
| tokens_total | 9 | 3 | 0 |
| cost_usd | 12 | 0 | 0 |
| turns | 10 | 2 | 0 |
| tool_calls | 12 | 0 | 0 |
| duration_ms | 11 | 1 | 0 |

