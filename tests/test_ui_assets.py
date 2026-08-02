"""Static guards over the console's shipped assets.

The console has no build step and no test runner of its own, so the only thing standing between
`contexer/ui/assets/console.js` and a browser is this file. These are deliberately *structural*
checks — they read the source and assert the shapes that four verified defects all came from:

* a handler that throws away the developer's typed draft BEFORE the request it depends on,
* a refusal to send that is indistinguishable from a successful write,
* a 409 whose job id is dropped, leaving a tab watching a signal that can never arrive,
* a selection that outlives the repo it was made in.

Each guard is written to FAIL against the code as it stood: they were run against a reconstructed
pre-fix copy of console.js and every one of them fired.
"""
import re
from pathlib import Path

import pytest

from contexer.ui import api

ASSETS = Path(api.__file__).parent / "assets"
SCRIPT = ASSETS / "console.js"
MARKUP = ASSETS / "index.html"
STYLES = ASSETS / "console.css"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text()


# --- source-shape helpers --------------------------------------------------------------

def _block_at(text: str, open_brace: int) -> str:
    """The braced block starting at `open_brace`, matched naively.

    Naive is sound here because a guard is only useful if it also *breaks* when the source stops
    looking like itself: `_test_the_block_reader_actually_reads_blocks` pins that down.
    """
    assert text[open_brace] == "{", text[open_brace - 40:open_brace + 10]
    depth = 0
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace:i + 1]
    raise AssertionError("unbalanced braces from offset %d" % open_brace)


def _code(text: str) -> str:
    """`text` with comments removed, for guards that assert an identifier is ABSENT — a comment
    explaining why it is absent must not read as the thing itself."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def _function_body(text: str, name: str) -> str:
    """The body of `function name(...) { ... }`."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", text)
    assert m, "no function %s in console.js" % name
    return _block_at(text, m.end() - 1)


def _click_handlers(text: str) -> list:
    """Every `click:` arrow-function body in the file."""
    out = []
    for m in re.finditer(r"click:\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", text):
        out.append(_block_at(text, m.end() - 1))
    return out


def test_the_block_reader_actually_reads_blocks(script):
    """A guard built on a broken reader is worse than no guard."""
    assert _block_at("x = { a: { b: 1 } } ;", 4) == "{ a: { b: 1 } }"
    with pytest.raises(AssertionError):
        _block_at("no brace here", 3)
    handlers = _click_handlers(script)
    assert len(handlers) >= 15, "expected the console's click handlers, found %d" % len(handlers)
    assert any("Save revision" in h or "state.edit = null" in h for h in handlers)
    assert any("/api/global" in h for h in handlers)
    assert any("/share" in h for h in handlers)


# --- F2: a draft must outlive a failed write -------------------------------------------

# Everything a handler can throw away that the developer cannot get back by hand: their typed
# edit, their typed global rule, and the selection they ticked box by box.
DISCARDS = (
    re.compile(r"state\.edit\s*=\s*null"),
    re.compile(r"globalDraft\.(content|title)\s*=\s*\"\""),
    re.compile(r"\bselected\.clear\(\)"),
    re.compile(r"state\.confirm\s*=\s*\"\""),
)


def test_no_click_handler_discards_state_before_the_write_it_depends_on(script):
    """`state.edit = null` before `await act(...)` is how a rewritten decision was lost to a 409.

    A handler may only discard the developer's work AFTER the daemon has taken it — which means
    inside `act`'s `onOk` callback, or behind `act`'s boolean. Anything that runs before the call
    runs for a request that may be rejected (409/400/429/500) or refused outright, and the console
    has no undo.
    """
    offenders = []
    for body in _click_handlers(script):
        call = body.find("act(")
        if call < 0:
            continue  # a Cancel/Clear button discards on purpose; it awaits nothing
        before = body[:call]
        for pattern in DISCARDS:
            m = pattern.search(before)
            if m:
                offenders.append((m.group(0), before.strip().splitlines()[-1].strip()))
    assert offenders == [], (
        "these handlers discard state before their act() call:\n  "
        + "\n  ".join("%s  (at: %s)" % o for o in offenders)
    )


def test_the_edit_conflict_branch_keeps_the_draft(script):
    """The 409 that means "an MCP session wrote this a second before you did" used to repaint the
    read-only pane over the text the developer had just typed. It must reload the version and
    leave the form standing."""
    body = _function_body(script, "act")
    m = re.search(r"err\.status === 409 && conflictVersion !== undefined\)\s*\{", body)
    assert m, "act() no longer has an edit-conflict branch"
    branch = _block_at(body, m.end() - 1)
    assert "state.edit" not in _code(branch), (
        "the 409 branch must not clear the open edit draft:\n" + branch
    )
    assert "await render()" in branch, "the 409 branch must still reload the decision"


# --- F5: "refused to send" must not read as "written" ----------------------------------

