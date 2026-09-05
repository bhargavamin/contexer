"""One declaration of every file Contexer keeps in ``STORE_DIR``, and how long it may live.

Why this exists. Every kind of file below was built ad hoc by 63 functions across 12 modules,
while the cleanup sweep carried its OWN hand-kept list of four glob patterns in an unrelated
function. Nothing tied the two together, so adding a kind opted it out of cleanup by default
and nobody found out: measured on one real machine, ``~/.contexer`` held 115 files across 39
distinct name shapes, and the per-repo caches for repos never reopened again (``.team_*``,
``.memory_synced_*``, ``.insight_*``, ``.anchor_verify_*``) accumulated indefinitely.

What it owns, and what it does NOT. This module owns the LIFETIME question and the NAME: which
files may be deleted when they go cold, which must never be touched, and what each one is
called. It does not own WHERE they live, and it never will: staying a pure leaf with no imports
from the package is what lets `store.py` import it.

`store.sidecar_path(kind, **fields)` is the one function that joins a DECLARED name onto the
store directory, `glob_for` renders the pattern for callers that sweep a whole family, and
`store.store_dir()` is the one that reads the directory. `tests/test_store_dir_seam.py` holds
all of it, because a module that builds the path by hand still works, so the erosion is
invisible.

Two things in the store directory are deliberately outside this declaration, and both are
someone else's to own. `config.toml` is the developer's own hand-edited settings file, owned
by `config.py`. The `evidence/` spool root is a directory whose tree `spool.py` owns; its name
is also this package's own module name, so declaring it would make the literal scan below
report seven false offenders and stop meaning anything.

The safety direction is asymmetric and the default is chosen accordingly. Failing to sweep a
cache costs disk. Sweeping a durable file costs the developer's data: a queued share, a
credential, an explicit guard dismissal, a decision store. So ``DURABLE`` is the default for
anything whose loss is not merely a recomputation, and a kind must state a lifetime to become
sweepable. A file matching NO declared kind is never swept either, which is what stops a
future ``.something_new`` from being deleted by a glob it was never meant to match.
"""
import fnmatch
import re
from typing import NamedTuple


DURABLE = None          # never swept: losing it loses work, not a cache
SESSION = 7 * 24 * 3600     # per-session bookkeeping; the session is long over
COLD_REPO = 30 * 24 * 3600  # per-repo cache for a repo nobody has opened in a month


class Kind(NamedTuple):
    """One kind of file: what it is called, how long it lives, and why.

    `template` is the single source of truth for the name. The sweep's glob is DERIVED from
    it rather than written beside it, because a name and a glob maintained separately drift:
    the first version of this module declared `.reconcile_outbox.json` while share.py actually
    writes `.reconcile-outbox.json`, so the durable listing did not cover the real file. The
    round-trip test (every template renders to a name that classifies back to this kind) is
    what makes that class of mistake unrepresentable.
    """
    name: str
    template: str
    lifetime: int | None
    why: str

    @property
    def glob(self) -> str:
        """The template with every field replaced by `*`.

        A template that STARTS with a field gets `[!.]*` rather than `*`, because fnmatch
        does not exempt a leading dot the way shell globbing does: a bare `*.json` for the
        decision stores also matched `.ws_*.json` and `.team_*.json` and silently made every
        sidecar durable.

        Three lock slugs DO begin with a dot (share.py passes '.outbox' and '.shared',
        auth.py '.team_auth'), so the anchor is not universally safe and those names have
        their own `dotted_lock` row. An earlier version of this docstring claimed no slug
        ever starts with a dot, which is why they went undeclared.
        """
        out = re.sub(r"\{[a-z_]+\}", "*", self.template)
        return "[!.]" + out if self.template.startswith("{") else out


