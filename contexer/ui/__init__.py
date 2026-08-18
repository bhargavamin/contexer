"""Local console: a loopback web UI over the decision store.

Import-light on purpose - `contexer.ui` is reached from the SessionStart hook path, so only
`daemon` (processes and sockets, no store) is imported here. `server` and `api` pull in
http.server and the store; import those modules directly, from the daemon process.
"""
from contexer.ui.daemon import (
    console_url,
    ensure_running,
    pairing_code,
    status,
    stop,
)

__all__ = ["console_url", "ensure_running", "pairing_code", "status", "stop"]
