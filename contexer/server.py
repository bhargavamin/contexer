import asyncio
import json
import os
import uuid
from mcp.server.fastmcp import FastMCP
from contexer import store

SESSION_ID = str(uuid.uuid4())

# Server-level instructions travel with the MCP server itself: the host surfaces them to the
# model on initialize (Claude Code injects them under "MCP Server Instructions"), so this guidance
# applies in EVERY repo and session with no per-project CLAUDE.md required. This is the universal
# channel for "use Contexer" - the hooks provide the deterministic, model-independent capture.
_INSTRUCTIONS = (
    "Contexer is the project's persistent engineering-decision memory. Use it in every session and "
    "every repo without being asked.\n"
    "CAPTURE - call update_context whenever you make, or the user states, a significant decision: a "
    "technology or approach chosen over alternatives (subtype=architecture), a naming/structure "
    "convention (pattern/convention), a rule like 'always X'/'never Y' (constraint), or anything that "
    "would surprise a future session. Pass the full reasoning, not just the conclusion. The server "
    "silently filters duplicates, so err on the side of calling it.\n"
    "MATURITY - store observations and settled or user-ratified decisions freely, but keep your OWN "
    "not-yet-approved proposals provisional (created_by=ai records them as 'suggested', not "
    "authoritative) instead of writing them as fact. A decision from an approved-but-unimplemented "
    "plan is provisional (created_by=plan) until implementation validates it, then reconciled.\n"
    "RETRIEVE - call get_context BEFORE reading files for any question about architecture, design "
    "rationale, constraints, patterns, or conventions."
)
mcp = FastMCP("contexer", instructions=_INSTRUCTIONS)


@mcp.tool()
def update_context(content: str, repo_path: str = "", subtype: str = "",
                   created_by: str = "ai", replace_id: str = "") -> str:
    """Called when Claude Code makes a significant decision mid-task. The server filters before storing.

    subtype: optional classification for filtered retrieval — architecture | constraint | pattern | convention
    created_by: 'ai' (default) | 'plan' (a decision from a just-approved plan - stored PROVISIONAL/
                suggested until implementation validates it, then reconciled) | 'bootstrap' (when
                storing bootstrap_context results) | 'scan' (low-insight repo facts)
    replace_id: ID (full UUID or 8-char short id) of an existing decision this content changes.
                Bypasses similarity filtering. Decisions are versioned, never overwritten:
                - a trivial change (typo/formatting, or a pattern/convention) is applied in
                  place as a new revision, with the prior revision kept in history;
                - a significant change (architecture/constraint) becomes a Suggested Update
                  attached to the live decision and returns an approval prompt - the current
                  revision stays trusted until the developer approves.

    IMPORTANT: If this returns an 'Engineering Decision Detected/Updated' approval prompt, show
    it to the developer immediately and wait for their response before continuing. Do NOT ignore it.
    """
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "Skipped — repo path not detected."
    stored, entry_id = store.update_decision(resolved, content, SESSION_ID, subtype,
                                             created_by=created_by, replace_id=replace_id)
    if not stored:
        return "Filtered — did not meet storage criteria."
    prompt = store.get_pending_approval_prompt(resolved, entry_id)
    if prompt:
        return prompt
    return f"Stored. id={entry_id}"


@mcp.tool()
def approve_decision(entry_id: str, action: str, content: str = "", repo_path: str = "") -> str:
    """Approve, edit, skip, ignore, or dismiss a decision pending developer approval.

    entry_id: the ID returned by update_context when a decision required approval
    action: 'approve' - accept (a Suggested Update is promoted to a new revision, history kept)
            | 'edit' - correct and approve | 'skip' - keep pending for later
            | 'dismiss' - discard a Suggested Update, keep the current revision
            | 'ignore' - suppress a new decision permanently
    content: required when action='edit' — the corrected decision text
    """
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "Skipped — repo path not detected."
    ok, msg = store.approve_decision(resolved, entry_id, action, content)
    return msg


@mcp.tool()
def get_context(repo_path: str = "", query: str = "", entry_type: str = "", limit: int = 0) -> str:
    """Returns stored context for the current repository. Call this when the task requires project context.

    query: optional keyword filter (case-insensitive substring match against decision content).
    entry_type: optional subtype filter — architecture | constraint | pattern | convention
    limit: max decisions to return (0 = auto: 25 for filtered queries, 10 for unfiltered overview).
    """
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "No repo path detected."
    return store.get_context(resolved, query, entry_type, limit)


# Coarse upper bound for the whole share() round-trip (drain outbox + push the new decision,
# each at RemoteStore's ~10s transport timeout). It only exists as a backstop: a pathological
# remote that holds the connection open past its own timeout must not hang the tool call or
# occupy an executor worker unboundedly. Set well above the healthy worst case so a legitimately
# slow (but working) push never false-trips; a false trip is harmless anyway — share() is
# local-first and idempotent, so the background push still lands or the outbox retries it.
_SHARE_TIMEOUT = 30.0


