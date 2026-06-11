import json
import uuid
from mcp.server.fastmcp import FastMCP
from contexer import store

SESSION_ID = str(uuid.uuid4())
mcp = FastMCP("contexer")


@mcp.tool()
def capture_context(description: str, repo_path: str = "") -> str:
    """Called at the start of every task. Captures the developer's task description for the given repo."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "Skipped — repo path not detected."
    entry_id = store.capture_task(resolved, description, SESSION_ID)
    if entry_id is None:
        return "Skipped — does not look like a task description."
    return f"Captured. id={entry_id}"


@mcp.tool()
def update_context(content: str, repo_path: str = "", subtype: str = "") -> str:
    """Called when Claude Code makes a significant decision mid-task. The server filters before storing.

    subtype: optional classification for filtered retrieval — architecture | constraint | pattern | convention
    """
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "Skipped — repo path not detected."
    stored, entry_id = store.update_decision(resolved, content, SESSION_ID, subtype)
    if stored:
        return f"Stored. id={entry_id}"
    return "Filtered — did not meet storage criteria."


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
    mcp.run()


if __name__ == "__main__":
    main()
