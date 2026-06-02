import uuid
from mcp.server.fastmcp import FastMCP
import store

SESSION_ID = str(uuid.uuid4())
mcp = FastMCP("contexer")


@mcp.tool()
def capture_context(repo_path: str, description: str) -> str:
    """Called at the start of every task. Captures the developer's task description for the given repo."""
    entry_id = store.capture_task(repo_path, description, SESSION_ID)
    return f"Captured. id={entry_id}"


@mcp.tool()
def update_context(repo_path: str, content: str) -> str:
    """Called when Claude Code makes a significant decision mid-task. The server filters before storing."""
    stored, entry_id = store.update_decision(repo_path, content, SESSION_ID)
    if stored:
        return f"Stored. id={entry_id}"
    return "Filtered — did not meet storage criteria."


@mcp.tool()
def get_context(repo_path: str) -> str:
    """Called at the start of every new session. Returns stored context for the current repository."""
    return store.get_context(repo_path)


if __name__ == "__main__":
    mcp.run()
