"""Guards over the console's shipped assets.

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
import json
import os
import re
import shutil
import subprocess
import tempfile
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


# --- brand and design-system parity with the hosted dashboard --------------------------
#
# The console is a second surface over the same product, so a user arriving from
# app.contexer.ai has to recognise it. These guards pin the parts that a reviewer caught
# drifting: the wrong logo, an uncoloured taxonomy, and two shared class names that had
# quietly acquired different meanings here.

ICON = ASSETS / "icon.svg"

# The C-bracket of the canonical mark (contexer-mark.svg, and the dashboard's BrandGlyph).
CANONICAL_MARK = "M23 8 H13 A6 6 0 0 0 7 14 V18 A6 6 0 0 0 13 24 H23"


def test_the_sidebar_wears_the_canonical_contexer_mark():
    """The console once drew a database cylinder here. The top-left corner is the one element a
    user compares between the two surfaces, so a different glyph reads as a different product."""
    markup = MARKUP.read_text()
    assert CANONICAL_MARK in markup, "the sidebar brand must be the canonical C-bracket mark"
    assert 'viewBox="0 0 32 32"' in markup, "the mark is authored on a 32-unit box"
    assert "brand-tile" in markup, "the cursor tile must be filled through a class, not a literal"
    assert re.search(r'\.brand-tile\s*\{[^}]*fill:\s*var\(--signal\)', STYLES.read_text()), (
        "the tile has to track --signal rather than hardcoding the lime"
    )


def test_the_favicon_is_served_and_is_the_same_mark():
    """A console tab is long-lived, so the favicon is the branding surface a user actually looks
    at. Missing, it is also an automatic /favicon.ico request the daemon answers 404/401."""
    from contexer.ui import server

    assert "/icon.svg" in server.ASSETS, "the favicon must be in the static allowlist"
    name, content_type = server.ASSETS["/icon.svg"]
    assert name == "icon.svg" and content_type.startswith("image/svg+xml"), (name, content_type)
    assert ICON.exists(), "contexer/ui/assets/icon.svg is missing"
    icon = ICON.read_text()
    assert CANONICAL_MARK in icon, "the favicon must be the same mark as the sidebar"
    assert re.search(r'<link[^>]+rel="icon"[^>]+href="/icon\.svg"', MARKUP.read_text()), (
        "index.html must point at the favicon"
    )
    # xmlns is a namespace name, not a fetch; nothing else may reach off-host.
    assert re.findall(r"https?://(?!www\.w3\.org/2000/svg)", icon) == [], icon


def test_every_subtype_has_a_colour_on_both_the_chip_and_the_row(script):
    """Subtype is a colour as well as a word — that is what makes a 200-row list scannable, and
    it is the dashboard's own language. A subtype with no rule falls back to neutral silently,
    so the taxonomy and the stylesheet are checked against each other rather than by eye."""
    m = re.search(r"const SUBTYPES = \[(.*?)\]", script, re.DOTALL)
    assert m, "the subtype vocabulary moved"
    subtypes = re.findall(r'"([a-z_]+)"', m.group(1))
    assert subtypes, m.group(1)

    classes = _block_at(script, script.index("{", script.index("const SUBTYPE_CLASS")))
    css = STYLES.read_text()
    for subtype in subtypes:
        assert re.search(r'\b%s:\s*"badge-%s"' % (subtype, subtype), classes), (
            "%s has no chip class" % subtype
        )
        assert re.search(r"\.badge-%s\s*\{[^}]*color:\s*var\(--t-%s\)" % (subtype, subtype), css), (
            "%s's chip must take its colour from the shared token" % subtype
        )
        assert re.search(
            r'\.drow\[data-subtype="%s"\]\s*\{[^}]*border-left-color:\s*var\(--t-%s\)'
            % (subtype, subtype), css
        ), "%s has no row stripe, or the stripe does not share the chip's token" % subtype
        assert re.search(r"--t-%s:" % subtype, css), "%s has no colour token" % subtype


def test_no_two_subtypes_share_a_colour_token():
    css = STYLES.read_text()
    values = dict(re.findall(r"--t-([a-z]+):\s*([^;]+);", css))
    assert len(values) >= 4, values
    assert len(set(values.values())) == len(values), "two subtypes resolve to the same hue: %r" % values


def test_a_status_chip_never_borrows_a_subtype_hue():
    """`suggested` used to be the exact blue that means `convention`, and the two render side by
    side on the same row. Status is a maturity, not a category; it spends no hue."""
    css = STYLES.read_text()
    convention = re.search(r"--t-convention:\s*([^;]+);", css).group(1).strip()
    suggested = re.search(r"\.badge-suggested\s*\{([^}]*)\}", css).group(1)
    assert convention not in suggested, "badge-suggested is reusing the convention hue"
    assert "dashed" in suggested, "suggested reads as provisional through its border, not a hue"
    for token in ("--t-convention", "--t-constraint", "--t-architecture", "--t-pattern"):
        assert token not in suggested, "%s belongs to a subtype" % token

    # The dashboard's .badge-pending is neutral; an amber one here means the same class name
    # carries two different urgencies across the two surfaces.
    pending = re.search(r"\.badge-pending\s*\{([^}]*)\}", css).group(1)
    assert "--warn" not in pending, "badge-pending must stay neutral, as it is on the dashboard"
    assert "--muted-foreground" in pending, pending


def test_stored_text_is_bidi_isolated_wherever_it_is_rendered():
    """Decision bodies are stored without unicode sanitization. An un-isolated RTL/override run
    reorders characters across the element boundary and can visually rewrite the control next to
    it — here, Approve and Delete. Same rule, same reason, as the hosted dashboard."""
    css = STYLES.read_text()
    m = re.search(r"((?:\.[a-z-]+(?: li)?,\s*)+[.a-z -]+)\{\s*unicode-bidi:\s*isolate", css)
    assert m, "console.css no longer isolates stored text"
    isolated = set(re.findall(r"\.([a-z-]+)", m.group(1)))
    for selector in ("drow-title", "drow-text", "detail-title", "tl-title", "review-title",
                     "prose", "code-block", "rev-body", "diff", "diff-side", "factors",
                     "notice-body"):
        assert selector in isolated, "%s renders stored text but is not isolated" % selector


def test_motion_is_dropped_under_prefers_reduced_motion():
    css = STYLES.read_text()
    m = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{", css)
    assert m, "console.css animates hover, icons and the press scale with no reduced-motion query"
    block = _block_at(css, m.end() - 1)
    assert "transition-duration" in block and "!important" in block, block
    assert re.search(r"\.btn:active[^{]*\{[^}]*transform:\s*none", block), (
        "with the transition gone the press scale is an instant jump, not a softer one"
    )


def test_every_view_names_itself_in_the_tab(script):
    """Seven views sharing one static <title> cannot be told apart in a tab strip, and several
    console tabs open on different repos is the normal case."""
    m = re.search(r"const VIEW_TITLE = \{", script)
    assert m, "no per-view tab titles"
    titles = _block_at(script, m.end() - 1)
    nav = _block_at(script, script.index("{", script.index("const activeNav = ")))
    for view in re.findall(r"^\s*([a-z]+):", nav, re.MULTILINE):
        assert re.search(r"\b%s:\s*\{" % view, titles), "%s has no tab title" % view
    assert "document.title" in script, "the titles are computed but never applied"


# --- The proposed-update diff ----------------------------------------------------------

# Two proposals in one real store rendered in two different views — an inline word diff that read
# as confetti, and a plain before/after dump — because the only switch was DIFF_BUDGET, i.e. LCS
# CELLS. The pair differed by 0.8% in cells and not at all in kind: both were rewrites. Size is
# not churn, so these guards pin the switch to churn and pin the split view's shape.

def test_the_diff_view_switches_on_churn_not_only_size(script):
    body = _function_body(script, "diffView")
    assert "sameShare" in body and "DIFF_INLINE_MIN_SAME" in body, (
        "diffView is back to switching on size alone: a rewrite that fits the LCS budget renders "
        "inline as struck-out confetti"
    )
    assert "splitDiffView" in body, "the low-similarity branch has nowhere to go"
    assert re.search(r"if\s*\(!parts\s*\|\|", body), (
        "an over-budget diff must still reach the split view, not fall through to the inline map"
    )
    assert "DIFF_INLINE_MAX_CHARS" in body, (
        "a SHORT rewrite scores like a long one; sending it to the split view costs it the "
        "word-level highlight for no readability gain"
    )
    assert re.search(r"const DIFF_INLINE_MIN_SAME = 0\.\d+", script), "the threshold is not a constant"


def test_the_split_diff_is_sentence_aligned_not_line_aligned(script):
    """Every proposal in a real store is ONE line of prose. A line-aligned view has nothing to
    align on and degenerates to the whole body removed beside the whole body added — which is the
    unreadable dump this replaced."""
    body = _function_body(script, "splitDiffView")
    assert "splitSentences" in body and "diffSeq" in body and "pairRows" in body, body
    scan = _function_body(script, "pushPlain")
    assert '".!?;".indexOf' in scan, "sentences are no longer split on sentence enders"
    assert ":" not in re.search(r'"\S*"\.indexOf', scan).group(0), (
        "splitting on ':' stranded every label — 'Rationale:', 'Guard:', 'Execution:' — in a "
        "bordered cell of its own; a label belongs with the clause it introduces"
    )
    for guard, why in (
        ("inCode", "a `;` inside an inline code span is not a sentence end"),
        ("inQuote", 'a `;` inside a "quoted phrase" cut the quote across two cells'),
        ("ENUM_TAIL_RE", "the '.' of a list marker ('1.', '(3)') is not a sentence end"),
    ):
        assert guard in scan, why
    assert "FENCE_RE" in _function_body(script, "splitSentences"), (
        "a fenced code block must be one row; splitting it on newlines put every statement of a "
        "quoted schema, and the ``` markers themselves, in cells of their own"
    )


def test_pair_rows_zips_to_the_longer_run(script):
    """One half of no-content-loss: a run of removals is zipped against the run of insertions
    after it, and whichever side is longer must still emit its leftovers opposite an empty cell.
    The other half — that splitSentences did not drop anything before pairRows ever saw it — is
    not checkable by reading source, and is pinned by test_no_stored_text_is_lost_on_the_way_to_a
    _cell below, which runs the real thing."""
    body = _function_body(script, "pairRows")
    assert "Math.max(dels.length, inss.length)" in body, (
        "zipping to the SHORTER run drops the tail of the longer one — content the developer is "
        "approving would never be shown"
    )
    assert body.count("flush()") >= 2, "a trailing del/ins run is only emitted by the final flush"
    view = _function_body(script, "splitDiffView")
    for cls in ("diff-side-same", "diff-side-del", "diff-side-ins", "diff-side-empty"):
        assert cls in view, "%s row shape is not rendered" % cls


def test_the_word_highlight_is_gated_on_pair_similarity(script):
    """Rows are paired by POSITION inside a replaced run, so on a rewrite the two sides of a pair
    are usually unrelated sentences (58 of 62 replaced rows in the real store share under 0.3).
    Highlighting those re-imports the confetti the split view exists to kill."""
    body = _function_body(script, "splitDiffView")
    assert "DIFF_PAIR_MIN_SAME" in body, "unrelated paired sentences are word-diffed again"
    assert re.search(r"sameShare\(parts\)\s*>=\s*DIFF_PAIR_MIN_SAME", body), body
    assert re.search(r"const DIFF_PAIR_MIN_SAME = 0\.\d+", script), "the threshold is not a constant"


def test_the_split_diff_is_laid_out_as_one_grid():
    """The two cells of a row are consecutive grid children, which is what equalizes their heights.
    Two independent columns would let the sides drift apart the moment one cell wraps."""
    css = STYLES.read_text()
    m = re.search(r"\.diff-split\s*\{", css)
    assert m, "no .diff-split rule"
    block = _block_at(css, m.end() - 1)
    assert "display: grid" in block and "grid-template-columns: 1fr 1fr" in block, block
    for cls in (".diff-side", ".diff-side-del", ".diff-side-ins", ".diff-side-empty"):
        assert re.search(re.escape(cls) + r"\s*[,{]", css), "%s has no style" % cls
    for m in re.finditer(r"@media[^{]*max-width[^{]*\{", css):
        narrow = _block_at(css, m.end() - 1)
        rule = re.search(r"\.diff-split\s*\{[^}]*\}", narrow)
        assert not (rule and "grid-template-columns: 1fr;" in rule.group(0)), (
            "the grid's children are a FLAT cell stream: collapsing to one column strands the two "
            "headers side by side and then alternates unlabelled cells, and an unchanged row "
            "prints its identical text twice in a row.\n" + rule.group(0)
        )
    assert re.search(r"\.diff-split \.diff-del\s*\{[^}]*text-decoration:\s*none", css), (
        "the cell tint already says removed; striking every word through as well is noise"
    )


# --- The diff, actually executed -------------------------------------------------------

# The guards above read source. They cannot see what splitSentences DOES, and the property that
# matters most — that nothing the developer is about to approve is dropped on the way to a cell —
# lives in behaviour, not shape. These run the shipped functions under node (the console has no
# test runner of its own; CI already provisions node for contexer-check).

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Minimal DOM: enough for h()/append() to build a tree and for the test to read it back.
_DOM_SHIM = """
class Node {
  constructor(tag){ this.tag = tag; this.className = ""; this.kids = []; }
  appendChild(n){ this.kids.push(n); return n; }
  setAttribute(){} addEventListener(){}
  set textContent(v){ this.kids = [new Text(v)]; }
  get textContent(){ return this.kids.map((k) => k.textContent).join(""); }
}
class Text extends Node {
  constructor(v){ super("#text"); this.value = String(v); }
  get textContent(){ return this.value; }
}
const document = { createElement: (t) => new Node(t), createTextNode: (v) => new Text(v) };
"""

_JS_FUNCTIONS = (
    "append", "h", "lcsOps", "tokenize", "diffTokens", "sameShare", "splitSentences",
    "pushPlain", "diffSeq", "pairRows", "sideParts", "diffCell", "splitDiffView", "diffView",
)


def _js_declaration(text: str, name: str) -> str:
    """`function name(...) { ... }`, declaration included — the body alone is not runnable."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", text)
    assert m, "no function %s in console.js" % name
    return text[m.start():m.end() - 1] + _block_at(text, m.end() - 1)


