"""Credential filtering for local benchmark metadata; judgments are rejected separately."""
import re

# Provider token shapes, private keys, JWTs, URL credentials, and keyword=value pairs whose value
# looks machine-generated (12+ characters mixing letters and digits, no spaces).
SECRET = re.compile(
    r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}"
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|npm_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{30,}"
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
    r"|(?i:password|passwd|secret|api[_-]?key|token)\s*[=:]\s*[\"']?"
    r"(?=[^\s\"']*\d)(?=[^\s\"']*[A-Za-z])[^\s\"']{12,}"
    r"|://[^/\s:@]+:[^@\s]+@")

def scrub(value):
    if isinstance(value, str):
        return SECRET.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    return value
