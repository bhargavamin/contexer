"""Tests for contexer/redact.py — the stdlib-only secret redactor.

Redaction is defense-in-depth: it runs at capture (store._normalize_content) so
secrets never reach disk, and again at the wire (remote._wire_args) so they never
egress. These tests pin the detector's contract: balanced provider patterns + a
keyword-gated generic catch-all, idempotent placeholders, and never-raises.
"""
import pytest

from contexer import redact

# ── provider token patterns (positive) ──────────────────────────────────────

@pytest.mark.parametrize("secret,kind", [
    ("AKIAIOSFODNN7EXAMPLE", "aws_key"),
    ("ASIAIOSFODNN7EXAMPLE", "aws_key"),
    ("ghp_" + "a" * 36, "github_token"),
    ("gho_" + "b" * 36, "github_token"),
    ("github_pat_" + "A1b2C3d4E5" * 4, "github_token"),
    ("xoxb-123456789012-1234567890123-" + "a" * 24, "slack_token"),
    ("sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc", "stripe_key"),
    ("rk_live_" + "4eC39HqLyjWDarjtT1zdp7dc", "stripe_key"),
    ("AIza" + "A" * 35, "google_api_key"),
    ("sk-ant-" + "a" * 24, "ai_key"),
    ("sk-" + "T3BlbkFJ" * 4, "ai_key"),
    ("SG." + "a" * 22 + "." + "b" * 43, "sendgrid_key"),
    ("npm_" + "a" * 36, "npm_token"),
    ("pypi-" + "AgEIcHlwaS5vcmc" + "a" * 20, "pypi_token"),
    ("SK" + "0123456789abcdef0123456789abcdef", "twilio_key"),
])
def test_provider_tokens_redacted(secret, kind):
    out, n = redact.scrub(f"the key is {secret} ok")
    assert secret not in out
    assert f"[REDACTED:{kind}]" in out
    assert n == 1


def test_pem_private_key_block_redacted():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdefG\n"
        "abcdefghijklmnopqrstuvwxyz0123456\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out, n = redact.scrub(f"deploy uses:\n{pem}\ndone")
    assert "MIIEpAIBAAKCAQEA" not in out
    assert "[REDACTED:private_key]" in out
    assert n == 1


def test_jwt_redacted():
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out, n = redact.scrub(f"token={jwt}")
    assert jwt not in out
    assert "[REDACTED:jwt]" in out
    assert n == 1


def test_bearer_token_redacted():
    out, n = redact.scrub("Authorization: Bearer abcDEF123456ghiJKL789mnoPQR")
    assert "abcDEF123456ghiJKL789mnoPQR" not in out
    assert "[REDACTED:bearer_token]" in out


def test_lowercase_bearer_token_redacted():
    # Authorization schemes are case-insensitive — a lowercase `bearer` must still be caught.
    out, n = redact.scrub("authorization: bearer abcDEF123456ghiJKL789mnoPQR")
    assert "abcDEF123456ghiJKL789mnoPQR" not in out
    assert "[REDACTED:bearer_token]" in out


def test_quoted_multiword_secret_fully_redacted():
    # A quoted value may contain spaces; the whole quoted span must be redacted, not just the
    # first word (else the rest of a passphrase leaks onto the wire).
    out, n = redact.scrub('password = "Correct Horse Battery Staple"')
    assert "Horse" not in out and "Staple" not in out
    assert "[REDACTED:secret]" in out
    assert n == 1


def test_connection_string_password_redacted():
    out, n = redact.scrub("postgres://admin:s3cr3tP4ss@db.example.com:5432/app")
    assert "s3cr3tP4ss" not in out
    assert "admin" in out          # only the password span is redacted
    assert "db.example.com" in out
    assert "[REDACTED:" in out


# ── keyword-gated generic catch-all ─────────────────────────────────────────

@pytest.mark.parametrize("text,value", [
    ('always use api_key = "my-internal-tok-123456"', "my-internal-tok-123456"),
    ("password: hunter2horse_battery", "hunter2horse_battery"),
    ("set client_secret=abcdef0123456789xyz", "abcdef0123456789xyz"),
    ("AUTH_TOKEN = 'Zx9kQ2mLpV8nR4tY'", "Zx9kQ2mLpV8nR4tY"),  # opaque mixed-case token
])
def test_generic_secret_assignment_redacted(text, value):
    out, n = redact.scrub(text)
    assert value not in out
    assert "[REDACTED:secret]" in out
    assert n >= 1


@pytest.mark.parametrize("text", [
    "auth: required for all endpoints",
    "token = short-lived JWTs",
    "password: required",
    "secret: optional",
    "the auth token must be rotated",
    "auth = bearer flow",
])
def test_generic_prose_not_redacted(text):
    # The keyword catch-all must not maul ordinary prose that happens to follow token:/auth= etc.
    # A plain lowercase word / hyphenated phrase is not a credential.
    out, n = redact.scrub(text)
    assert out == text
    assert n == 0


@pytest.mark.parametrize("text", [
    'api_key = "<your-key-here>"',
    'password = "***"',
    "secret: ${SECRET}",
    "token = env(MY_TOKEN)",
    'api_key = ""',
    "password = x",              # too short to be a real secret
])
def test_placeholder_values_not_redacted(text):
    out, n = redact.scrub(text)
    assert "[REDACTED" not in out
    assert n == 0


# ── negatives: no false positives on ordinary content ───────────────────────

@pytest.mark.parametrize("text", [
    "The function retries failed operations three times before giving up.",
    "commit 9fceb02d0ae598e95dc970b74767f19372d61af8 fixes the bug",
    "id 550e8400-e29b-41d4-a716-446655440000 was assigned",
    "use snake_case naming for all functions in this module",
    "the base64 blob aGVsbG8gd29ybGQ appears in the doc",
])
def test_ordinary_content_untouched(text):
    out, n = redact.scrub(text)
    assert out == text
    assert n == 0


# ── idempotency, counting, robustness ───────────────────────────────────────

def test_idempotent():
    text = "keys: AKIAIOSFODNN7EXAMPLE and api_key = 'my-internal-tok-123456'"
    once, n1 = redact.scrub(text)
    twice, n2 = redact.scrub(once)
    assert once == twice          # placeholder never re-matches
    assert n2 == 0


def test_multiple_secrets_counted():
    text = "a=AKIAIOSFODNN7EXAMPLE b=ghp_" + "c" * 36
    out, n = redact.scrub(text)
    assert n == 2
    assert "AKIA" not in out and "ghp_" not in out


def test_scrub_text_convenience():
    assert redact.scrub_text("AKIAIOSFODNN7EXAMPLE") == redact.scrub("AKIAIOSFODNN7EXAMPLE")[0]


def test_count_secrets_convenience():
    assert redact.count_secrets("AKIAIOSFODNN7EXAMPLE here") == 1
    assert redact.count_secrets("nothing sensitive here") == 0


@pytest.mark.parametrize("bad", [None, 123, b"AKIAIOSFODNN7EXAMPLE", ["a"], {"k": "v"}])
def test_never_raises_on_bad_input(bad):
    # Non-str input must not raise; it returns something string-ish with count 0.
    out, n = redact.scrub(bad)
    assert isinstance(out, str)
    assert isinstance(n, int)


def test_empty_string():
    assert redact.scrub("") == ("", 0)