def test_mutate_throws_when_it_refuses_to_send_rather_than_returning_null(script):
    """`return null` on `state.busy` was indistinguishable from a 204's empty body, so a
    double-click reported a write that never left the browser."""
    body = _function_body(script, "mutate")
    assert re.search(r"if \(state\.busy\) throw new BusyError\(\);", body), body[:200]
    assert not re.search(r"if \(state\.busy\)\s*return\s+null", body), body[:200]
    assert re.search(r"function BusyError\(\)", script), "BusyError must be a distinct error type"


def test_act_reports_success_only_for_a_write_that_happened(script):
    """`else if (okMessage) toast(okMessage)` on a `null` return is what toasted "Approved." for a
    decision that was never approved. The busy branch must bail out before any success path."""
    body = _function_body(script, "act")
    busy = re.search(r"err instanceof BusyError\)\s*\{", body)
    assert busy, "act() must recognise a refusal to send"
    branch = _code(_block_at(body, busy.end() - 1))
    assert "okMessage" not in branch and "onOk" not in branch, branch
    assert "render()" not in branch, "a request that was never sent must not refetch:\n" + branch
    assert re.search(r"return\s+false", branch), "the busy branch must report failure:\n" + branch
    # A payload is not a success signal: a 204 answers with no body at all.
    assert not re.search(r"^\s*return out;", _code(body), re.MULTILINE), (
        "act() must return a boolean, not the payload"
    )
    assert re.search(r"return\s+true", body), "act() must have a success return"


def test_every_act_caller_that_navigates_away_waits_for_the_write(script):
    """Leaving the detail view told the developer a decision was deleted; a refused DELETE left it
    live and listed."""
    for body in _click_handlers(script):
        if "act(" in body and re.search(r"\bgo\(", body):
            assert re.search(r"if \(ok\b", body), (
                "a handler navigates away without checking act()'s result:\n" + body
            )


def test_the_share_button_never_clears_a_selection_it_did_not_send(script):
    body = _code(next(b for b in _click_handlers(script) if "/share" in b))
    assert "selected.delete" in body, (
        "the share handler must drop the ids it sent, one by one, on success:\n" + body
    )
    assert "selected.clear()" not in body, (
        "clearing the whole selection up front destroyed it for a request that was refused:\n"
        + body
    )
    assert body.index("act(") < body.index("selected.delete"), (
        "the selection must be dropped inside act()'s success callback, not before the call:\n"
        + body
    )


# --- F12: the 409 that names a job must be followed ------------------------------------

def test_the_login_conflict_branch_consumes_the_job_id_the_daemon_sends(script):
    """`api._login` puts the in-flight job's id in its 409 precisely so a second tab can learn the
    login FAILED. Dropping it left that tab watching /api/config — a channel that only ever reports
    success — until the 6-minute client backstop expired."""
    start = _function_body(script, "startLogin")
    m = re.search(r"err\.status === 409\)\s*\{", start)
    assert m, "startLogin no longer handles the single-flight 409"
    branch = _block_at(start, m.end() - 1)
    assert "err.data" in branch and "job" in branch, (
        "the 409 branch must read the job id out of the body:\n" + branch
    )
    assert "state.login.job" in branch, branch
    assert not re.search(r"state\.login\.attached\s*=\s*true\s*;", branch), (
        "attaching unconditionally throws the job id away:\n" + branch
    )
    # The fallback for an older daemon that names no job must still exist.
    assert re.search(r"state\.login\.attached\s*=\s*!job", branch), branch


def test_no_comment_still_claims_the_409_carries_no_job_id(script):
    """The branch used to assert this in a comment. It is false, and a false comment outlives the
    code it describes."""
    stale = re.findall(r"^.*409 carries no job.*$", script, re.MULTILINE | re.IGNORECASE)
    assert stale == [], stale
    assert re.findall(r"^.*a 409 carries no job id to poll.*$", script, re.MULTILINE) == []


def test_the_login_job_is_polled_on_the_job_endpoint(script):
    """Both the primary path and an attached-by-409 tab must reach /api/login/status?job=."""
    tick = _code(_function_body(script, "loginTick"))
    assert re.search(
        r'state\.login\.attached\s*\?\s*"/api/config"\s*:\s*"/api/login/status\?job="', tick
    ), (
        "the poll target must be the job whenever one is known, and /api/config only for the "
        "attached fallback:\n" + tick
    )
    assert "encodeURIComponent(state.login.job)" in tick, tick


# --- F14: a selection belongs to the repo it was made in -------------------------------

def test_the_share_selection_is_scoped_to_a_repo(script):
    """A module-scoped Set survived the repo switch that invalidated it: the header claimed
    "3 selected" over unchecked boxes, and Share POSTed repo A's ids to repo B's slug."""
    assert not re.search(r"shareSel:\s*new Set\(\)", script), (
        "shareSel must carry the slug it belongs to, not be a bare Set"
    )
    assert re.search(r"shareSel:\s*\{\s*slug:", script), "shareSel must be keyed by slug"
    assert re.search(r"function shareSelectionFor\(", script), (
        "there must be one place that resolves a repo's selection"
    )
    scoped = _function_body(script, "shareSelectionFor")
    assert "state.shareSel.slug !== " in scoped and "new Set()" in scoped, scoped