def _js_consts(text: str) -> str:
    """The module constants the diff reads. Taken from the source, never restated here: a test
    carrying its own copy of a threshold passes while the shipped one drifts. `let`, so a case
    that needs a different budget can say so."""
    out = []
    for name in ("DIFF_BUDGET", "SENTENCE_BUDGET", "DIFF_INLINE_MIN_SAME", "DIFF_PAIR_MIN_SAME",
                 "DIFF_INLINE_MAX_CHARS", "FENCE_RE", "ENUM_TAIL_RE"):
        m = re.search(r"^\s*const (" + name + r") = (.+?);\s*(?://.*)?$", text, re.MULTILINE)
        assert m, "console.js no longer defines %s" % name
        out.append("let %s = %s;" % (m.group(1), m.group(2)))
    return "\n".join(out)


def _run_js(script_text: str, snippet: str):
    """Run `snippet` with the console's diff functions in scope; it prints JSON on the last line."""
    src = "\n".join([_DOM_SHIM, _js_consts(script_text)]
                    + [_js_declaration(script_text, n) for n in _JS_FUNCTIONS] + [snippet])
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        proc = subprocess.run([NODE, path], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# Every shape the splitter has to survive, and what it must not do to it.
SPLIT_CASES = [
    ("Use this schema:\n\n```sql\nCREATE TABLE d (\n  id UUID PRIMARY KEY,\n  b TEXT NOT NULL\n);\n"
     "```\nApply it at boot.", "fenced block"),
    ("Do this:\n```js\nconst a = 1;\nconst b = 2;", "unterminated fence"),
    ("Hard rules (from CLAUDE.md): 1. All SQL lives in db. 2. None in apps. Done.", "enumerators"),
    ('The wording ("all SQL lives in db; none in apps") conflated two things. Next.', "quote"),
    ("Set `a: 1; b: 2` now. Then stop.", "inline code span"),
    ("head:\n    indented line\n    another line", "indentation"),
    ("Rate is 0.5 per sec. Next.", "decimal"),
    ("There are 3. Continue with rollout.", "digit-final sentence"),
    ("Steps: 1) ship it. 2) watch it.", "paren markers"),
    ("", "empty"),
    ("no terminator at all", "no sentence end"),
]


@needs_node
def test_no_stored_text_is_lost_on_the_way_to_a_cell(script):
    """The split view is the ONLY place a reviewer sees a proposal in full — `.drow-text` is
    clamped to two lines — so a developer clicking Approve must have been shown every word of what
    they are approving. Checked end to end: source -> sentences -> LCS -> rows -> columns."""
    cases = json.dumps([c for c, _ in SPLIT_CASES])
    out = _run_js(script, """
const cases = %s;
const norm = (s) => s.replace(/\\s+/g, " ").trim();
const report = [];
for (const a of cases) {
  for (const b of cases) {
    const rows = pairRows(diffSeq(splitSentences(a), splitSentences(b)));
    const left = rows.map((r) => r.left).filter((v) => v !== null);
    const right = rows.map((r) => r.right).filter((v) => v !== null);
    report.push({
      a: norm(a), b: norm(b),
      leftOk: norm(left.join(" ")) === norm(a),
      rightOk: norm(right.join(" ")) === norm(b),
      order: JSON.stringify(left) === JSON.stringify(splitSentences(a)),
    });
  }
}
console.log(JSON.stringify(report));
""" % cases)
    bad = [r for r in out if not (r["leftOk"] and r["rightOk"] and r["order"])]
    assert bad == [], "text dropped or reordered on the way to a cell: %r" % bad[:3]


@needs_node
def test_the_splitter_cuts_on_claims_not_on_punctuation(script):
    """Each of these produced a cell that was not a claim: a lone '1.', a ``` marker, half of a
    quoted phrase, a line of SQL, or a row that had lost its indentation."""
    out = _run_js(script, """
const cases = %s;
console.log(JSON.stringify(cases.map((c) => splitSentences(c))));
""" % json.dumps([c for c, _ in SPLIT_CASES]))
    rows = dict(zip([label for _, label in SPLIT_CASES], out))
    assert rows["fenced block"] == [
        "Use this schema:",
        "```sql\nCREATE TABLE d (\n  id UUID PRIMARY KEY,\n  b TEXT NOT NULL\n);\n```",
        "Apply it at boot.",
    ], rows["fenced block"]
    assert rows["unterminated fence"] == ["Do this:", "```js\nconst a = 1;\nconst b = 2;"]
    assert rows["enumerators"] == [
        "Hard rules (from CLAUDE.md): 1. All SQL lives in db.", "2. None in apps.", "Done."
    ], rows["enumerators"]
    assert rows["quote"] == [
        'The wording ("all SQL lives in db; none in apps") conflated two things.', "Next."
    ], rows["quote"]
    assert rows["inline code span"] == ["Set `a: 1; b: 2` now.", "Then stop."]
    assert rows["indentation"] == ["head:", "    indented line", "    another line"], (
        "leading whitespace is content in a quoted block; only trailing whitespace may be trimmed"
    )
    assert rows["decimal"] == ["Rate is 0.5 per sec.", "Next."], rows["decimal"]
    assert rows["digit-final sentence"] == ["There are 3.", "Continue with rollout."], (
        "position is what makes a number a list marker: one that merely ENDS a sentence is a "
        "count, and suppressing its boundary swallowed the next claim into the same row"
    )
    assert rows["paren markers"] == ["Steps: 1) ship it.", "2) watch it."], rows["paren markers"]
    assert rows["empty"] == [] and rows["no sentence end"] == ["no terminator at all"]


@needs_node
def test_the_view_is_chosen_by_churn_and_size_together(script):
    """A rewrite goes side-by-side however small the LCS table is; a small edit keeps the inline
    highlight however low its similarity scores."""
    out = _run_js(script, """
const long = (w) => (w + " ").repeat(60).trim();
const cases = {
  smallEdit: [long("alpha beta gamma") + " tail one.", long("alpha beta gamma") + " tail two."],
  rewrite: [long("alpha beta gamma") + " done.", long("delta epsilon zeta") + " done."],
  shortRewrite: ["Store decisions in JSON.", "Persist decisions to SQLite."],
};
const out = {};
for (const k of Object.keys(cases)) {
  const node = diffView(cases[k][0], cases[k][1]);
  const kinds = [];
  const walk = (n) => { if (n.className) kinds.push(n.className); (n.kids || []).forEach(walk); };
  walk(node);
  out[k] = { root: node.className, kinds: kinds, text: node.textContent.length };
}
console.log(JSON.stringify(out));
""")
    assert out["smallEdit"]["root"] == "diff", "a small edit lost its inline word highlight"
    assert any(k.startswith("diff-del") for k in out["smallEdit"]["kinds"])
    assert out["rewrite"]["root"] == "diff-split", "a rewrite still renders as inline confetti"
    assert out["shortRewrite"]["root"] == "diff", (
        "a short rewrite scores like a long one but interleaving a few words is not confetti"
    )
    assert any(k.startswith("diff-ins") for k in out["shortRewrite"]["kinds"])


@needs_node
def test_an_unalignable_pair_is_reported_honestly(script):
    """Over the sentence budget the aligner must say "I could not align these" — one removed row
    beside one added row. Emitting every sentence as removed-and-added instead paints sentences
    that never changed as changed, which is a lie the reviewer cannot see through."""
    out = _run_js(script, """
SENTENCE_BUDGET = 4;                       // any real pair of texts is now over budget
const a = "One. Two. Three.";
const b = "One. Two changed. Three.";
const rows = pairRows(diffSeq(splitSentences(a), splitSentences(b)));
console.log(JSON.stringify({ rows: rows, sameCount: rows.filter((r) => r.same).length }));
""")
    assert len(out["rows"]) == 1, out["rows"]
    assert out["rows"][0]["left"] == "One.\nTwo.\nThree."
    assert out["rows"][0]["right"] == "One.\nTwo changed.\nThree."


@needs_node
def test_the_word_highlight_fires_for_a_related_pair_and_not_for_an_unrelated_one(script):
    """DIFF_PAIR_MIN_SAME is not a switch that is always off: over the real store exactly one
    replaced row is a genuine rewording (0.71) and it is the one that gets highlighted."""
    out = _run_js(script, """
const rel = ["The real customer later logs in, resumes at billing naturally, and completes.",
             "The real customer resumes at billing later and completes normally."];
const unrel = ["Gate access until the Stripe subscription first becomes active.",
               "Webhook reconciler: the payload is the sole writer of root metadata."];
const shape = (a, b) => {
  const node = splitDiffView(a, b);
  const spans = [];
  const walk = (n) => { if (/diff-(del|ins)$/.test(n.className)) spans.push(n.className); (n.kids||[]).forEach(walk); };
  walk(node);
  return { spans: spans.length, text: node.textContent };
};
console.log(JSON.stringify({ related: shape(rel[0], rel[1]), unrelated: shape(unrel[0], unrel[1]) }));
""")
    assert out["related"]["spans"] > 0, "a genuine rewording renders with no change marked at all"
    assert out["unrelated"]["spans"] == 0, (
        "two unrelated sentences paired by position were word-diffed: that is the confetti the "
        "split view exists to remove, one level down"
    )
    # Suppressing the highlight must not suppress the text it would have marked up.
    assert "payload is the sole writer" in out["unrelated"]["text"], out["unrelated"]["text"]
    assert "Gate access until" in out["unrelated"]["text"], out["unrelated"]["text"]
