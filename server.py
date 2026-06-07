import json
import uuid
from mcp.server.fastmcp import FastMCP
import store

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
def bootstrap_context(repo_path: str = "") -> str:
    """Scans a repo for inferable decisions and gap questions. Present inferred
    items to the user for confirmation, store confirmed ones via update_context,
    then ask the gap questions and store each answer."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return json.dumps({"error": "repo path not detected"})
    return json.dumps(store.bootstrap_scan(resolved), indent=2)


@mcp.tool()
def get_context_for_prompt(repo_path: str = "", prompt: str = "") -> str:
    """Auto-called by UserPromptSubmit hook on every prompt. Detects rationale/decision
    questions (why, reason, rationale, decided...) and injects matching stored decisions
    as additionalContext. Returns empty string for non-rationale prompts — silent no-op."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return ""
    return store.get_context_for_prompt(resolved, prompt)


if __name__ == "__main__":
    mcp.run()