def test_the_share_handler_resolves_its_ids_against_the_slug_it_posts_to(script):
    """Reading the ids off a closure captured at render time is what let a stale selection be
    submitted elsewhere; they must be re-resolved for the slug in the URL being built."""
    body = next(b for b in _click_handlers(script) if "/share" in b)
    assert "shareSelectionFor(slug)" in body, body
    assert body.index("shareSelectionFor(slug)") < body.index("act("), body
    assert not re.search(r"Array\.from\(selected\)", body), (
        "the ids must come from the scoped lookup, not the captured Set:\n" + body
    )
    assert re.search(r"ids\.length === 0", body), (
        "an emptied selection must refuse to POST rather than share nothing:\n" + body
    )


def test_the_team_view_reads_its_selection_through_the_scoped_lookup(script):
    view = _function_body(script, "viewTeam")
    assert "shareSelectionFor(slug)" in view, view[:400]
    assert not re.search(r"const selected = state\.shareSel(\b|\.ids)", view), view[:400]


# --- an unreadable file is never an empty one ------------------------------------------

def test_the_global_view_tells_an_unreadable_file_from_an_empty_one(script):
    """A corrupt ~/.contexer/_global.json read as `{"entries": []}`, so the view said "No global
    rules" over a file that still held them — and the Add button beside that sentence rewrote the
    file with one entry, destroying every global rule on the machine. The store refuses that write
    now; the view must stop making the claim that invited it.
    """
    body = _code(_function_body(script, "viewGlobal"))
    assert re.search(r"data\.ok === false", body), (
        "viewGlobal must read the readability flag off the payload:\n" + body[:500]
    )
    # The notice must come back before anything renders a list, an empty state or the Add form.
    guard = body.find("if (unreadable)")
    assert guard > 0, "viewGlobal must return early when the file could not be parsed:\n" + body[:500]
    assert guard < body.index("No global rules"), (
        'the "No global rules" empty state must be unreachable for an unreadable file'
    )
    assert guard < body.index("Add a global rule"), (
        "the Add-rule form must not be offered over a file whose contents are unknown"
    )
    branch = _block_at(body, body.index("{", guard))
    assert "globalUnreadableNotice" in branch, branch


def test_the_unreadable_notices_are_built_from_one_component(script):
    """The Global, Deleted and store-unreadable notices must read as the same thing, because they
    are: `notice("bad", ...)` with the file's own parser error attached."""
    for name in ("unreadableNotice", "tombstonesUnreadableNotice", "globalUnreadableNotice"):
        body = _function_body(script, name)
        assert re.search(r'return notice\("bad"', body), name + " must use the shared component"
        assert 'class: "mono", text: String(error)' in body, (
            name + " must surface the underlying parser error"
        )


def test_an_unreadable_file_never_shows_a_zero_count_in_the_nav(script):
    """`0` in the Global badge means "no rules"; the one thing an unparseable file does not mean.
    Deleted already answers "!" for exactly this, and the two must not disagree."""
    m = re.search(r"const badges = \{", script)
    assert m, "the nav badge map moved"
    badges = _block_at(script, m.end() - 1)
    assert re.search(r"global:\s*state\.globalOk === false \? \"!\"", badges), badges
    assert re.search(r"state\.tombstonesOk === false", badges), badges
    assert re.search(r"globalOk:\s*true", script), "state.globalOk must default to readable"


# --- the CSP the console is served under -----------------------------------------------

def test_the_assets_stay_inside_the_content_security_policy():
    """`default-src 'none'; style-src 'self'; script-src 'self'` — an inline handler, an inline
    style attribute or an off-host URL is not a lint nit here, it is a blank page."""
    for path in (SCRIPT, MARKUP, STYLES):
        text = path.read_text()
        code = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.MULTILINE)
        code = re.sub(r"^\s*<!--.*?-->", "", code, flags=re.DOTALL | re.MULTILINE)
        assert "innerHTML" not in code, "%s: innerHTML with untrusted decision content" % path.name
        assert "outerHTML" not in code, path.name
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", code), "%s: inline event handler" % path.name
        assert not re.search(r"\sstyle\s*=\s*[\"']", code), "%s: inline style attribute" % path.name
        assert not re.search(r"https?://", code), "%s: off-host URL" % path.name


def test_the_markup_carries_no_inline_script_or_style_block():
    markup = MARKUP.read_text()
    assert not re.search(r"<script(?![^>]*\ssrc=)", markup), "inline <script> is blocked by the CSP"
    assert not re.search(r"<style\b", markup), "inline <style> is blocked by the CSP"


def test_hidden_still_means_hidden_for_the_toast_and_banner():
    """Kept alongside the sibling guard in test_ui_api.py: a `display:` rule on a component that is
    toggled through `element.hidden` outranks the UA stylesheet, and shipped a permanently visible
    "Disconnected" banner over a working dashboard."""
    css = STYLES.read_text()
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css), (
        "console.css must neutralise `display` for [hidden] elements"
    )
