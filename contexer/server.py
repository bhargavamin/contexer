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
    "would surprise a future session. A synthesized understanding of how a subsystem works, reached "
    "by exploring the codebase to answer a question, is capture-worthy too (subtype=architecture) — "
    "store it the same turn, since the session may end with the answer. Pass the full reasoning, not "
    "just the conclusion, and always pass a concise, one-line, imperative title (<= 100 chars) "
    "summarizing the decision — e.g. 'Use Postgres for decision store' — omit it only if you truly "
    "can't summarize better than the store's own derivation from content. The server silently filters "
    "duplicates, so err on the side of calling it.\n"
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
                   created_by: str = "ai", replace_id: str = "", title: str = "",
                   source_files: list[str] | None = None) -> str:
    """Called when Claude Code makes a significant decision mid-task. The server filters before storing.

    A synthesized understanding of how a subsystem works — produced by exploring or reading the
    codebase to answer a question — is also capture-worthy (subtype='architecture' for subsystem
    behaviour/structure, 'pattern' for recurring code organization); store it in the SAME turn as
    the exploration, since sessions often end right after the answer and there may be no next
    prompt to catch it.

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
    source_files: repo-relative paths this content describes (max 10). When capturing a
                comprehension summary, pass the files it describes so future injections can
                flag it as possibly stale once that code changes. Anchors a newly stored
                decision, and also re-anchors a replace_id correction (fresh files + current
                HEAD) as soon as the corrected text becomes the live, rendered content — for
                a trivial correction or a re-capture of still-accurate content, that's
                immediate; for a significant correction (architecture/constraint) it happens
                only once a developer approves the resulting Suggested Update, since until
                then the OLD content is still what's shown. Never a recurrence. When an
                injection shows a decision with "[may be stale: ...]", re-read the named
                file(s) and re-capture via replace_id, passing source_files again so the
                anchor refreshes (immediately, or on approval) and the note clears; omitting
                source_files on that correction leaves the old anchor in place and the note
                keeps firing.
    title: Provide a concise, one-line, imperative title (<= 100 chars) summarizing the decision,
           shown when it's listed/injected — e.g. 'Use Postgres for decision store'. Only omit it
           when you can't summarize better than the content itself; the store then derives one
           from `content`.

    If this returns a 'pending review' notice, the decision is recorded but NOT yet trusted and
    does not block your work — keep going. Surface it to the developer for approval at a natural
    point (call approve_decision when they respond, or they can run `contexer review`); never
    discard it silently. Use review_pending to list everything awaiting review with its content.
    """
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "Skipped — repo path not detected."
    lint = store.capture_lint(content, created_by=created_by, replace_id=replace_id)
    if lint:
        return lint
    stored, entry_id = store.update_decision(resolved, content, SESSION_ID, subtype,
                                             created_by=created_by, replace_id=replace_id,
                                             title=title, source_files=source_files)
    if not stored:
        return "Filtered — did not meet storage criteria."
    prompt = store.get_pending_approval_prompt(resolved, entry_id)
    if prompt:
        return prompt
    return f"Stored. id={entry_id}"