@mcp.tool()
async def share_decision(decision_id: str = "", repo_path: str = "") -> str:
    """Explicitly push a local decision up to your team cloud context (never auto-shares).

    decision_id: the decision to share (full id or 8-char prefix); omit to share the most
    recent decision. Syncs to your PERSONAL cloud context today; true team review arrives
    with a team-scoped push endpoint."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "Skipped — repo path not detected."
    from contexer import share as _share

    # share() is synchronous and does blocking network I/O (RemoteStore -> asyncio.run).
    # This is the ONE MCP tool that reaches the network from inside FastMCP's event loop, so
    # run its blocking body on a worker thread the loop AWAITS rather than calling it inline
    # (which would freeze the whole server for the round-trip and, since asyncio.run can't run
    # inside a running loop, previously failed outright and misreported "endpoint unreachable").
    # Off the loop there is no running loop, so share()'s own asyncio.run works unchanged.
    #
    # Bounded so a wedged transport can't hang the tool call. NOTE a Python thread doing
    # blocking I/O can't be cancelled, so on timeout the worker keeps running in the
    # background until its transport gives up — but on the SHARED default executor that
    # occupancy is bounded (unlike a per-call executor), only sharing degrades (the loop
    # stays free for every other tool), and share() is local-first + outbox-backed so nothing
    # is lost. Fully reclaiming a wedged connection needs async-native transport (follow-up).
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_share.share, resolved, decision_id),
            timeout=_SHARE_TIMEOUT,
        )
    except TimeoutError:
        return (
            f"Saved locally — the team cloud did not respond within {int(_SHARE_TIMEOUT)}s. "
            "The push continues in the background and the outbox retries it automatically; "
            "your local decision is unchanged."
        )


@mcp.tool()
def bootstrap_context(repo_path: str = "", insight: str = "") -> str:
    """Scans a repo for inferable decisions and gap questions, filtered by how much
    insight the user has into the repo.

    insight: 'high' — user wrote or maintains the repo: confirm inferred items with
    them, then ask the intent gap questions.
    'medium' — user works with the repo but didn't build it: store inferred facts
    directly, ask only purpose and the user's goal.
    'low' — user is seeing the repo for the first time: store inferred facts directly,
    read README/docs for purpose, ask only what the user plans to do here.
    Empty — auto-detect from git history. The result includes 'insight' and 'decisive';
    if decisive is false, ask the user how well they know the repo, then re-call
    with their answer."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return json.dumps({"error": "repo path not detected"})
    return json.dumps(store.bootstrap_scan(resolved, insight), indent=2)


@mcp.tool()
def capture_user_constraint(prompt: str, repo_path: str = "") -> str:
    """Called on every UserPromptSubmit. Detects prescriptive directives ('always X', 'never Y',
    'from now on Z') and stores them as constraint or convention decisions automatically."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return ""
    entry_id, content = store.capture_user_constraint(resolved, prompt, SESSION_ID)
    if entry_id is None:
        return ""
    return (
        f"Auto-stored as constraint: '{content}'. "
        f"Acknowledge this briefly to the user — e.g. 'Stored as a constraint in Contexer.'"
    )


@mcp.tool()
def get_context_for_prompt(repo_path: str = "", prompt: str = "") -> str:
    """Auto-called by UserPromptSubmit hook on every prompt. Detects rationale/decision
    questions (why, reason, rationale, decided...) and injects matching stored decisions
    as additionalContext. Returns empty string for non-rationale prompts — silent no-op."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return ""
    return store.get_context_for_prompt(resolved, prompt)


@mcp.tool()
def update_global_context(content: str, subtype: str = "") -> str:
    """Stores a cross-cutting rule in the global store — applies to ALL repos.

    Use this only for constraints or conventions that genuinely apply everywhere:
    e.g. "always use conventional commits", "never commit untested code".
    Do NOT use for repo-specific decisions — use update_context instead.

    subtype: constraint | convention (defaults to convention if omitted)
    """
    stored, entry_id = store.update_global_decision(content, SESSION_ID, subtype)
    if stored:
        return f"Stored globally. id={entry_id}"
    return "Filtered — must be a novel constraint or convention (architecture/pattern are always repo-specific)."


@mcp.tool()
def get_global_context(query: str = "", entry_type: str = "", limit: int = 0) -> str:
    """Returns stored global context — constraints and conventions that apply across all repos.

    query: optional keyword filter (case-insensitive substring match).
    entry_type: constraint | convention
    limit: max decisions to return (0 = auto: 25 for filtered, 10 for unfiltered).
    """
    return store.get_global_context(query, entry_type, limit)


def main():
    # Bind this server process to the repo it was spawned in (its cwd's git root). Each host
    # session launches its own server with cwd = that session's project, so decisions resolve
    # to the right repo even if the shared ~/.contexer/.current_repo pointer has been clobbered
    # by a different tool or session. _resolve_repo prefers this over the shared pointer.
    store.set_session_repo(store._git_root(os.getcwd()))
    mcp.run()


if __name__ == "__main__":
    main()
