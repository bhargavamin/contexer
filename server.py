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
def update_context(content: str, repo_path: str = "") -> str:
    """Called when Claude Code makes a significant decision mid-task. The server filters before storing."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "Skipped — repo path not detected."
    stored, entry_id = store.update_decision(resolved, content, SESSION_ID)
    if stored:
        return f"Stored. id={entry_id}"
    return "Filtered — did not meet storage criteria."


@mcp.tool()
def get_context(repo_path: str = "") -> str:
    """Called at the start of every new session. Returns stored context for the current repository."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return "No repo path detected."
    return store.get_context(resolved)


@mcp.tool()
def bootstrap_context(repo_path: str = "") -> str:
    """Scans a repo for inferable decisions and gap questions. Present inferred
    items to the user for confirmation, store confirmed ones via update_context,
    then ask the gap questions and store each answer."""
    resolved = store._resolve_repo(repo_path)
    if not resolved:
        return json.dumps({"error": "repo path not detected"})
    return json.dumps(store.bootstrap_scan(resolved), indent=2)


if __name__ == "__main__":
    mcp.run()