@mcp.tool()
def approve_decision(entry_id: str, action: str, content: str = "", repo_path: str = "",
                     source_files: list[str] | None = None) -> str:
    """Approve, edit, skip, ignore, or dismiss decision(s) pending developer review — or
    retire an already-trusted (approved/suggested) decision with 'ignore'.

    entry_id: a decision id (full or 8-char prefix); a comma-separated list to act on several
              at once ("id1,id2,id3"); or "all" (or "*") to act on EVERY pending decision — so
              the developer can clear the whole backlog in one gesture after reviewing ("all"
              only reaches pending decisions, not active ones — ignore those by id).
    action: 'approve' - accept (a Suggested Update is promoted to a new revision, history kept)
            | 'edit' - correct and approve | 'skip' - keep pending for later
            | 'dismiss' - discard a Suggested Update, keep the current revision
            | 'ignore' - suppress a new decision permanently, OR retire an already-trusted
              decision (e.g. to consolidate an overlap-report cluster — full history is kept,
              only status flips to 'ignored'). 'approve'/'edit'/'dismiss'/'skip' remain
              pending-only: an already-approved decision cannot be re-approved.
    content: required when action='edit' — the corrected decision text (single decision only)
    source_files: repo-relative files this decision describes — anchors it for staleness
                  tracking and the commit-time guard; single-id approvals only.
    """
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "Skipped — repo path not detected."
    if source_files and (entry_id.strip().lower() in ("all", "*") or "," in entry_id):
        raise ValueError("source_files requires a single decision id")
    hidden = 0
    if entry_id.strip().lower() in ("all", "*"):
        pending = store.get_pending_decisions(resolved)
        # Only act on decisions that review_pending actually SHOWED (its display cap). Approving
        # beyond the cap would trust decisions the developer never saw — so "all" clears the shown
        # set and reports the remainder rather than silently approving unseen content.
        shown = pending[:store._FILTERED_DISPLAY]
        ids = [d["id"] for d in shown]
        hidden = len(pending) - len(shown)
        if not ids:
            return "Nothing pending review."
    else:
        ids = [i.strip() for i in entry_id.split(",") if i.strip()]
    if not ids:
        return "No decision id given."
    if len(ids) == 1 and not hidden:
        return store.approve_decision(resolved, ids[0], action, content,
                                      source_files=source_files)[1]
    # Bulk: 'edit' needs per-decision content, so it's single-only.
    if action == "edit":
        return "Bulk 'edit' isn't supported — edit decisions one at a time."
    results = store.approve_decisions(resolved, ids, action, content)  # one atomic transaction
    succeeded = sum(1 for _i, ok, _m in results if ok)
    failed = len(results) - succeeded
    header = f"Applied '{action}' to {succeeded} of {len(results)} decision(s)"
    if failed:
        header += f" ({failed} failed — see below)"
    body = "\n".join(f"  {i[:8]}: {m}" for i, _ok, m in results)
    tail = ""
    if hidden:
        tail = (f"\n\n{hidden} more pending decision(s) were NOT touched — 'all' only acts on the "
                f"{store._FILTERED_DISPLAY} shown by review_pending. Run review_pending again to "
                "review and clear the rest.")
    return f"{header}:\n{body}{tail}"


@mcp.tool()
def resolve_conflict(entry_id: str, choice: str, repo_path: str = "") -> str:
    """Record the developer's EXPLICIT pick between a decision's standing (approved) version
    and its unreviewed Suggested Update, as shown in a labeled conflict render (both versions,
    with dates). Call this ONLY when the developer themselves stated the pick in a genuine user
    turn in this conversation. NEVER call it from your own inference, codebase exploration, or
    judgment about which version looks correct — if the developer hasn't said, ask them. This
    approves nothing: the update stays pending formal review (review_pending / `contexer
    review`); a later explicit statement from the developer outranks the memo.

    entry_id: the decision's id exactly as rendered, e.g. (id=6fb28fd9) — at least 8 chars.
    choice:   'standing' (steer by the current approved version) or 'update' (steer by the
              unreviewed proposal — its content becomes operative but the approved version
              still renders as a demoted continuation line, since only a review action fully
              hides reviewed content).
    """
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "Skipped — repo path not detected."
    return store.record_conflict_memo(resolved, entry_id, choice, session_id=SESSION_ID)[1]


