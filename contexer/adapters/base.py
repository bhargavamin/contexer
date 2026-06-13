"""Adapter contract for one AI-assistant integration target.

An adapter is a *module* (duck-typed, no class needed) that exposes:

  NAME: str                       # "claude" | "cursor"
  is_present(home: Path) -> bool  # does this tool look installed for the user?
  install(home: Path) -> list[str]    # wire MCP + hooks; return human-facing log lines
  uninstall(home: Path) -> list[str]  # remove MCP + hooks; return log lines
  status_lines(home: Path) -> list[str]  # diagnostic lines for `contexer status`

Plus hook entrypoints called from the hook command strings, each returning the
JSON string to print on stdout (never raises — hooks must not crash the host).

Shared config-file helpers (_load/_save/marker checks) land here in a later task,
when the Claude install logic moves out of cli.py.
"""