KINDS: tuple[Kind, ...] = (
    # ── durable: the developer's own data and queued work ──────────────────────────────
    Kind("store",            "{slug}.json",                DURABLE,   "a decision store; _global.json is "
                                                                      "this kind too, with slug=GLOBAL_SLUG"),
    Kind("deleted",          "{slug}.deleted.json",        DURABLE,   "tombstones; restore_deleted reads them"),
    Kind("migrated",         "{slug}.json.migrated",       DURABLE,   "a folded worktree stray, kept deliberately"),
    Kind("lock",             "{slug}.lock",                DURABLE,   "flock target; unlinking under a holder is unsafe"),
    Kind("dotted_lock",      ".{slug}.lock",               DURABLE,   "the same, for the three lock slugs that "
                                                                      "already start with a dot: share.py passes "
                                                                      "'.outbox' and '.shared', auth.py '.team_auth'"),
    Kind("repo_pointer",     ".current_repo",              DURABLE,   "shared pointer, last resort in resolve_repo"),
    Kind("pending_review",   ".pending_review_{slug}",     DURABLE,   "normally consumed by the next prompt, but "
                                                                      "ONLY a new pending decision re-arms it, so "
                                                                      "expiring it loses the nudge for a decision "
                                                                      "that is still waiting"),
    Kind("outbox",           ".outbox.json",               DURABLE,   "queued shares waiting for the team cloud"),
    Kind("reconcile_outbox", ".reconcile-outbox.json",     DURABLE,   "queued reconciliations; note the hyphen"),
    Kind("shared_markers",   ".shared.jsonl",              DURABLE,   "which decisions went where; drives the shared tick"),
    Kind("team_creds",       ".team_auth.json",            DURABLE,   "the team bearer token"),
    Kind("guard_dismissed",  ".guard_dismissed_{slug}.json", DURABLE, "explicit per-pair human dismissals, permanent"),
    Kind("reconcile_lock",  ".reconcile_{slug}.lock",      DURABLE, "flock target for the evidence consumer"),
    # Machine-global, not per-repo: the budget is "tell them once per RELEASE", and a
    # slug-keyed flag fires once per repo per release instead. Durable rather than a cache
    # because it holds which version the developer was already told about, and losing that
    # re-announces a release they have already seen - noise, in the one feature whose whole
    # design constraint is not being noisy. Re-fetching the version costs one request.
    Kind("update_check",     ".update_check.json",         DURABLE,   "latest release seen, declared floor, and "
                                                                      "which of each the developer was told about"),
    Kind("share_policy",     ".team_share_policy_{slug}.json", DURABLE,
         "explicit human-approved automatic proposal policy for one repository"),
    Kind("proposal_outbox",  ".team-proposal-outbox.json", DURABLE,
         "global queue of stable automatic proposal intents awaiting delivery"),
    Kind("proposal_receipts", ".team-proposal-receipts.jsonl", DURABLE,
         "append-only proposal outcomes and activation baselines"),
    Kind("proposal_attention", ".team-proposal-attention.json", DURABLE,
         "proposal intents requiring explicit developer attention"),
    Kind("proposal_diagnostics", ".team-proposal-diagnostics.jsonl", DURABLE,
         "private size-bounded stderr sink for detached proposal uploader telemetry"),
    Kind("share_policy_lock", ".team_share_policy_{slug}.lock", DURABLE,
         "independent flock target for one repository proposal policy"),
    Kind("proposal_outbox_lock", ".team-proposal-outbox.lock", DURABLE,
         "independent flock target for proposal queue read-modify-write"),
    Kind("proposal_drainer_lock", ".team-proposal-drainer.lock", DURABLE,
         "non-blocking flock target ensuring at most one proposal uploader"),
    Kind("proposal_receipts_lock", ".team-proposal-receipts.lock", DURABLE,
         "independent flock target for receipt append and compaction"),
    Kind("proposal_attention_lock", ".team-proposal-attention.lock", DURABLE,
         "independent flock target for attention queue read-modify-write"),


    # ── session bookkeeping: the session it belonged to is over ────────────────────────
    Kind("working_set",      ".ws_{slug}_{session}.json",  SESSION,   "per-session working set; dedup is done"),
    Kind("retrieval_log",    ".retrieval_{slug}.jsonl",    SESSION,   "per-repo retrieval log, tail-capped"),
    Kind("reconcile_log",    ".reconcile_{slug}.jsonl",    SESSION,   "per-repo reconciliation receipt log, tail-capped"),
    Kind("bootstrap_offered", ".bootstrap_offered_{slug}", SESSION,   "once-per-session offer flag"),
    Kind("edited_files",     ".edited_{slug}.json",        SESSION,   "recent edits; freshness window is 30 min"),
    Kind("resume_mining",    ".resume_mining",             SESSION,   "legacy resume marker; no producer, retained for GC"),
    Kind("pending_capture",  ".pending_capture",           SESSION,   "post-write flag; consumed by the next prompt"),
    Kind("gemini_capture",   ".gemini_pending_capture",     SESSION,   "Gemini's own post-write flag, namespaced "
                                                                      "so it cannot collide with Claude's"),
    Kind("gemini_reload",    ".gemini_pending_reload",      SESSION,   "Gemini's post-compression reload flag"),
    # Declared but NOT routed through `filename`: ui/daemon.py confines its module-level imports
    # to a small stdlib set because importing contexer.store on the SessionStart path costs a
    # measured 134ms against a ~0.3ms warm budget, and a test enforces that allowlist. So the
    # daemon keeps its own literals; these rows exist so the sweep knows the files rather than
    # treating them as unknown, which is the state that hid four earlier declaration errors.
    Kind("console_state",    "ui.json",                    DURABLE,   "console daemon statefile: port, pid, token"),
    Kind("console_log",      "ui.log",                     DURABLE,   "console daemon log; the daemon may hold it open"),

    Kind("gemini_first",     ".gemini_first_prompt_{slug}", SESSION,  "Gemini's first-prompt marker"),
    Kind("team_pending",     ".team_pending_{slug}.json",  SESSION,   "legacy pending file, dropped on first poll"),

    # ── cold-repo caches: recomputed by the next session that needs them ──────────────
    Kind("team_cache",       ".team_{slug}.json",          COLD_REPO, "team decisions; re-pulled on next sync"),
    Kind("team_seen",        ".team_seen_{slug}_{consumer}.json", COLD_REPO,
                                                                     "per-consumer high-water mark; re-inits to head"),
    Kind("memory_synced",    ".memory_synced_{slug}",      COLD_REPO, "memory-import fingerprint; one re-import"),
    Kind("insight",          ".insight_{slug}",            COLD_REPO, "legacy familiarity cache; no producer, retained for GC"),
    Kind("anchor_verify",    ".anchor_verify_{slug}",      COLD_REPO, "anchor-verification TTL stamp"),
    Kind("miner_verify",     ".miner_verify_{slug}",       COLD_REPO, "scan-convention TTL stamp"),
    Kind("spool_maintained", ".spool_maintained_{slug}",   COLD_REPO, "spool retention/orphan-sweep TTL "
                                                                      "stamp; same shape as the two above"),
    Kind("guard_advised",    ".guard_advised_{slug}.json", COLD_REPO, "guard throttle stamps, content-keyed"),
    Kind("retrieval_index",  ".retrieval_index_{slug}.json", COLD_REPO, "BM25 index; disposable by design and "
                                                                      "rebuilt by ensure_retrieval_index at the "
                                                                      "next session start that needs it"),
)