@mcp.tool()
def review_pending(repo_path: str = "") -> str:
    """List decisions awaiting the developer's review — brand-new pending-approval decisions and
    suggested updates — each with its id and full content, so you can surface them conversationally
    and approve via approve_decision. The in-session equivalent of the `contexer review` terminal
    command. Call this when the developer asks to review, or when SessionStart reported items pending."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "No repo path detected."
    return store.format_pending_review(resolved)


@mcp.tool()
def list_shareable(repo_path: str = "") -> str:
    """List decisions available to push to your personal cloud, each with its id and content, so
    the developer can pick which to share. Use this when the developer wants to share but hasn't
    named a decision — show the list, let them choose, then call share_decision with the chosen
    id(s) (comma-separated for a multi-select)."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "No repo path detected."
    return store.format_shareable_list(resolved)


@mcp.tool()
def get_context(repo_path: str = "", query: str = "", entry_type: str = "", limit: int = 0,
                 files: list[str] | None = None) -> str:
    """Returns stored context for the current repository. Call this when the task requires project context.

    query: optional keyword filter (case-insensitive substring match against decision content).
    entry_type: optional subtype filter — architecture | constraint | pattern | convention
    limit: max decisions to return (0 = auto: 25 for filtered queries, 10 for unfiltered overview).
    files: repo-relative files you are about to work on — returns the decisions that govern
    them (anchors + content references).
    """
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "No repo path detected."
    result = store.get_context(resolved, query, entry_type, limit, files)
    # Follow-through log (Retrieval V1 Part B): if a recent pointer nudge for this repo
    # matches this query's topic AND this call actually found decisions, record it. Log-only
    # — never changes the result above.
    found = "No matching decisions" not in result and "No context stored" not in result
    store.log_followup_if_matching(resolved, query, found)
    return result


# Coarse upper bound for the whole share round-trip (drain outbox + push the new decision,
# each at RemoteStore's ~10s transport timeout). It only exists as a backstop: a pathological
# remote that holds the connection open past its own timeout must not hang the tool call. On
# timeout the awaited push is CANCELLED (the socket closes), so nothing lingers. Set well above
# the healthy worst case so a legitimately slow (but working) push never false-trips; a false
# trip is harmless anyway — share is local-first and idempotent, so the outbox retries it.
_SHARE_TIMEOUT = 30.0


