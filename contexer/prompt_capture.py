"""Pure bounded classifiers for factual decisions embedded in user prompts.

Persistence, locking, revision mutation, and acknowledgments stay in :mod:`contexer.store`.
This leaf only decides whether a prompt contains one of the deliberately narrow factual shapes
that deterministic capture supports and, for lifecycle corrections, which existing decision is
the closest still-retired target.
"""

import re

from contexer import revisions

MAX_FACTUAL_PROMPT_LEN = 300
MAX_LIFECYCLE_PROMPT_LEN = 1200

_SYSTEM_TEXT_PREFIXES = (
    "<task-notification", "<system-reminder", "<persisted-output",
    "[contexer", "contexer:",
)

_ENV_OPERATION = r"(?:run(?:s|ning)?|enabled|deployed|hosted|available|used|required|needed)"
_PRODUCTION_ENV = r"(?:live|prod(?:uction)?)(?:\s+env(?:ironment)?)?"
_NONPRODUCTION_ENV = r"(?:stag(?:e|ing)|test(?:ing)?|dev(?:elopment)?)(?:\s+env(?:ironment)?)?"
_PRODUCTION_ONLY_DECLARATION = re.compile(
    rf"(?:"
    rf"\bonly\s+{_ENV_OPERATION}\s+(?:in|on|for)\s+(?:the\s+)?{_PRODUCTION_ENV}\b"
    rf"|\b{_ENV_OPERATION}\s+only\s+(?:in|on|for)\s+(?:the\s+)?{_PRODUCTION_ENV}\b"
    rf"|\b{_ENV_OPERATION}\s+(?:in|on|for)\s+(?:the\s+)?{_PRODUCTION_ENV}\s+only\b"
    rf")",
    re.IGNORECASE,
)
_NONPRODUCTION_EXCLUSION = re.compile(
    rf"(?:"
    rf"\b(?:is|are|was|were)?(?:\s*not|n['’]t)\s+"
    rf"(?:{_ENV_OPERATION}\s+)?(?:(?:in|on|for)\s+)?(?:the\s+)?{_NONPRODUCTION_ENV}\b"
    rf"|\b{_NONPRODUCTION_ENV}\s+"
    rf"(?:(?:is|are|was|were)(?:\s+not|n['’]t)|does(?:\s+not|n['’]t))\s+"
    rf"(?:need|require|run|use|host|deploy|enable|apply)\w*\b"
    rf")",
    re.IGNORECASE,
)
_DECLARATION_NONASSERTION = re.compile(
    r"\b(?:"
    r"i\s+(?:thought|heard|read|was\s+told|am\s+not\s+sure|was\s+not\s+sure)"
    r"|(?:docs?|documentation|document|readme|spec(?:ification)?|ticket|issue|comment|message)\s+"
    r"(?:say|says|said|claim|claims|claimed|suggest|suggests|suggested)"
    r"|(?:someone|they|he|she)\s+"
    r"(?:say|says|said|claim|claims|claimed|suggest|suggests|suggested)"
    r"|according\s+to"
    r"|(?:maybe|perhaps|possibly|probably|apparently|seemingly)"
    r"|(?:could|might|may)\s+it\s+be"
    r"|(?:but|however)\s+(?:that|this|it)\s+(?:is|was)\s+"
    r"(?:wrong|false|incorrect|outdated)"
    r"|(?:but|however)\s+now"
    r"|no\s+longer"
    r")\b",
    re.IGNORECASE,
)

_ENVIRONMENT_NAME = r"(?:stag(?:e|ing)|test(?:ing)?|dev(?:elopment)?|prod(?:uction)?|live|qa|sandbox|preview|demo)"
_ENV_RETIRED_STATE = re.compile(
    r"\b(?:removed|retired|decommissioned|deleted|destroyed|disabled|shut\s+down|torn\s+down)\b",
    re.IGNORECASE,
)
_ENV_REACTIVATED_STATE = re.compile(
    r"\b(?:recreated|restored|reinstated|reprovisioned|re-provisioned|reactivated|"
    r"re-established|brought\s+back|created\s+again|available\s+again|active\s+again)\b",
    re.IGNORECASE,
)
_ENV_LIFECYCLE_REVERSAL = re.compile(
    rf"\b(?P<subject>(?:the\s+)?(?P<environment>{_ENVIRONMENT_NAME})\s+env(?:ironment)?)\s+"
    rf"(?:(?:was|had\s+been|is)\s+)?"
    rf"(?P<retired>{_ENV_RETIRED_STATE.pattern})"
    rf"[^?.!\n]{{0,80}}?(?:[,;]\s*)?(?:but|and)\s+now\s+"
    rf"(?:(?:it|it['’]?s|that\s+env(?:ironment)?|the\s+same\s+env(?:ironment)?)\s+)?"
    rf"(?:(?:is|was|has\s+been)\s+)?"
    rf"(?P<active>{_ENV_REACTIVATED_STATE.pattern})"
    rf"(?:\s+(?:in|on|under)\s+"
    rf"(?:(?!(?:and|but|then)\b)[\w'-]+\s*){{1,8}})?",
    re.IGNORECASE,
)
_LIFECYCLE_NONASSERTION = re.compile(
    r"\b(?:"
    r"i\s+(?:thought|heard|read|was\s+told|am\s+not\s+sure|was\s+not\s+sure)"
    r"|(?:docs?|documentation|document|readme|spec(?:ification)?|ticket|issue|comment|message)\s+"
    r"(?:say|says|said|claim|claims|claimed|suggest|suggests|suggested)"
    r"|(?:someone|they|he|she)\s+"
    r"(?:say|says|said|claim|claims|claimed|suggest|suggests|suggested)"
    r"|according\s+to"
    r"|(?:maybe|perhaps|possibly|probably|apparently|seemingly)"
    r"|(?:but|however)\s+(?:that|this|it)\s+(?:is|was)\s+"
    r"(?:wrong|false|incorrect|outdated)"
    r")\b",
    re.IGNORECASE,
)

