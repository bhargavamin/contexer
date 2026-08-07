"""Deterministic secret redaction, stdlib only (`re`).

Defense-in-depth: `scrub` runs at capture (store._normalize_content) so secrets never
reach disk, and again at the wire (remote._wire_args) so they never egress to the remote
Teams MCP. A leaf module — imported by store.py and remote.py, importing neither.

Design (balanced detection, developer-confirmed):
  - high-confidence provider token shapes (AWS/GitHub/Slack/Stripe/Google/AI/SendGrid/npm/
    PyPI/Twilio), PEM private-key blocks, JWTs, Bearer tokens, connection-string passwords;
  - a keyword-gated generic catch-all (`secret = "value"`) that skips placeholder-ish values;
  - NO entropy heuristic (would false-positive on SHAs/UUIDs/base64).

Each match becomes `[REDACTED:<kind>]`. Idempotent: the placeholder never re-matches, so a
double scrub (capture then wire) is a no-op. Never raises — any failure returns best-effort.
"""
import re

_PLACEHOLDER = "[REDACTED:{}]"

# ── full-match provider/format patterns (whole match → placeholder) ──────────
# Ordered: multi-line PEM and JWT first, then fixed-shape provider tokens.
_FULL: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL), "private_key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "jwt"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "aws_key"),
    (re.compile(r"\bgh[posru]_[A-Za-z0-9]{36,}\b"), "github_token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), "github_token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack_token"),
    (re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}\b"), "stripe_key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "google_api_key"),
    (re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b"), "sendgrid_key"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), "npm_token"),
    (re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}\b"), "pypi_token"),
    (re.compile(r"\bSK[0-9a-f]{32}\b"), "twilio_key"),
    # AI keys last of the token group: `sk-` / `sk-ant-` are hyphenated (distinct from
    # Stripe's underscore `sk_live_`), so no overlap with the patterns above.
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b"), "ai_key"),
]

# ── group-replace patterns (preserve context, redact only the secret span) ───
# Bearer is IGNORECASE: HTTP auth schemes are case-insensitive, so `bearer <tok>` must match too.
_BEARER = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE)
_CONN = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*)://([^:@/\s]+):([^@/\s]+)@")
# Generic keyword=value. The value is either a QUOTED span (may contain spaces — so a quoted
# passphrase is redacted whole, not just its first word) or a bare non-whitespace token.
_GENERIC = re.compile(
    r"(?i)\b(password|passwd|pwd|secret_key|secret|api[_-]?key|access[_-]?key|"
    r"access[_-]?token|client[_-]?secret|private[_-]?key|auth_token|auth|token)"
    r"(\s*[:=]\s*)"
    r"(?:(['\"])(.+?)\3|([^\s'\"]+))")

# ── additive export for external high-confidence-only consumers ──────────────
# The commit-time guard's Tier-2 "secret" armed rule (contexer/store.py) needs to
# ask "does this line look like a real credential?" with ZERO tolerance for false
# positives (a blocking check, unlike scrub's redact-on-suspicion posture). So it
# gets ONLY the already-compiled high-confidence pattern objects — provider token
# shapes, PEM private-key blocks, JWTs, and connection-string passwords — never the
# keyword-gated generic catch-all, whose `_looks_secretlike` gate is precision-
# risky on ordinary code (`token = short_lived`). Pure re-export of existing
# pattern objects: no new regex, no behavior change to `scrub`, module stays leaf.
HIGH_CONFIDENCE_PATTERNS: list[re.Pattern] = [rx for rx, _kind in _FULL] + [_CONN]

_MIN_VALUE_LEN = 6
# A plain lowercase word or hyphenated/apostrophed phrase ("required", "short-lived",
# "bearer") — prose, not a credential. Used to gate the generic keyword catch-all.
_PROSE_VALUE = re.compile(r"[a-z]+(?:[-'][a-z]+)*")


def _looks_secretlike(value: str) -> bool:
    """Gate for the generic keyword catch-all: redact a value only if it plausibly IS a
    credential, not ordinary prose that merely follows `token:`/`auth =`. A credential is
    >=6 chars and NOT a plain lowercase word/phrase (that is how "required", "short-lived",
    "optional" slipped through and mauled real decisions). Known tradeoff: an all-lowercase
    passphrase reads as prose and is missed here — provider patterns still catch real API keys,
    and erring toward not corrupting stored decisions is the deliberate choice."""
    if len(value) < _MIN_VALUE_LEN:
        return False
    return _PROSE_VALUE.fullmatch(value) is None


def _is_placeholder(value: str) -> bool:
    """A value that is obviously a stand-in, not a real secret — never redacted."""
    if value.startswith("[REDACTED"):        # already scrubbed → idempotent no-op
        return True
    if value.startswith("<") and value.endswith(">"):
        return True
    if value.startswith("${") or value.startswith("env("):
        return True
    if value and set(value) <= {"*"}:         # "***"
        return True
    return False


def scrub(text: str) -> tuple[str, int]:
    """Return (redacted_text, redaction_count). Never raises."""
    try:
        if not isinstance(text, str):
            text = str(text)
        if not text:
            return text, 0

        total = 0
        for rx, kind in _FULL:
            text, n = rx.subn(_PLACEHOLDER.format(kind), text)
            total += n

        def _bearer(m: re.Match) -> str:
            nonlocal total
            total += 1
            return f"{m.group(1)}{_PLACEHOLDER.format('bearer_token')}"

        def _conn(m: re.Match) -> str:
            nonlocal total
            total += 1
            return f"{m.group(1)}://{m.group(2)}:{_PLACEHOLDER.format('credential')}@"

        def _generic(m: re.Match) -> str:
            nonlocal total
            quote = m.group(3)                       # the quote char, or None for a bare value
            value = m.group(4) if quote else m.group(5)
            if _is_placeholder(value) or not _looks_secretlike(value):
                return m.group(0)
            total += 1
            secret = _PLACEHOLDER.format("secret")
            if quote:
                return f"{m.group(1)}{m.group(2)}{quote}{secret}{quote}"
            return f"{m.group(1)}{m.group(2)}{secret}"

        text = _BEARER.sub(_bearer, text)
        text = _CONN.sub(_conn, text)
        text = _GENERIC.sub(_generic, text)
        return text, total
    except Exception:
        # Redaction must never break capture or a push; degrade to best-effort.
        return (text if isinstance(text, str) else ""), 0


def scrub_text(text: str) -> str:
    """Convenience: the redacted text only."""
    return scrub(text)[0]


def count_secrets(text: str) -> int:
    """Convenience: how many secrets scrub would redact (for preview messaging)."""
    return scrub(text)[1]