@mcp.tool()
async def share_decision(decision_id: str = "", repo_path: str = "", confirm: bool = False) -> str:
    """Explicitly push a local decision up to your team cloud context (never auto-shares).

    decision_id: the decision(s) to share — a full id / 8-char prefix, or a comma-separated
    selection ("ab12cd34,ef56gh78") to share several at once; omit to share the most recent.
    Use list_shareable first when the developer hasn't named which decision. Syncs to your
    PERSONAL cloud context today; true team review arrives with a team-scoped push endpoint.
    confirm: safety gate. When false (default) this PREVIEWS what would be sent and does NOT
    push — show the preview to the developer and call again with confirm=true to actually send.
    Pushing is an outward action (leaves the machine), so it is confirmed by default; a developer
    who set skip_confirm in config.toml bypasses the preview."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "Skipped — repo path not detected."
    from contexer import config as _config

    profile = _config.load_profile()  # loaded once, reused by the preview and the push
    from contexer.remote import RemoteStore

    # Safe-by-default: a personal-cloud push is OUTWARD (the decision leaves the machine and may
    # be cached/indexed even if later deleted). Preview only when a push could ACTUALLY happen —
    # the SAME configured/authenticated check as the push path (team mode + endpoint + a resolvable
    # token), so we never advertise a push that would no-op. Otherwise share() reports the
    # not-configured result itself. This pushes nothing; from_profile may refresh an expired token
    # exactly as the push would, but sends no decision. confirm=True / skip_confirm bypass the gate.
    if not confirm and not profile.skip_confirm and RemoteStore.from_profile(profile) is not None:
        return store.format_share_preview(resolved, decision_id, profile=profile)

    from contexer import share as _share

    ids = [i.strip() for i in decision_id.split(",") if i.strip()]  # multi-select support

    # This is the ONE MCP tool that reaches the network from inside FastMCP's event loop, so
    # it AWAITS the async-native share path (share_ids_async -> RemoteStore.apush_decision ->
    # awaited httpx transport) rather than calling the blocking sync share() inline (which,
    # since asyncio.run can't run inside a running loop, previously failed outright and
    # misreported "endpoint unreachable"). The loop stays free for every other tool.
    #
    # Bounded by _SHARE_TIMEOUT so a wedged transport can't hang the tool call. Because the
    # push is AWAITED (not offloaded to an un-cancellable worker thread), wait_for CANCELS it
    # on timeout: the cancellation propagates into the async transport and closes the socket,
    # so nothing lingers in the background (#108). share_ids_async is local-first + outbox-
    # backed, so a false trip is harmless — the decision is saved and the outbox retries it.
    try:
        return await asyncio.wait_for(
            _share.share_ids_async(resolved, ids, profile=profile),
            timeout=_SHARE_TIMEOUT,
        )
    except TimeoutError:
        # The awaited push was cancelled at the deadline. Cancellation bypasses share_async's
        # own enqueue-on-failure, so queue the selection here (off the loop) to make the
        # "outbox retries it" promise real — idempotent, so re-queuing an already-sent id is
        # safe. Best-effort: the local decision is unchanged and re-shareable regardless.
        try:
            await asyncio.to_thread(_share.enqueue_ids_for_retry, resolved, ids)
        except Exception:
            pass
        return (
            f"Saved locally — the team cloud did not respond within {int(_SHARE_TIMEOUT)}s. "
            "The push was cancelled and the outbox retries it automatically; "
            "your local decision is unchanged."
        )


@mcp.tool()
def bootstrap_context(repo_path: str = "", insight: str = "", apply: bool = True) -> str:
    """Detected facts and measured conventions are stored automatically (idempotent —
    re-calls skip already-known items); the result carries 'stored'/'pending'/'skipped'
    counts plus any residual gap questions ('pending' items await `contexer review`).
    Set apply=false for a read-only preview that stores nothing.

    Scans a repo for inferable decisions and gap questions, filtered by how much
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
    result = (store.bootstrap_apply(resolved, SESSION_ID, insight) if apply
              else store.bootstrap_scan(resolved, insight))
    # Ask-shape rides the result, not the session-start injection: it is only usable when
    # there are gaps to ask, and this is the one place the model reads them.
    if result.get("gaps"):
        result = {**result, "how_to_ask": store.GAP_ASK_GUIDE}
    return json.dumps(result, indent=2)


@mcp.tool()
def capture_user_constraint(prompt: str, repo_path: str = "") -> str:
    """Called on every UserPromptSubmit. Detects prescriptive directives ('always X', 'never Y',
    'from now on Z') and stores them as constraint or convention decisions automatically."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return ""
    near: list = []
    entry_id, content, status = store.capture_user_constraint(resolved, prompt, SESSION_ID, near)
    if entry_id is None:
        return ""
    return store.constraint_ack(content, status, entry_id, near)


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
def update_global_context(content: str, subtype: str = "", title: str = "") -> str:
    """Stores a cross-cutting rule in the global store — applies to ALL repos.

    Use this only for constraints or conventions that genuinely apply everywhere:
    e.g. "always use conventional commits", "never commit untested code".
    Do NOT use for repo-specific decisions — use update_context instead.

    subtype: constraint | convention (defaults to convention if omitted)
    title: Provide a concise, one-line, imperative title (<= 100 chars) summarizing the rule.
           Only omit it when you can't summarize better than the content itself; the store
           then derives one from `content`.
    """
    lint = store.capture_lint(content, created_by="ai", replace_id="")
    if lint:
        # capture_lint's bounce text names update_context (the common case) — retarget it
        # here so a restated GLOBAL rule gets re-submitted globally, not filed repo-scoped.
        return lint.replace("update_context", "update_global_context")
    stored, entry_id = store.update_global_decision(content, SESSION_ID, subtype, title=title)
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
