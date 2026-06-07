"""Token -> USD cost, derived locally (auth-independent).

Per-MTok rates (current as of 2026-06). Cache write = 1.25x input, cache read = 0.1x input.
Source: Anthropic pricing / claude-api skill reference.
"""

# input, output $ per million tokens
_RATES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# aliases the CLI accepts
_ALIASES = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


def _resolve(model: str) -> str:
    if model in _RATES:
        return model
    if model in _ALIASES:
        return _ALIASES[model]
    # tolerate a date-suffixed full id (e.g. claude-haiku-4-5-20251001)
    for known in _RATES:
        if model.startswith(known):
            return known
    return ""


def cost_usd(model: str, usage: dict) -> float:
    """Compute cost from a usage dict with the keys Claude Code's -p JSON reports:
    input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens.
    Falls back to 0.0 (and is flagged by the caller) for an unknown model.
    """
    key = _resolve(model)
    if not key:
        return 0.0
    in_rate, out_rate = _RATES[key]
    fresh_in = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    return (
        fresh_in * in_rate
        + cache_write * in_rate * 1.25
        + cache_read * in_rate * 0.1
        + out * out_rate
    ) / 1_000_000


def is_known(model: str) -> bool:
    return bool(_resolve(model))