_BY_NAME = {k.name: k for k in KINDS}

# `.team_auth.json` also matches the sweepable `.team_{slug}.json`, and the credentials file
# must win, so every durable glob is consulted before any sweepable one. Longest-glob-first
# would not do it (`.team_seen_*.json` is longer than `.team_auth.json`).
#
# Order still decides between two SWEEPABLE kinds, and one pair relies on it:
# `.team_pending_x.json` matches both `.team_pending_*.json` (SESSION) and `.team_*.json`
# (COLD_REPO), and SESSION wins only because it is declared first. A test pins that, so
# reordering these rows fails rather than quietly quadrupling a lifetime.
_DURABLE_GLOBS = tuple(k.glob for k in KINDS if k.lifetime is DURABLE)


def filename(kind: str, **fields: str) -> str:
    """The file name for `kind`, e.g. `filename("insight", slug="Users_me_proj")`.

    `store.sidecar_path` is the one caller that joins the result onto the store directory.
    `cli.py` calls this directly at three sites, because `contexer status` is a diagnostic
    that must resolve against the home it was asked to inspect rather than the process-wide
    one; a test allowlist names them. This module stays a pure leaf with no imports from the
    package: it knows what files are CALLED and how long they live, never where the store
    directory is or how a repo path becomes a slug, which is store's job.
    """
    try:
        return _BY_NAME[kind].template.format(**fields)
    except KeyError as exc:
        raise KeyError(f"unknown sidecar kind or missing field: {kind} {fields}") from exc


def glob_for(kind: str) -> str:
    """The fnmatch pattern that matches every file of `kind`, e.g. `.team_*.json`.

    The companion to `filename`, for the callers that sweep a family rather than address one
    file. It exists because `team_context` spelled `.team_*.json` as a literal directly below
    a comment reading "Ask the declaration, not a literal": renaming a kind would have left
    that sweep matching the old shape, silently.
    """
    try:
        return _BY_NAME[kind].glob
    except KeyError as exc:
        raise KeyError(f"unknown sidecar kind: {kind}") from exc


def lifetime_for(name: str) -> int | None:
    """Seconds a file named `name` may live once cold, or None if it must never be swept.

    Unknown names return None. That default is the whole point: a kind nobody declared is a
    kind nobody reasoned about, and deleting it would be a guess.
    """
    for glob in _DURABLE_GLOBS:
        if fnmatch.fnmatch(name, glob):
            return None
    for kind in KINDS:
        if kind.lifetime is not DURABLE and fnmatch.fnmatch(name, kind.glob):
            return kind.lifetime
    return None
