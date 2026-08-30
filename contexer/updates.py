"""Update delivery: how a developer finds out a new Contexer release exists.

The problem this solves. A version check already existed and surfaced only in `contexer
status`, a command almost nobody runs, so releases with important fixes went unnoticed. The
fix is not a louder check; it is a delivery path with a defined channel, a defined budget,
and state that survives between sessions.

Five constraints shape every line below, and each one kills an otherwise obvious design:

1. **No network call on the hook path.** `deliver()` reads one small file and nothing else.
   Fetching is done by `refresh()`, which only ever runs in a detached background process
   (`spawn_refresh`) or from an explicit CLI command.
2. **Never upgrade mid-session.** Nothing here mutates an install. `contexer upgrade` is a
   user-invoked command in the CLI, never a side effect of a hook.
3. **No new runtime dependency.** stdlib only, like `redact.py` and `miner.py`.
4. **Works airgapped.** `CONTEXER_NO_UPDATE_CHECK` disables the whole module, and every
   failure path degrades to silence rather than to an error.
5. **No telemetry.** The only outbound call is the anonymous PyPI JSON endpoint the version
   check already used. Nothing about the user is sent.

The budget is two rules, not one. A given release is announced at most once (keyed on the
version), AND no two routine notices land within `MIN_NOTICE_INTERVAL`. The second rule is what
stops a burst of releases becoming a burst of notices: see its comment for the measurement and
for when to re-tune it.

Why the state file is machine-global rather than per-repo. Neither budget rule can be expressed
per repo: a slug-keyed flag fires once per repo per release, so a developer with eight repos
gets eight notices, and a per-repo interval would let eight repos each spend their own. The version is a property of the binary, not of a repo,
so its state is too. Seven machine-global durables already live in `sidecars.py` as precedent.
With several sessions running at once the first writer wins and the rest stay silent, which
is what "once per release" means, not a race to paper over.

Why the floor is a `Project-URL`. PyPI has no equivalent of `npm deprecate pkg@"<0.2.3"`: it
offers whole-release yank and whole-project status and nothing in between, so a rolling
minimum-supported-version is a convention Contexer invents rather than a field it fills in.
A `Project-URL` entry rides metadata the check already downloads, needs no second endpoint
(constraint 5 stays intact), and moves forward exactly when a release is cut. Absent key
means no floor, which is the state of the world today.

What this module does NOT own. The rendering channel per host is the adapters' concern
(`notify()` on each adapter module, returning None where the host has no user-facing
channel), and the upgrade command itself lives in `cli.py` next to the other commands. This
module owns the FACT (what version exists, what the floor is) and the POLICY (whether the
developer should be told right now), and nothing about how the words reach a screen.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from importlib.metadata import PackageNotFoundError, distribution, version as _dist_version
from pathlib import Path
from typing import NamedTuple

from contexer import sidecars

PYPI_JSON_URL = "https://pypi.org/pypi/contexer/json"

# The fetch is short because it runs while a developer waits for something else, even when
# detached: a wedged socket holding a background process open for minutes is a worse failure
# than missing one day's check.
FETCH_TIMEOUT = 3

# How long a fetched state stays fresh. A release is not urgent news, and a shorter window
# buys nothing but requests. This happens to equal MIN_NOTICE_INTERVAL below and is unrelated
# to it: this one bounds how stale the FACT may be, that one how often we may SPEAK. Do not
# collapse them into one constant.
TTL_SECONDS = 24 * 3600

# The minimum gap between two routine notices, whatever the release cadence.
#
# Why a floor exists at all: "once per release" is only a budget if releases are occasional.
# Measured on this repo, 48 releases landed in the 90 days to 2026-08-30, one every 1.9 days,
# and two landed on a single day more than once. Without a time floor a burst day means two
# notices in one day, which is the nagging the silent-operation constraint exists to prevent.
#
# Why 24 hours and not longer: that cadence is an artefact of active development and is
# expected to fall (developer's call, 2026-08-30). At one release every 1.9 days a 24h floor
# binds only on a same-day burst, so today it is close to one notice per release; once releases
# are a few days apart it never binds at all and this constant costs nothing. Raise it if the
# cadence stays high and the notice starts to feel frequent - that is the signal to re-tune,
# and this comment is where the reasoning lives.
#
# The limit is on TIME rather than version distance. Version-distance rules (skip patch bumps,
# wait for N releases) were rejected on the data: almost every bump in the measured window was
# a MINOR one, so filtering patches would have removed two notices out of nine.
#
# A suppressed notice is NOT consumed: it stays owed and fires once the gap has passed, naming
# whatever the latest release is by then. So a developer is at most this far behind the news,
# and never sees more than one line about it in the window.
#
# A floor breach ignores this entirely. A floor is declared only when running an older build is
# a real risk, and that is worth interrupting for.
MIN_NOTICE_INTERVAL = 24 * 3600

# `project_urls` keys are human-written display labels, so match them normalized rather than
# byte-exact: "Minimum supported version", "Minimum-Supported-Version" and "minimum supported
# version" are the same intent and a floor that silently fails to parse is worse than no floor.
_FLOOR_KEY = "minimumsupportedversion"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# The floor value is a release-tag URL, so the version is read off the tag rather than stored
# as a bare string: the link has to lead somewhere that explains WHY the floor moved, and a
# bare version in metadata explains nothing.
_TAG_RE = re.compile(r"/tag/v?([0-9]+(?:\.[0-9]+)*)/?$")


def disabled() -> bool:
    """True when the developer has opted out. Checked at every entry point, not just the
    fetch: on an airgapped box the notice is noise even if a stale state file could produce
    one."""
    return bool(os.environ.get("CONTEXER_NO_UPDATE_CHECK"))


def version_tuple(v: str | None) -> tuple | None:
    """A comparable tuple for a plain numeric version, or None when it is not one.

    Deliberately strict. A pre-release or local build ("0.42.0rc1", "0.42.0+dev") returns
    None and therefore never triggers a notice, which is the safe direction: telling someone
    running a dev build to "upgrade" to the release they are already ahead of is worse than
    saying nothing.
    """
    if not v:
        return None
    try:
        return tuple(int(p) for p in v.split("."))
    except (ValueError, AttributeError):
        return None


def installed_version() -> str | None:
    """The version of the wheel this process runs from, or None when it is not installed as
    a package (a bare source tree), in which case there is nothing to compare against."""
    try:
        return _dist_version("contexer")
    except PackageNotFoundError:
        return None


# ── the fact: what PyPI says ─────────────────────────────────────────────────────────────

def _floor_from_project_urls(project_urls: object) -> str | None:
    """The declared minimum supported version, or None when no floor is published.

    Absent key means no floor. That is the normal state of the world, so a missing or
    malformed entry must read as "no floor" rather than as an error: a metadata typo must
    never be able to tell every user their install is unsupported.
    """
    if not isinstance(project_urls, dict):
        return None
    for label, url in project_urls.items():
        if not isinstance(label, str) or not isinstance(url, str):
            continue
        if _NON_ALNUM.sub("", label.lower()) != _FLOOR_KEY:
            continue
        match = _TAG_RE.search(url.strip())
        if match and version_tuple(match.group(1)):
            return match.group(1)
        return None
    return None


def fetch() -> dict | None:
    """Ask PyPI what the latest release is and whether a floor is declared.

    Returns `{"latest": str, "floor": str | None}`, or None on any failure. Never raises:
    every caller is either a background process nobody is watching or a hook that must not
    care. Only ever called off the hook path.
    """
    if disabled():
        return None
    try:
        with urllib.request.urlopen(PYPI_JSON_URL, timeout=FETCH_TIMEOUT) as resp:
            info = json.load(resp).get("info") or {}
        latest = info.get("version")
        if not isinstance(latest, str) or not version_tuple(latest):
            return None
        return {"latest": latest, "floor": _floor_from_project_urls(info.get("project_urls"))}
    except Exception:
        return None


# ── the state file ───────────────────────────────────────────────────────────────────────

def state_path() -> Path:
    """Location of the machine-global state file.

    `store` is imported inside the function, as the module object, for the two reasons the
    codebase already documents: importing it eagerly costs measurable milliseconds on paths
    that may not need it, and a test monkeypatching `contexer.store.STORE_DIR` is only
    visible at the call site through the module-object form.
    """
    from contexer import store
    return store.STORE_DIR / sidecars.filename("update_check")


def read_state() -> dict:
    """The stored state, or an empty dict.

    A corrupt or unreadable file reads as empty. This is a render path, not a write path
    holding queued work: the worst case is one redundant fetch and possibly one repeated
    notice, so treating "unreadable" as "absent" is safe here in a way it would not be for
    the share outbox.
    """
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: dict) -> bool:
    """Persist the state atomically. False when it could not be written.

    Inability to write is not an error the caller should act on: on a host where
    `~/.contexer` is read-only the notice simply repeats, which is the mild failure, while
    raising here would break whatever hook was merely trying to be helpful.
    """
    from contexer import store
    try:
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        store.atomic_write(state_path(), json.dumps(state))
        return True
    except (OSError, TypeError, ValueError):
        return False


def is_stale(state: dict | None = None) -> bool:
    """Whether the state is old enough to be worth re-fetching. Missing state is stale."""
    state = read_state() if state is None else state
    checked = state.get("checked_at")
    if not isinstance(checked, (int, float)):
        return True
    # A clock that has moved backwards (a restored machine, a VM snapshot) would otherwise
    # pin the state as fresh forever, so a future timestamp counts as stale too.
    return not (0 <= time.time() - checked < TTL_SECONDS)


def refresh(force: bool = False) -> dict:
    """Re-fetch and store, unless the state is still fresh. Returns the state as it stands.

    `checked_at` advances even when the fetch FAILS, and that is the point rather than an
    oversight: without it an offline machine re-spawns a doomed refresher on every single
    prompt forever. One attempt per TTL, offline or not.
    """
    if disabled():
        return {}
    state = read_state()
    if not force and not is_stale(state):
        return state
    fetched = fetch()
    state["checked_at"] = time.time()
    if fetched:
        state["latest"] = fetched["latest"]
        # Assign unconditionally: a floor that is WITHDRAWN upstream must disappear here too,
        # or a machine would stay permanently breached against a rule that no longer exists.
        state["floor"] = fetched["floor"]
    write_state(state)
    return state


def spawn_refresh() -> None:
    """Start a detached background refresh if, and only if, the state is stale.

    This is the whole answer to "no network on the hook path": the hook pays one stat and a
    fork, and the answer arrives in time for some later prompt. Nothing waits for it, and a
    failure to spawn is silently fine.
    """
    if disabled() or not is_stale():
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "contexer.updates"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


# ── the policy: should the developer be told, right now ──────────────────────────────────

def _routine_text(latest: str, installed: str) -> str:
    return (f"Contexer {latest} is available (you have {installed}). "
            f"Run `contexer upgrade` to update.")


def _breach_text(floor: str, installed: str) -> str:
    return (f"Contexer {installed} is below the minimum supported version {floor} "
            f"and may not behave correctly. Run `contexer upgrade`.")


def _due(state: dict, installed: str | None) -> tuple[str, dict] | None:
    """The notice owed right now as `(text, fields to record once it is delivered)`, or None.

    The single decision point. `notice` and `deliver` differ only in WHEN they write the
    fields back, never in what is owed, so the two can never drift into disagreeing about
    whether something has already been said.

    Two things can be owed and only one may be said, because a floor breach competing with a
    routine availability notice reads as less urgent than it is. The breach wins outright.

    Both are fire-once, keyed differently and deliberately so: the routine notice is keyed on
    the version it announces, so each release is announced once; the breach is keyed on the
    FLOOR, so it repeats only when the floor itself moves. Keying the breach on the release
    would re-announce it on every release; not keying it at all would fire it on every prompt
    until the developer upgraded, which is the nagging the silent-operation constraint exists
    to prevent.
    """
    installed_t = version_tuple(installed)
    if not installed_t:
        return None

    floor = state.get("floor")
    floor_t = version_tuple(floor) if isinstance(floor, str) else None
    latest = state.get("latest")
    latest_t = version_tuple(latest) if isinstance(latest, str) else None

    if floor_t and installed_t < floor_t and state.get("notified_floor") != floor:
        marks = {"notified_floor": floor, "notified_at": time.time()}
        # "Replaces" has to hold ACROSS turns, not just within one. Marking the pending
        # release as announced too is what stops the breach on this prompt from being
        # followed by a routine "an update is available" on the next one, which would deliver
        # the same instruction a second time in a weaker voice.
        if isinstance(latest, str):
            marks["notified"] = latest
        return _breach_text(floor, installed), marks

    if latest_t and latest_t > installed_t and state.get("notified") != latest:
        last = state.get("notified_at")
        if isinstance(last, (int, float)) and 0 <= time.time() - last < MIN_NOTICE_INTERVAL:
            return None          # owed, but not yet due: see MIN_NOTICE_INTERVAL
        return _routine_text(latest, installed), {"notified": latest,
                                                  "notified_at": time.time()}
    return None


def deliver(render):
    """Hand the owed notice to `render`, and record it as said ONLY if `render` delivered it.

    This ordering is the whole contract. `render` is an adapter's `notify`, which returns None
    on a host with no user-facing channel, and consuming before rendering would silently burn
    the notice on exactly those hosts: the developer would never be told, and the terminal
    backstop would find nothing left to say. So the seam is consulted first and the state is
    written second.

    Does NOT start the background refresh: the caller does that, because a hook may decide it
    has something better to say this prompt and never reach here, and the state must still be
    kept warm on those prompts. The hot path is one file read; no fetch ever happens here.
    """
    if disabled():
        return None
    state = read_state()
    due = _due(state, installed_version())
    if not due:
        return None
    text, marks = due
    rendered = render(text)
    if rendered is None:
        return None                       # this host cannot show it; leave it owed
    state.update(marks)
    write_state(state)
    return rendered


# ── install-method detection, for `contexer upgrade` ──────────────────────────────────────

UV_TOOL = "uv-tool"
UV_TOOL_SOURCE = "uv-tool-source"
PIP = "pip"
UNKNOWN = "unknown"


def _direct_url(dist) -> str:
    """The PEP 610 `direct_url.json` URL recorded for `dist`, or "" when there is none.

    The file is located through `dist.files` and read as an ordinary path rather than through
    `Distribution.read_text`, which takes no encoding argument. The repo pins the encoding on
    every text read for a reason (a locale-default decode is ASCII under `LC_ALL=C`), and an
    API that cannot be pinned has no business on that path when a pinnable one exists.
    """
    try:
        for entry in dist.files or ():
            if entry.name == "direct_url.json":
                raw = Path(dist.locate_file(entry)).read_text(encoding="utf-8")
                return (json.loads(raw) or {}).get("url") or ""
    except Exception:
        return ""
    return ""


def install_method() -> tuple[str, str]:
    """How this copy of Contexer was installed, as `(method, detail)`.

    The distinction that matters is `uv-tool-source`: a clone installed with
    `uv tool install --from <path>` looks exactly like a normal uv tool install from the
    outside, and running `uv tool upgrade contexer` against it would silently replace a
    developer's working tree build with a PyPI release. PEP 610's `direct_url.json` is what
    tells them apart, and it is written by the installer rather than inferred, so it is the
    trustworthy signal rather than a path-shape guess.

    `detail` carries the source directory for `uv-tool-source` and the install location
    otherwise. Any failure to introspect returns `UNKNOWN`, whose handling is to print
    guidance rather than to run anything.
    """
    try:
        dist = distribution("contexer")
        location = str(dist.locate_file(""))
    except Exception:
        return UNKNOWN, ""
    url = _direct_url(dist)
    if url.startswith("file://"):
        return UV_TOOL_SOURCE, url[len("file://"):]
    # `uv tool` keeps each tool in its own environment under a `uv/tools/<name>` directory.
    # Checking the path is a heuristic, but only ever selects BETWEEN upgrade commands, never
    # decides whether to run one, and the source case above is already excluded by then.
    if os.path.join("uv", "tools") in location:
        return UV_TOOL, location
    return PIP, location


class UpgradePlan(NamedTuple):
    """What to do about an install of a given kind.

    Named rather than a bare 3-tuple, following `share_status.py`: a caller reads
    `plan.runnable` instead of remembering which slot holds the boolean.
    """
    runnable: bool      # may Contexer run this for the developer, or only print it
    command: str        # the command that upgrades this kind of install
    why_not: str        # why it is not run for them; empty when it is


def upgrade_plan(method: str, detail: str = "") -> UpgradePlan:
    """`(runnable, command, why_not)` for an install of this kind.

    One cascade over the method, in one place. An earlier version split this across two
    functions in two modules, so "which command" and "may we run it" could disagree about the
    same install. `runnable` is False whenever running the command could destroy something the
    developer meant to keep, or would pick an environment that is theirs to choose.
    """
    if method == UV_TOOL:
        return UpgradePlan(True, "uv tool upgrade contexer", "")
    if method == UV_TOOL_SOURCE:
        script = Path(detail) / "scripts" / "install.sh" if detail else Path("scripts/install.sh")
        return UpgradePlan(False, f"bash {script}", (
            f"This copy was installed from source at {detail}, not from PyPI. "
            "Upgrading from PyPI would replace your own build, so it is not done for you."))
    if method == PIP:
        return UpgradePlan(False, f"{Path(sys.executable).name} -m pip install --upgrade contexer", (
            f"This copy was installed with pip ({detail}). The target environment is yours to "
            "choose, so it is not upgraded for you."))
    return UpgradePlan(False, "uv tool install --reinstall contexer",
                       "Could not tell how this copy was installed, so it is not upgraded for you.")


if __name__ == "__main__":  # pragma: no cover - the detached refresher's entry point
    refresh()