_ENVIRONMENT_ALIASES = {
    "stage": "staging", "staging": "staging",
    "test": "test", "testing": "test",
    "dev": "development", "development": "development",
    "prod": "production", "production": "production", "live": "production",
    "qa": "qa", "sandbox": "sandbox", "preview": "preview", "demo": "demo",
}
_ENVIRONMENT_REFERENCES = {
    "staging": re.compile(r"\bstag(?:e|ing)(?:\s+env(?:ironment)?)?\b", re.IGNORECASE),
    "test": re.compile(r"\btest(?:ing)?(?:\s+env(?:ironment)?)?\b", re.IGNORECASE),
    "development": re.compile(r"\bdev(?:elopment)?(?:\s+env(?:ironment)?)?\b", re.IGNORECASE),
    "production": re.compile(
        r"\b(?:prod(?:uction)?|live)(?:\s+env(?:ironment)?)?\b", re.IGNORECASE),
    "qa": re.compile(r"\bqa(?:\s+env(?:ironment)?)?\b", re.IGNORECASE),
    "sandbox": re.compile(r"\bsandbox(?:\s+env(?:ironment)?)?\b", re.IGNORECASE),
    "preview": re.compile(r"\bpreview(?:\s+env(?:ironment)?)?\b", re.IGNORECASE),
    "demo": re.compile(r"\bdemo(?:\s+env(?:ironment)?)?\b", re.IGNORECASE),
}
_PUNCT_RE = re.compile(r"[^\w\s]")


def environment_scope_declaration(text: str) -> bool:
    """Whether a prompt asserts the bounded production-only deployment shape."""
    candidate = text.strip()
    if not candidate or len(candidate) > MAX_FACTUAL_PROMPT_LEN or "?" in candidate:
        return False
    if candidate.lower().startswith(_SYSTEM_TEXT_PREFIXES) or "```" in candidate:
        return False
    if _DECLARATION_NONASSERTION.search(candidate):
        return False
    return bool(
        _PRODUCTION_ONLY_DECLARATION.search(candidate)
        and _NONPRODUCTION_EXCLUSION.search(candidate)
    )


def environment_lifecycle_revision(text: str) -> tuple[str, str] | None:
    """Extract an asserted environment retirement reversal as ``(environment, content)``."""
    candidate = text.strip()
    if (not candidate or len(candidate) > MAX_LIFECYCLE_PROMPT_LEN
            or "?" in candidate or "```" in candidate):
        return None
    if (candidate.lower().startswith(_SYSTEM_TEXT_PREFIXES)
            or _LIFECYCLE_NONASSERTION.search(candidate)):
        return None
    match = _ENV_LIFECYCLE_REVERSAL.search(candidate)
    if match is None:
        return None
    environment = _ENVIRONMENT_ALIASES[match.group("environment").lower()]
    content = " ".join(match.group(0).strip(" \t,;.!?").split())
    return environment, content


def _tokenize(text: str) -> set[str]:
    return set(_PUNCT_RE.sub("", text.lower()).split())


def _overlap_ratio(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def find_environment_lifecycle_target(
    environment: str, content: str, existing: list,
) -> dict | None:
    """Find the closest still-retired decision for one explicitly reactivated environment."""
    reference = _ENVIRONMENT_REFERENCES[environment]
    content_tokens = _tokenize(content)
    candidates = []
    for entry in existing:
        if entry.get("status", "approved") == "pending_approval":
            continue
        current = revisions.current_content(entry)
        if (reference.search(current) and _ENV_RETIRED_STATE.search(current)
                and not _ENV_REACTIVATED_STATE.search(current)):
            candidates.append(entry)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda entry: _overlap_ratio(
            content_tokens, _tokenize(revisions.current_content(entry))),
    )
