/* Contexer local console — hand-written, no build step, no framework.
 *
 * CSP: default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:;
 *      connect-src 'self'
 * => every handler is addEventListener, every icon is inline SVG in index.html, and every
 *    piece of server-provided text is written with textContent / createTextNode. There is no
 *    innerHTML in this file: decision content is untrusted input rendered inside the user's
 *    authenticated session.
 */
"use strict";

(() => {
  // ── Vocabulary (mirrors contexer/store.py; the console never invents values) ──────────
  const SUBTYPES = ["architecture", "constraint", "pattern", "convention"];
  // Subtype -> chip class. The classes and their hues live in console.css; this map only says
  // which subtypes HAVE one, so an unknown value falls through to the neutral chip.
  const SUBTYPE_CLASS = {
    architecture: "badge-architecture",
    constraint: "badge-constraint",
    pattern: "badge-pattern",
    convention: "badge-convention",
  };
  const GLOBAL_SUBTYPES = ["constraint", "convention"];
  const STATUSES = ["approved", "suggested", "pending_approval", "ignored"];
  // Tab titles. `scoped` views belong to one repo, so their tab names it: several console tabs
  // open on different repos is the normal case, and seven tabs all reading "Contexer Console"
  // cannot be told apart. Most specific segment first — a tab strip truncates from the right.
  const VIEW_TITLE = {
    dashboard: { label: "Dashboard", scoped: true },
    decisions: { label: "Decisions", scoped: true },
    sessions: { label: "Sessions", scoped: true },
    review: { label: "Review", scoped: true },
    deleted: { label: "Deleted", scoped: true },
    global: { label: "Global rules", scoped: false },
    team: { label: "Team context", scoped: false },
    config: { label: "Settings", scoped: false },
  };
  const STATUS_LABEL = {
    approved: "approved",
    suggested: "suggested",
    pending_approval: "pending",
    ignored: "ignored",
  };
  const POLL_MS = 10000;
  const LOGIN_POLL_MS = 1000;
  // The daemon caps a login job at ~5 minutes and reports `failed` itself, so this is only the
  // backstop for the cases where no job answers: an older daemon whose 409 names no job to poll,
  // or a daemon that went away mid-flow.
  const LOGIN_MAX_MS = 360000;
  const DIFF_BUDGET = 250000; // LCS cells; beyond this the columns render unmarked
  // Share of characters that must survive unchanged for the word-level marks to be drawn inside
  // the two columns. Measured over the real proposals in a store: a rewrite scores 0.04-0.11 and
  // marking it stripes practically every word, while a hardening pass that keeps the original
  // prose scores 0.69-0.99 and marks exactly what moved.
  const DIFF_MARK_MIN_SAME = 0.5;
  // Combined length under which the marks are drawn whatever the share says: a short rewrite
  // scores like a long one ("Store decisions in JSON." -> "Persist decisions to SQLite." shares
  // 0.30) but a few marked words are still easy to eye over. The smallest real proposal pair is
  // 2324 chars, so this only catches the genuinely small edit.
  const DIFF_MARK_MAX_CHARS = 500;
  // Rows one decisions request asks for. There is no pager, so the header says what is SHOWN
  // as well as what matched: "300 matching" over 200 rendered rows was a plain miscount.
  const PAGE_LIMIT = 200;
  // The value the subtype <select> shows for an entry that has none. Not a store value: the
  // API reads "" as "leave the subtype alone", so it is never sent as a change.
  const NO_SUBTYPE = "";

  const state = {
    csrf: "",
    version: "",
    port: "",
    stores: [],
    slug: "", // sticky: the store the store-scoped nav links point at
    route: { name: "boot", slug: "", id: "" },
    filters: { q: "", subtype: "", status: "", file: "" },
    counts: { global: null, team: null },
    // Last known readability of the tombstone sidecar. A corrupt sidecar is not "nothing
    // deleted", so the Deleted nav badge has to be able to say so from any view.
    tombstonesOk: true,
    // Same idea for ~/.contexer/_global.json: a corrupt file is not "no global rules", so the
    // Global nav badge has to be able to say so from any view.
    globalOk: true,
    edit: null, // { id, content, title, subtype, stored }
    confirm: "", // id awaiting a delete/restore confirmation
    // The Teams login this tab is waiting on. `attached` is the fallback for a login this tab
    // cannot follow by job — an older daemon's 409 names none, so all that is left to watch is
    // the session state itself. A 409 that DOES name one is polled like our own.
    login: {
      polling: false,
      attached: false,
      job: "",
      message: "",
      error: "",
      after: null,
      timer: 0,
      deadline: 0,
    },
    // A pull that failed for want of a Teams session: { slug, message, state }. Held in state so
    // it survives the refetch and stays on screen until it is acted on — a toast would not.
    authPanel: null,
    // Share selection, SCOPED to the repo it was made in. A module-scoped bare Set outlived the
    // repo switch that invalidated it: the header kept claiming "3 selected" while every checkbox
    // rendered unchecked, and "Share selected" POSTed repo A's ids to repo B's slug.
    shareSel: { slug: "", ids: new Set() },
    disconnected: false,
    busy: false,
    // True only while a 10s background refresh is in flight: every GET it issues carries
    // X-Contexer-Poll so the daemon's idle watchdog ignores it and a forgotten background tab
    // cannot keep the daemon alive forever.
    pollMode: false,
  };

  // ── DOM helpers ───────────────────────────────────────────────────────────────────────
  function append(node, kids) {
    if (kids === null || kids === undefined || kids === false) return;
    if (Array.isArray(kids)) {
      for (const k of kids) append(node, k);
      return;
    }
    node.appendChild(kids instanceof Node ? kids : document.createTextNode(String(kids)));
  }

  /** h("div", {class, text, on:{click}, props:{value}, ...attrs}, children) */
  function h(tag, opts, kids) {
    const node = document.createElement(tag);
    if (opts) {
      for (const key of Object.keys(opts)) {
        const val = opts[key];
        if (val === null || val === undefined || val === false) continue;
        if (key === "class") node.className = val;
        else if (key === "text") node.textContent = String(val);
        else if (key === "on") {
          for (const ev of Object.keys(val)) node.addEventListener(ev, val[ev]);
        } else if (key === "props") {
          for (const p of Object.keys(val)) node[p] = val[p];
        } else node.setAttribute(key, String(val));
      }
    }
    append(node, kids);
    return node;
  }

  function frag(kids) {
    const f = document.createDocumentFragment();
    append(f, kids);
    return f;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function tpl(id) {
    const t = document.getElementById(id);
    return t ? t.content.cloneNode(true) : null;
  }

  // ── Formatting ────────────────────────────────────────────────────────────────────────
  /** Accepts an ISO-8601 string or an epoch-seconds number; returns ms or null. */
  function toMs(v) {
    if (typeof v === "number" && isFinite(v)) return v > 1e11 ? v : v * 1000;
    if (typeof v === "string" && v) {
      const ms = Date.parse(v);
      return isFinite(ms) ? ms : null;
    }
    return null;
  }

  function fmtAgo(v) {
    const ms = toMs(v);
    if (ms === null) return "";
    let s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 45) return "just now";
    if (s < 5400) return Math.round(s / 60) + "m ago";
    if (s < 172800) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  function fmtStamp(v) {
    const ms = toMs(v);
    if (ms === null) return "";
    const d = new Date(ms);
    const pad = (n) => String(n).padStart(2, "0");
    return (
      d.getFullYear() +
      "-" +
      pad(d.getMonth() + 1) +
      "-" +
      pad(d.getDate()) +
      " " +
      pad(d.getHours()) +
      ":" +
      pad(d.getMinutes())
    );
  }

  function fmtDuration(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds)) return "";
    if (seconds < 3600) return Math.max(1, Math.round(seconds / 60)) + " min";
    if (seconds < 172800) return Math.round(seconds / 3600) + " h";
    return Math.round(seconds / 86400) + " d";
  }

  function shortId(id) {
    return String(id || "").slice(0, 8);
  }

  function titleOf(d) {
    const t = (d && d.title ? String(d.title) : "").trim();
    if (t) return t;
    const c = (d && d.content ? String(d.content) : "").trim();
    if (!c) return "(untitled decision)";
    return c.length > 96 ? c.slice(0, 96) + "…" : c;
  }

  /** Row-level file summary (Task 4 of #174): the first two anchored files, then a "+N more"
   * count — mirrors the "+N more" convention the store's own staleness note uses, so a
   * decision anchored to many files still renders as one short line instead of pushing the
   * row's height around. The detail pane's "Anchored files" block lists every file in full;
   * this stays a compact pointer at it. */
  function filesLabel(files) {
    if (!Array.isArray(files) || !files.length) return "";
    const shown = files.slice(0, 2).join(", ");
    const extra = files.length - 2;
    return extra > 0 ? shown + `, +${extra} more` : shown;
  }

  /** Tolerates both `[...]` and `{key: [...]}` payload shapes for list endpoints. */
  function asList(payload, key) {
    if (Array.isArray(payload)) return payload;
    if (payload && Array.isArray(payload[key])) return payload[key];
    return [];
  }

  function num(v) {
    return typeof v === "number" && isFinite(v) ? v : 0;
  }

  // ── Chrome: disconnected banner, toast ────────────────────────────────────────────────
  const banner = document.getElementById("banner");
  const bannerText = document.getElementById("banner-text");
  const toastEl = document.getElementById("toast");
  let toastTimer = 0;

  function setDisconnected(on, message) {
    state.disconnected = !!on;
    banner.hidden = !on;
    if (on) {
      bannerText.textContent =
        message || "Disconnected from the Contexer daemon. It may have shut down when idle.";
    }
  }

  function toast(message, bad) {
    toastEl.textContent = String(message);
    toastEl.className = bad ? "toast is-bad" : "toast";
    toastEl.hidden = false;
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      toastEl.hidden = true;
    }, 4000);
  }

  /** What actually went wrong, in the banner itself.
   *
   *  A transport-level rejection never reaches the daemon, so ~/.contexer/ui.log has no record
   *  of it and "Disconnected" on its own is unfalsifiable — indistinguishable from a daemon that
   *  is up and answering, which is exactly the confusion this replaces. The browser's own
   *  DOMException name and message are the only evidence that exists, so they belong on screen
   *  and in the devtools console rather than requiring the user to go find them. */
  function describeTransportFailure(path, err) {
    const kind = (err && (err.name || err.constructor && err.constructor.name)) || "TypeError";
    const detail = (err && err.message) || "no detail";
    try {
      window.console.error("contexer console: request to " + path + " failed before reaching " +
                           "the daemon:", err);
    } catch (ignored) {
      /* devtools console unavailable; the banner still carries the text */
    }
    return "Could not reach the daemon: " + kind + " (" + detail + ") on " + path +
           ". The request never arrived, so ui.log will not show it. Retry, or check that " +
           "`contexer ui --status` says running.";
  }

  // ── Networking ────────────────────────────────────────────────────────────────────────
  function NetworkError() {
    this.name = "NetworkError";
    this.message = "network";
  }
  NetworkError.prototype = Object.create(Error.prototype);

  /** Raised by mutate() when it REFUSES to send because another mutation is still in flight.
   *
   *  Refusal used to be a `null` return, which act() could not tell from a 204's empty body — so
   *  a double-click reported "Approved." for a request that never left the browser, and "Share
   *  selected" destroyed the selection before discovering nothing had been sent. A distinct throw
   *  makes "never sent" impossible to mistake for "succeeded". */
  function BusyError() {
    this.name = "BusyError";
    this.message = "Another change is still saving — try that again in a moment.";
  }
  BusyError.prototype = Object.create(Error.prototype);

  async function req(path, opts) {
    const o = opts || {};
    const method = o.method || "GET";
    const headers = {};
    if (o.poll || (state.pollMode && method === "GET")) headers["X-Contexer-Poll"] = "1";
    if (method !== "GET") {
      headers["X-Contexer-Token"] = state.csrf;
      if (o.body !== undefined) headers["Content-Type"] = "application/json";
    }
    const send = () => fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      body: o.body === undefined ? undefined : JSON.stringify(o.body),
    });
    let res;
    try {
      res = await send();
    } catch (err) {
      // A transport-level rejection is not proof the daemon is gone. The browser keeps pooled
      // and pre-opened sockets, and one the server has since closed fails the instant it is
      // reused — no request reaches the daemon, so there is no status and no log line. Retrying
      // once gets a fresh connection and succeeds; only a second failure means the daemon is
      // genuinely unreachable. A GET is safe to repeat. A mutation is NOT: it may have been
      // delivered and applied before the socket broke, so it reports instead of risking a
      // double write.
      if (method !== "GET") {
        setDisconnected(true, describeTransportFailure(path, err));
        throw new NetworkError();
      }
      try {
        res = await send();
      } catch (err2) {
        // A hidden tab is not a broken daemon: browsers freeze background tabs and cancel their
        // in-flight requests, which is indistinguishable here from a real outage. Staying quiet
        // means the banner cannot be a lie the user has to disprove — the visibilitychange
        // handler re-renders the moment the tab is looked at again.
        if (document.visibilityState !== "visible") throw new NetworkError();
        setDisconnected(true, describeTransportFailure(path, err2));
        throw new NetworkError();
      }
    }
    setDisconnected(false);
    const text = await res.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (err) {
        data = null;
      }
    }
    if (!res.ok) {
      const e = new Error((data && data.error) || "HTTP " + res.status);
      e.status = res.status;
      e.data = data;
      throw e;
    }
    return data;
  }

  /** GETs the CSRF value + version. The session cookie authenticates; this token authorizes
   *  mutations (the cookie is HttpOnly, so the page cannot read it). */
  async function handshake() {
    const info = await req("/healthz");
    if (info) {
      if (typeof info.csrf === "string") state.csrf = info.csrf;
      if (typeof info.version === "string") state.version = info.version;
    }
    return info;
  }

  /** Mutation + one automatic retry after refreshing the CSRF value (daemon restart).
   *  Throws BusyError rather than returning when it declines to send: see BusyError. */
  async function mutate(path, method, body) {
    if (state.busy) throw new BusyError();
    state.busy = true;
    try {
      return await req(path, { method, body });
    } catch (err) {
      if (err && err.status === 403) {
        try {
          await handshake();
          return await req(path, { method, body });
        } catch (err2) {
          throw err2;
        }
      }
      throw err;
    } finally {
      state.busy = false;
    }
  }

  /** Runs a mutation, reports failures, and refetches the affected view. No optimistic UI.
   *
   *  Returns TRUE only when the write reached the daemon and the daemon accepted it. Every caller
   *  that throws something away on a write's behalf — a selection, an open confirmation, a typed
   *  draft, the current route — must do it in `onOk` or behind this boolean, never before the
   *  await. `onOk` runs after the write succeeds and before the refetch, so the discard is
   *  reflected by the very render that reports it.
   *
   *  A truthy payload is NOT a success signal: a 204 answers with no body at all, so `out` is
   *  legitimately null on a write that worked. */
  async function act(path, method, body, okMessage, onOk) {
    try {
      const out = await mutate(path, method, body);
      // A 200 carrying `error` is the daemon refusing in prose. It is a failure, so nothing the
      // caller staged on success may run.
      if (out && out.error) {
        toast(out.error, true);
        await render();
        return false;
      }
      if (out && out.message) toast(out.message);
      else if (okMessage) toast(okMessage);
      if (typeof onOk === "function") onOk();
      await render();
      return true;
    } catch (err) {
      if (err instanceof BusyError) {
        // Nothing was sent. No success message, no refetch, and — because onOk never ran — no
        // state discarded for a request that does not exist.
        toast(err.message, true);
        return false;
      }
      if (err instanceof NetworkError) return false;
      // `current_version` is what makes a 409 an EDIT conflict. Other 409s (a restore refused
      // because the store is at capacity, a request against an unreadable store) carry their own
      // message, and claiming "changed underneath you" for those was simply wrong.
      const conflictVersion = err && err.data ? err.data.current_version : undefined;
      if (err && err.status === 409 && conflictVersion !== undefined) {
        // The draft is deliberately LEFT OPEN. Clearing state.edit here painted the read-only
        // pane over text the developer had just typed and could not get back; the reload gives
        // the form a fresh `if_version` to save against instead.
        toast("Changed underneath you (now v" + conflictVersion + ") — reloaded, your edits are kept.", true);
        await render();
        return false;
      }
      toast((err && err.message) || "Request failed", true);
      await render();
      return false;
    }
  }

  // ── Teams session ─────────────────────────────────────────────────────────────────────
  /* `contexer login` opens a browser and blocks with no timeout, so the daemon runs it as a
   * tracked subprocess and hands back a job id. Everything here is that job's client: start it,
   * poll it about once a second, and — when a pull is what needed the session — run that pull
   * again afterwards so the developer gets the pull's real result, not "try again".
   *
   * No token is ever read or written here. /api/config carries none, and the only credential
   * operations are "start a login" and "log out". */
  // `expired` and `refresh_failed` are both dead sessions, but only one of them might still
  // refresh itself, so they do not share a label.
  //
  // `renewable` is a HEALTHY session whose access token is merely past its expiry — the next
  // sync renews it with no interaction, which is what tokens minted with expires_in 3600 do
  // every hour. It MUST have an entry here: an unknown state falls through to
  // `session.logged_in === true ? "logged_in" : "none"`, and api._config derives logged_in as
  // `state === "logged_in"`, so a missing entry would badge a working session "signed out".
  const LOGIN_LABEL = {
    logged_in: "signed in",
    renewable: "renews on next sync",
    expired: "session expired",
    refresh_failed: "refresh rejected",
    static_only: "static token only",
    none: "signed out",
  };
  const LOGIN_TONE = {
    logged_in: "badge-approved",
    renewable: "badge-approved",
    expired: "badge-warn",
    refresh_failed: "badge-rejected",
    static_only: "badge-warn",
    none: "",
  };
  const WAITING = "waiting for your browser… finish the sign-in there";
  // state.confirm otherwise holds a decision id, so a bare word cannot collide with one.
  const LOGOUT_CONFIRM = "logout";

  /** The five states of `auth_state`. An older daemon sends only `logged_in`; deriving a state
   *  from it beats inventing one, and beats rendering an empty badge. */
  function sessionState(session) {
    const s = String((session && session.state) || "");
    if (LOGIN_LABEL[s]) return s;
    return session && session.logged_in === true ? "logged_in" : "none";
  }

  /** Repaints the waiting/failure line between renders, so a poll tick costs no refetch. */
  function paintLoginStatus() {
    const text = state.login.polling ? state.login.message : state.login.error;
    for (const node of document.querySelectorAll(".login-status")) node.textContent = text;
  }

  function loginStatusLine() {
    const failed = !state.login.polling && !!state.login.error;
    return h("span", {
      class: "login-status" + (failed ? " is-bad" : ""),
      text: state.login.polling ? state.login.message : state.login.error,
    });
  }

  function loginButton(label, after) {
    return h("button", {
      class: "btn btn-primary btn-sm",
      type: "button",
      text: label,
      props: { disabled: state.login.polling },
      on: { click: () => startLogin(after) },
    });
  }

  function stopLogin(errorMessage) {
    if (state.login.timer) window.clearTimeout(state.login.timer);
    state.login.timer = 0;
    state.login.polling = false;
    state.login.attached = false;
    state.login.job = "";
    state.login.after = null;
    state.login.message = "";
    state.login.error = errorMessage || "";
  }

  function scheduleLoginTick() {
    if (state.login.timer) window.clearTimeout(state.login.timer);
    state.login.timer = window.setTimeout(loginTick, LOGIN_POLL_MS);
  }

  /** `after` is what the login was for: null, or { kind: "pull", slug }. */
  async function startLogin(after) {
    if (state.login.polling || state.busy) return;
    state.login.after = after || null;
    state.login.attached = false;
    state.login.job = "";
    state.login.error = "";
    state.login.message = WAITING;
    state.login.polling = true;
    state.login.deadline = Date.now() + LOGIN_MAX_MS;
    await render();

    let out = null;
    try {
      out = await mutate("/api/login", "POST");
    } catch (err) {
      if (err instanceof NetworkError) {
        // The banner already carries the transport detail; overwriting it would lose the evidence.
        stopLogin("");
        await render();
        return;
      }
      if (err instanceof BusyError) {
        stopLogin(err.message);
        await render();
        return;
      }
      if (err && err.status === 409) {
        // Single-flight: a login is already running, started by another tab or a terminal. The
        // daemon names that job in the 409 body precisely so this tab can follow the SAME job and
        // learn if it FAILS. Watching /api/config instead only ever sees a session go live, so a
        // login the user denied left this tab waiting out the whole LOGIN_MAX_MS backstop and then
        // reporting a generic timeout. Attach to the session state only when no job is named.
        const job = err.data && err.data.job ? String(err.data.job) : "";
        state.login.job = job;
        state.login.attached = !job;
        state.login.message = String(err.message || "a login is already running");
        scheduleLoginTick();
        paintLoginStatus();
        return;
      }
      stopLogin(String((err && err.message) || "Could not start the login."));
      await render();
      return;
    }
    state.login.job = out && out.job ? String(out.job) : "";
    if (!state.login.job) {
      stopLogin("The daemon accepted the login but reported no job to follow.");
      await render();
      return;
    }
    scheduleLoginTick();
    paintLoginStatus();
  }

  async function loginTick() {
    state.login.timer = 0;
    if (!state.login.polling) return;
    if (Date.now() > state.login.deadline) {
      stopLogin("Stopped waiting for the login. Check the browser window, or start one again.");
      await render();
      return;
    }
    // A login wait is user-driven, so these GETs must NOT be marked as polls: the daemon's idle
    // watchdog ignores polls, and it kills the login subprocess when it exits.
    state.pollMode = false;
    const path = state.login.attached
      ? "/api/config"
      : "/api/login/status?job=" + encodeURIComponent(state.login.job);
    let out;
    try {
      out = await req(path);
    } catch (err) {
      // Keep waiting through a transport failure: req() already retried and put the reason on the
      // banner, and the deadline above is what ends an unanswered wait.
      if (err instanceof NetworkError) {
        scheduleLoginTick();
        return;
      }
      stopLogin(String((err && err.message) || "Lost track of the login."));
      await render();
      return;
    }
    if (state.login.attached) {
      if (sessionState((out && out.login) || {}) === "logged_in") await finishLogin("");
      else scheduleLoginTick();
      return;
    }
    const st = String((out && out.state) || "");
    const message = out && out.message ? String(out.message) : "";
    if (st === "ok") {
      await finishLogin(message);
      return;
    }
    if (st === "failed") {
      stopLogin(message || "The login did not complete.");
      await render();
      return;
    }
    if (message) state.login.message = message;
    paintLoginStatus();
    scheduleLoginTick();
  }

  async function finishLogin(message) {
    const after = state.login.after; // captured: stopLogin clears it
    stopLogin("");
    if (after && after.kind === "pull") {
      toast("Signed in — running the pull again.");
      await pullNow(after.slug);
      return;
    }
    toast(message || "Signed in to Contexer Teams.");
    await render();
  }

  /** A pull is the one action that can fail for want of a Teams session, so it does not go
   *  through act(): an `auth: true` failure becomes a panel with a Log in button that re-runs
   *  this same pull, and a failure WITHOUT it keeps the plain message — nobody should be told to
   *  re-login because their network is down. */
  async function pullNow(slug) {
    if (state.busy) return null;
    state.authPanel = null;
    let out;
    try {
      out = await mutate("/api/store/" + encodeURIComponent(slug) + "/pull", "POST");
    } catch (err) {
      if (err instanceof BusyError) {
        toast(err.message, true);
        return null;
      }
      if (err instanceof NetworkError) return null;
      const data = (err && err.data) || null;
      if (data && data.auth === true) {
        state.authPanel = authFailure(slug, data.error || err.message, data.state);
        await render();
        return null;
      }
      toast(String((err && err.message) || "Pull failed."), true);
      await render();
      return null;
    }
    if (out && out.error) {
      if (out.auth === true) state.authPanel = authFailure(slug, out.error, out.state);
      else toast(String(out.error), true);
    } else if (out && out.message) {
      toast(String(out.message));
    } else {
      toast("Pulled.");
    }
    await render();
    return out;
  }

  function authFailure(slug, message, sessionKind) {
    return {
      slug,
      message: String(message || "This machine has no usable Teams session."),
      state: String(sessionKind || ""),
    };
  }

  /** The actionable half of a failed pull: the daemon's own sentence plus the one button that
   *  fixes it. Built per render, never a statically hidden element — nothing to strand. */
  function authRecoveryPanel(slug) {
    const panel = state.authPanel;
    if (!panel || panel.slug !== slug) return null;
    return notice("bad", [
      h("strong", { text: "This pull needs a Teams sign-in." }),
      h("div", { text: panel.message }),
      panel.state ? h("div", { class: "mono", text: "session " + panel.state }) : null,
      h("div", { class: "btn-row mt-2" }, [
        loginButton("Log in", { kind: "pull", slug }),
        h("button", {
          class: "btn btn-ghost btn-sm",
          type: "button",
          text: "Dismiss",
          on: {
            click: () => {
              state.authPanel = null;
              render();
            },
          },
        }),
        loginStatusLine(),
      ]),
      h("div", {
        class: "muted",
        text: "Signing in re-runs this pull and reports what it actually did.",
      }),
    ]);
  }

  // ── Routing ───────────────────────────────────────────────────────────────────────────
  function parseHash() {
    const raw = String(window.location.hash || "").replace(/^#/, "");
    const parts = raw.split("/").filter((p) => p !== "");
    const dec = (s) => {
      try {
        return decodeURIComponent(s);
      } catch (err) {
        return s;
      }
    };
    if (parts[0] === "config") return { name: "config", slug: "", id: "" };
    if (parts[0] === "global") return { name: "global", slug: "", id: "" };
    if (parts[0] === "team") return { name: "team", slug: dec(parts[1] || ""), id: "" };
    if (parts[0] === "store" && parts[1]) {
      const slug = dec(parts[1]);
      const leaf = parts[2] || "";
      if (leaf === "decisions") return { name: "decisions", slug, id: dec(parts[3] || "") };
      if (leaf === "sessions") return { name: "sessions", slug, id: dec(parts[3] || "") };
      if (leaf === "review") return { name: "review", slug, id: "" };
      if (leaf === "deleted") return { name: "deleted", slug, id: "" };
      return { name: "dashboard", slug, id: "" };
    }
    return { name: "", slug: "", id: "" };
  }

  function hrefFor(name, slug, id) {
    const s = encodeURIComponent(slug || state.slug || "");
    if (name === "config") return "#/config";
    if (name === "global") return "#/global";
    if (name === "team") return "#/team/" + s;
    if (name === "dashboard") return "#/store/" + s;
    if (name === "decisions")
      return "#/store/" + s + "/decisions" + (id ? "/" + encodeURIComponent(id) : "");
    if (name === "sessions")
      return "#/store/" + s + "/sessions" + (id ? "/" + encodeURIComponent(id) : "");
    if (name === "review") return "#/store/" + s + "/review";
    if (name === "deleted") return "#/store/" + s + "/deleted";
    return "#/config";
  }

  function go(hash) {
    if (window.location.hash === hash) render();
    else window.location.hash = hash;
  }

  // ── Sidebar ───────────────────────────────────────────────────────────────────────────
  const navEl = document.getElementById("nav");
  const switcherEl = document.getElementById("switcher");
  const switcherMenu = document.getElementById("switcher-menu");

  function currentStoreRow() {
    return state.stores.find((s) => s && s.slug === state.slug) || null;
  }

  function paintSidebar() {
    const row = currentStoreRow();
    const nameEl = document.getElementById("switcher-name");
    const metaEl = document.getElementById("switcher-meta");
    const emblem = document.getElementById("switcher-emblem");
    const name = row ? String(row.name || row.slug) : state.stores.length ? "select a repo" : "no stores";
    nameEl.textContent = name;
    nameEl.title = row ? String(row.repo_path || "") : "";
    metaEl.textContent = row ? String(row.repo_path || "") : "";
    emblem.textContent = name.slice(0, 1).toUpperCase() || "-";

    clear(switcherMenu);
    if (state.stores.length === 0) {
      switcherMenu.appendChild(
        h("div", { class: "switcher-opt muted", text: "No stores in ~/.contexer" })
      );
    }
    for (const s of state.stores) {
      const isCurrent = s.slug === state.slug;
      const opt = h(
        "a",
        {
          class: "switcher-opt" + (isCurrent ? " is-current" : ""),
          href: hrefFor("dashboard", s.slug),
          title: String(s.repo_path || ""),
          on: {
            click: () => {
              switcherEl.open = false;
            },
          },
        },
        [
          isCurrent ? tpl("tpl-check") : h("span", { class: "check-spacer" }),
          h("span", { class: "switcher-opt-ident" }, [
            h("span", { class: "switcher-opt-name", text: String(s.name || s.slug) }),
            h("span", { class: "switcher-opt-meta", text: String(s.repo_path || "") }),
          ]),
          h("span", {
            class: "switcher-opt-count",
            text: s.ok === false ? "unreadable" : num(s.decisions) + (s.is_current ? " · here" : ""),
          }),
        ]
      );
      switcherMenu.appendChild(opt);
    }

    const activeNav = {
      dashboard: "dashboard",
      decisions: "decisions",
      sessions: "sessions",
      review: "review",
      global: "global",
      team: "team",
      deleted: "deleted",
      config: "config",
    }[state.route.name];

    for (const item of navEl.querySelectorAll(".nav-item")) {
      const key = item.getAttribute("data-nav");
      const on = key === activeNav;
      item.classList.toggle("is-active", on);
      if (on) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
      item.setAttribute("href", hrefFor(key, state.slug));
    }

    const badges = {
      decisions: row && row.ok !== false ? num(row.decisions) : null,
      pending: row && row.ok !== false ? num(row.pending) : null,
      // "!" rather than a count: an unreadable sidecar has no trustworthy number, and 0 would
      // read as "nothing deleted" — the one thing it does not mean.
      deleted:
        row && row.ok !== false
          ? state.tombstonesOk === false
            ? "!"
            : num(row.tombstoned)
          : null,
      global: state.globalOk === false ? "!" : state.counts.global,
      team: state.counts.team,
    };
    for (const key of Object.keys(badges)) {
      const node = navEl.querySelector('[data-count="' + key + '"]');
      if (!node) continue;
      const v = badges[key];
      if (v === null || v === undefined || v === 0) {
        node.hidden = true;
        node.textContent = "";
      } else {
        node.hidden = false;
        node.textContent = String(v);
        node.classList.toggle("is-quiet", key !== "pending");
      }
    }

    const title = VIEW_TITLE[activeNav];
    document.title = title
      ? title.label + (title.scoped && row ? " · " + name : "") + " · Contexer Console"
      : "Contexer Console";

    document.getElementById("version-line").textContent =
      "contexer " + (state.version ? "v" + state.version : "—");
    document.getElementById("port-line").textContent = state.port ? "127.0.0.1:" + state.port : "";
  }

  // ── Shared components ─────────────────────────────────────────────────────────────────
  function statusBadge(status) {
    const s = String(status || "approved");
    return h("span", {
      class: "badge badge-" + (s === "pending_approval" ? "pending" : s),
      text: STATUS_LABEL[s] || s,
    });
  }

  /** Subtype is a colour as well as a word (see console.css "Subtype colour"). An unknown value
   *  degrades to the neutral chip rather than throwing: the taxonomy is enforced at the write
   *  path, and a display helper is the wrong place to re-litigate it. */
  function subtypeBadge(subtype) {
    const s = String(subtype || "");
    const tone = SUBTYPE_CLASS[s];
    return h("span", {
      class: "badge" + (tone ? " " + tone : ""),
      text: s || "unclassified",
    });
  }

  /** The row's left stripe, carrying the same colour as its chip. Returns null — which `h` drops
   *  — for anything outside the taxonomy, so an unknown subtype gets no stripe rather than an
   *  attribute no rule matches. */
  function subtypeAttr(subtype) {
    const s = String(subtype || "");
    return SUBTYPE_CLASS[s] ? s : null;
  }

  function statCard(label, value, hint, href, signal) {
    const kids = [
      h("span", { class: "stat-label", text: label }),
      h("span", { class: "stat-value" + (signal ? " is-signal" : ""), text: String(value) }),
      hint ? h("span", { class: "stat-hint", text: hint }) : null,
    ];
    return href
      ? h("a", { class: "stat-card", href }, kids)
      : h("div", { class: "stat-card" }, kids);
  }

  /** The bar width is data-driven, so it is set through the CSSOM. Never a style attribute:
   *  `style-src 'self'` without 'unsafe-inline' blocks inline style attributes. */
  function barFill(cls, pct) {
    const node = h("span", { class: "bar-fill" + (cls ? " " + cls : "") });
    node.style.setProperty("width", pct + "%");
    return node;
  }

  function barRows(rows, tone) {
    const max = rows.reduce((m, r) => Math.max(m, num(r.count)), 0) || 1;
    return h(
      "div",
      { class: "bars" },
      rows.map((r) => {
        const pct = Math.round((num(r.count) / max) * 100);
        const cls = typeof tone === "function" ? tone(r) : "";
        return h("div", { class: "bar-row" }, [
          h("span", { class: "bar-label", text: String(r.label) }),
          h("span", { class: "bar-track" }, [
            barFill(cls, Math.max(pct, 2)),
          ]),
          h("span", { class: "bar-count", text: String(num(r.count)) }),
        ]);
      })
    );
  }

  function emptyState(title, kids) {
    return h("div", { class: "empty" }, [
      tpl("tpl-empty-glyph"),
      h("div", { class: "empty-title", text: title }),
      h("div", { class: "empty-text" }, kids),
    ]);
  }

  function notice(kind, kids) {
    return h("div", { class: "notice" + (kind ? " is-" + kind : "") }, [
      h("div", { class: "notice-body" }, kids),
    ]);
  }

  function bootstrapHint() {
    return [
      "Nothing here yet. Start a coding session in this repo and run the ",
      h("code", { text: "/bootstrap" }),
      " flow (or let Contexer capture decisions as you work — ",
      h("code", { text: "contexer install" }),
      " wires the hooks).",
    ];
  }

  function unreadableNotice(error) {
    return notice("bad", [
      h("strong", { text: "Store unreadable." }),
      " Contexer could not parse this repo's store file, so its contents are unknown — this is not the same as an empty store.",
      error ? h("div", { class: "mono", text: String(error) }) : null,
    ]);
  }

  /** The global-rule file's own "I could not be parsed", given the same weight as the store's.
   *
   *  A corrupt ~/.contexer/_global.json used to read as an empty one: the view said "No global
   *  rules" over a file that still held them, and the Add button beside that sentence rewrote the
   *  file with the single new rule — destroying every global constraint on the machine. The write
   *  is refused now, but "no rules" over an unreadable file is still a lie about rules that apply
   *  to every repo the developer works in, so it gets a notice rather than an empty state. */
  function globalUnreadableNotice(error) {
    return notice("bad", [
      h("strong", { text: "Global rules unreadable." }),
      " Contexer could not parse ~/.contexer/_global.json, so which rules apply to every repo on this machine is unknown — this is not the same as having none. Adding is refused until the file is repaired or moved aside, so nothing can be overwritten.",
      error ? h("div", { class: "mono", text: String(error) }) : null,
    ]);
  }

  function tombstonesUnreadableNotice(error) {
    return notice("bad", [
      h("strong", { text: "Tombstones unreadable." }),
      " Contexer could not parse this repo's tombstone sidecar, so which decisions were deleted is unknown — this is not the same as nothing having been deleted. Deleting is refused until the file is moved aside, so nothing more can be lost.",
      error ? h("div", { class: "mono", text: String(error) }) : null,
    ]);
  }

  function pageHead(eyebrow, title, sub, meta, actions) {
    return h("div", { class: "page-head" }, [
      h("div", { class: "page-head-main" }, [
        eyebrow ? h("span", { class: "eyebrow", text: eyebrow }) : null,
        h("h1", { class: "page-title" }, title),
        sub ? h("p", { class: "page-sub" }, sub) : null,
        meta && meta.length
          ? h(
              "div",
              { class: "head-meta" },
              meta.filter((m) => m).map((m) => h("span", { text: m }))
            )
          : null,
      ]),
      actions ? h("div", { class: "page-actions" }, actions) : null,
    ]);
  }

  function decisionRow(slug, d, activeId) {
    const id = String(d.id || "");
    return h(
      "a",
      {
        class: "drow" + (id && id === activeId ? " is-active" : ""),
        href: hrefFor("decisions", slug, id),
        "data-subtype": subtypeAttr(d.subtype),
      },
      [
        h("span", { class: "drow-title", text: titleOf(d) }),
        h("span", { class: "drow-text", text: String(d.content || "") }),
        Array.isArray(d.source_files) && d.source_files.length
          ? h("span", { class: "drow-files mono", text: filesLabel(d.source_files) })
          : null,
        h("span", { class: "drow-meta" }, [
          subtypeBadge(d.subtype),
          statusBadge(d.status),
          d.has_proposal ? h("span", { class: "badge badge-warn", text: "update pending" }) : null,
          num(d.occurrence_count) > 1
            ? h("span", { class: "badge", text: "×" + num(d.occurrence_count) })
            : null,
          d.anchor_commit
            ? h("span", { class: "drow-when", text: "@" + shortId(d.anchor_commit) })
            : null,
          h("span", {
            class: "drow-when",
            text: fmtAgo(d.updated_at || d.timestamp) + " · " + shortId(id),
          }),
        ]),
      ]
    );
  }

  // ── Word-level diff (before/after for proposed updates) ───────────────────────────────
  function tokenize(s) {
    return String(s === null || s === undefined ? "" : s)
      .split(/(\s+)/)
      .filter((t) => t !== "");
  }

  function diffTokens(a, b) {
    const n = a.length;
    const m = b.length;
    if ((n + 1) * (m + 1) > DIFF_BUDGET) return null;
    const w = m + 1;
    const dp = new Uint32Array((n + 1) * w);
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i * w + j] =
          a[i] === b[j]
            ? dp[(i + 1) * w + j + 1] + 1
            : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
      }
    }
    const out = [];
    const push = (kind, v) => {
      const last = out[out.length - 1];
      if (last && last.kind === kind) last.v += v;
      else out.push({ kind, v });
    };
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        push("same", a[i]);
        i++;
        j++;
      } else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) {
        push("del", a[i]);
        i++;
      } else {
        push("ins", b[j]);
        j++;
      }
    }
    while (i < n) push("del", a[i++]);
    while (j < m) push("ins", b[j++]);
    return out;
  }

  /** Share of the diff's characters that are unchanged. 1 when there is nothing to compare. */
  function sameShare(parts) {
    let same = 0;
    let total = 0;
    for (const p of parts) {
      total += p.v.length;
      if (p.kind === "same") same += p.v.length;
    }
    return total ? same / total : 1;
  }

  /** One column: the stored text in full, with this side's own edits marked when marking helps. */
  function diffColumn(label, text, parts, keep) {
    const body = h("pre", { class: "code-block" });
    if (!parts) body.textContent = String(text || "");
    else
      for (const p of parts) {
        if (p.kind === "same") body.appendChild(document.createTextNode(p.v));
        else if (p.kind === keep)
          body.appendChild(
            h("span", { class: keep === "del" ? "diff-del" : "diff-ins", text: p.v })
          );
      }
    return h("div", {}, [h("div", { class: "diff-col-label", text: label }), body]);
  }

  /** A proposed update, always as stored-vs-proposed columns: the reviewer is approving the whole
   *  text, so the whole text is what they read, in the order they read prose.
   *
   *  The word-level marks are an aid on top of that, not the view itself, and they are drawn ONLY
   *  when the change is small (`DIFF_MARK_MIN_SAME`, or a short pair whatever it scores). A
   *  rewrite shares almost nothing, so LCS matches only filler — "the", "is", "access" — and
   *  marking it paints nearly every word, which reads as noise over both columns and hides the
   *  handful of words that did survive. Unmarked columns say the same thing more honestly there. */
  function diffView(before, after) {
    const parts = diffTokens(tokenize(before), tokenize(after));
    const short = String(before || "").length + String(after || "").length <= DIFF_MARK_MAX_CHARS;
    const mark = parts && (short || sameShare(parts) >= DIFF_MARK_MIN_SAME) ? parts : null;
    return h("div", { class: "diff-cols" }, [
      diffColumn("Stored now", before, mark, "del"),
      diffColumn("Proposed", after, mark, "ins"),
    ]);
  }

  // ── View: dashboard ───────────────────────────────────────────────────────────────────
  async function viewDashboard(slug) {
    const data = (await req("/api/store/" + encodeURIComponent(slug))) || {};
    const counts = data.counts || {};
    state.counts.global = num(counts.global);
    state.counts.team = num(counts.team);

    const health = data.health || {};
    const ok = data.ok !== false && health.ok !== false;
    const pending = asList(data.pending, "pending");
    const proposals = asList(data.proposals, "proposals");
    const recent = asList(data.recent, "recent");
    const stale = data.staleness || {};
    const tombstones = data.tombstones || {};
    state.tombstonesOk = tombstones.ok !== false;

    const head = pageHead(
      "· local store",
      [String(data.name || slug)],
      "Everything Contexer has stored for this repository, on this machine.",
      [
        data.repo_path ? String(data.repo_path) : null,
        "store touched " + (fmtAgo(data.mtime) || "—"),
        num(counts.decisions) + " rows",
      ],
      [
        h("a", { class: "btn btn-ghost btn-sm", href: hrefFor("decisions", slug), text: "Browse decisions" }),
      ]
    );

    if (!ok) {
      return frag([head, unreadableNotice(health.error || data.error)]);
    }

    if (num(counts.decisions) === 0 && num(counts.pending) === 0) {
      return frag([head, h("div", { class: "card" }, [emptyState("No decisions stored", bootstrapHint())])]);
    }

    const subtypeRows = asList(data.subtype_mix, "subtype_mix").map((r) => ({
      label: String(r.subtype || "unclassified"),
      count: num(r.count),
    }));
    const statusRows = asList(data.status_mix, "status_mix").map((r) => ({
      label: STATUS_LABEL[String(r.status)] || String(r.status),
      count: num(r.count),
      status: String(r.status),
    }));

    const cards = h("div", { class: "stat-grid" }, [
      statCard("Decisions", num(counts.decisions), "stored for this repo", hrefFor("decisions", slug)),
      statCard(
        "Needs you",
        num(counts.pending) + num(counts.proposed_updates),
        "pending + proposed updates",
        hrefFor("review", slug),
        num(counts.pending) + num(counts.proposed_updates) > 0
      ),
      statCard("Global rules", num(counts.global), "apply to every repo", hrefFor("global")),
      statCard("Team context", num(counts.team), "cached team rows", hrefFor("team", slug)),
    ]);

    const mix = h("div", { class: "grid-2" }, [
      h("div", { class: "card" }, [
        h("div", { class: "card-head" }, [h("h3", { class: "card-title", text: "By subtype" })]),
        subtypeRows.length
          ? barRows(subtypeRows)
          : h("p", { class: "muted", text: "No subtypes recorded." }),
      ]),
      h("div", { class: "card" }, [
        h("div", { class: "card-head" }, [h("h3", { class: "card-title", text: "By status" })]),
        statusRows.length
          ? barRows(statusRows, (r) =>
              r.status === "pending_approval"
                ? "is-warn"
                : r.status === "ignored"
                  ? "is-bad"
                  : r.status === "suggested"
                    ? "is-quiet"
                    : ""
            )
          : h("p", { class: "muted", text: "No statuses recorded." }),
      ]),
    ]);

    const needs = h("div", { class: "card" }, [
      h("div", { class: "card-head" }, [
        h("h3", { class: "card-title" }, [
          "Needs ",
          h("span", { class: "serif-em", text: "you" }),
        ]),
        pending.length || proposals.length
          ? h("a", { class: "btn btn-ghost btn-sm", href: hrefFor("review", slug), text: "Open review" })
          : null,
      ]),
      pending.length === 0 && proposals.length === 0
        ? h("p", { class: "muted", text: "Nothing waiting on your approval." })
        : h("div", { class: "list" }, [
            ...pending.map((d) => needsRow(slug, d, false)),
            ...proposals.map((p) => needsRow(slug, p, true)),
          ]),
    ]);

    const timeline = h("div", { class: "card" }, [
      h("div", { class: "card-head" }, [h("h3", { class: "card-title", text: "Recent" })]),
      recent.length === 0
        ? h("p", { class: "muted", text: "No recent activity." })
        : h(
            "div",
            { class: "tl" },
            recent.map((d) => {
              const ms = toMs(d.updated_at || d.timestamp);
              const fresh = ms !== null && Date.now() - ms < 86400000;
              return h("div", { class: "tl-item" + (fresh ? " is-fresh" : "") }, [
                h("a", {
                  class: "tl-title",
                  href: hrefFor("decisions", slug, d.id),
                  text: titleOf(d),
                }),
                h("div", { class: "tl-meta" }, [
                  subtypeBadge(d.subtype),
                  statusBadge(d.status),
                  h("span", { class: "drow-when", text: String(d.created_by || "ai") }),
                  h("span", { class: "drow-when", text: fmtAgo(d.updated_at || d.timestamp) }),
                ]),
              ]);
            })
          ),
    ]);

    const teamNote =
      stale.stale === true
        ? notice("warn", [
            "Team context last synced " +
              (fmtDuration(stale.age_seconds) ? fmtDuration(stale.age_seconds) + " ago" : "a while ago") +
              " — it may be stale. ",
            h("a", { class: "mono", href: hrefFor("team", slug), text: "pull now" }),
          ])
        : null;

    // Without this the tombstoned count reads 0 over a sidecar that may hold plenty.
    const tombNote =
      tombstones.ok === false
        ? notice("warn", [
            "Deleted decisions are unknown — this repo's tombstone sidecar could not be parsed. ",
            h("a", { class: "mono", href: hrefFor("deleted", slug), text: "open Deleted" }),
          ])
        : null;

    return frag([head, teamNote, tombNote, cards, needs, mix, timeline]);
  }

  function needsRow(slug, d, isProposal) {
    const id = String(d.id || "");
    const proposed = isProposal ? d.proposed || {} : null;
    const subtype = isProposal ? proposed.subtype || d.subtype : d.subtype;
    return h("div", { class: "drow", "data-subtype": subtypeAttr(subtype) }, [
      h("a", { class: "drow-title", href: hrefFor("decisions", slug, id), text: titleOf(d) }),
      h("span", {
        class: "drow-text",
        text: isProposal ? String(proposed.content || "") : String(d.content || ""),
      }),
      h("span", { class: "drow-meta" }, [
        subtypeBadge(subtype),
        isProposal
          ? h("span", { class: "badge badge-warn", text: "proposed update" })
          : statusBadge(d.status),
        h("span", { class: "drow-when", text: shortId(id) }),
        h("span", { class: "btn-row push" }, [
          h("button", {
            class: "btn btn-primary btn-sm",
            type: "button",
            text: "Approve",
            on: { click: () => approve(slug, id, "approve") },
          }),
          h("button", {
            class: "btn btn-danger btn-sm",
            type: "button",
            text: "Reject",
            on: { click: () => approve(slug, id, "reject") },
          }),
        ]),
      ]),
    ]);
  }

  function approve(slug, id, action) {
    return act(
      "/api/store/" + encodeURIComponent(slug) + "/decisions/" + encodeURIComponent(id) + "/approve",
      "POST",
      { action },
      action === "approve" ? "Approved." : "Rejected."
    );
  }

  // ── View: decisions ───────────────────────────────────────────────────────────────────
  async function viewDecisions(slug, id) {
    const f = state.filters;
    const params = new URLSearchParams();
    if (f.q) params.set("q", f.q);
    if (f.subtype) params.set("subtype", f.subtype);
    if (f.status) params.set("status", f.status);
    if (f.file) params.set("file", f.file);
    params.set("limit", String(PAGE_LIMIT));
    params.set("offset", "0");
    const listUrl =
      "/api/store/" + encodeURIComponent(slug) + "/decisions?" + params.toString();

    const [listData, detail] = await Promise.all([
      req(listUrl),
      id
        ? req(
            "/api/store/" + encodeURIComponent(slug) + "/decisions/" + encodeURIComponent(id)
          ).catch((err) => {
            if (err instanceof NetworkError) throw err;
            return { __error: (err && err.message) || "not found" };
          })
        : Promise.resolve(null),
    ]);

    const rows = asList(listData, "decisions");
    const total = listData && typeof listData.total === "number" ? listData.total : rows.length;
    const unreadable = listData && listData.ok === false;
    // One page, no pager: past PAGE_LIMIT the count and the list disagree, so say which is which
    // instead of printing a total over a shorter list.
    const truncated = total > rows.length;

    const head = pageHead(
      "· decisions",
      ["Stored ", h("span", { class: "serif-em", text: "decisions" })],
      "Search, inspect, edit or delete what this repo feeds to your agents.",
      [
        String(total) + " matching",
        truncated ? "showing the first " + rows.length : null,
        f.q ? 'query "' + f.q + '"' : null,
        f.file ? 'file "' + f.file + '"' : null,
      ]
    );

    const search = h("input", {
      class: "input",
      id: "search",
      type: "search",
      placeholder: "Search decisions",
      autocomplete: "off",
      props: { value: f.q },
      on: {
        input: (ev) => {
          f.q = ev.target.value;
          debouncedRender();
        },
      },
    });

    // Comma-separated (`a.py, b.py`) mirrors the API's own `file=a,b` convention (see
    // ui/api.py:_list_param), so one box covers filtering by several files at once.
    const fileFilter = h("input", {
      class: "input file-filter",
      type: "search",
      placeholder: "Filter by file (path or path,path)",
      autocomplete: "off",
      "aria-label": "Filter by file",
      props: { value: f.file },
      on: {
        input: (ev) => {
          f.file = ev.target.value;
          debouncedRender();
        },
      },
    });

    const toolbar = h("div", { class: "toolbar" }, [
      h("div", { class: "search-wrap" }, [
        search,
        f.q ? null : h("span", { class: "search-hint", text: "/" }),
      ]),
      fileFilter,
      selectFilter("subtype", SUBTYPES, "All subtypes"),
      selectFilter("status", STATUSES, "All statuses", (v) => STATUS_LABEL[v] || v),
      f.q || f.subtype || f.status || f.file
        ? h("button", {
            class: "btn btn-ghost btn-sm",
            type: "button",
            text: "Clear",
            on: {
              click: () => {
                state.filters = { q: "", subtype: "", status: "", file: "" };
                render();
              },
            },
          })
        : null,
    ]);

    if (unreadable) {
      return frag([head, unreadableNotice(listData && listData.error)]);
    }

    const list = h("div", { class: "card" }, [
      rows.length === 0
        ? emptyState(
            f.q || f.subtype || f.status || f.file ? "No matches" : "No decisions stored",
            f.q || f.subtype || f.status || f.file
              ? ["Nothing matches those filters."]
              : bootstrapHint()
          )
        : h("div", { class: "list list-scroll" }, rows.map((d) => decisionRow(slug, d, id))),
      truncated
        ? h("p", {
            class: "muted",
            text:
              "Showing the first " +
              rows.length +
              " of " +
              total +
              " matches. Search or filter to reach the rest.",
          })
        : null,
    ]);

    const pane = detail ? detailPane(slug, detail) : null;
    const split = h("div", { class: "split" + (pane ? "" : " is-single") }, [list, pane]);
    return frag([head, toolbar, split]);
  }

  function selectFilter(key, values, allLabel, labeller) {
    return h(
      "select",
      {
        class: "select",
        "aria-label": allLabel,
        props: { value: state.filters[key] },
        on: {
          change: (ev) => {
            state.filters[key] = ev.target.value;
            render();
          },
        },
      },
      [
        h("option", { value: "", text: allLabel, props: { selected: state.filters[key] === "" } }),
        ...values.map((v) =>
          h("option", {
            value: v,
            text: labeller ? labeller(v) : v,
            props: { selected: state.filters[key] === v },
          })
        ),
      ]
    );
  }

  function detailPane(slug, d) {
    if (d.__error) {
      return h("div", { class: "detail" }, [
        h("div", { class: "detail-head" }, [
          h("h2", { class: "detail-title", text: "Not found" }),
          closeButton(slug),
        ]),
        notice("bad", [String(d.__error)]),
      ]);
    }

    const id = String(d.id || "");
    const conf = d.confidence && typeof d.confidence === "object" ? d.confidence : {};
    const factors = Array.isArray(conf.factors) ? conf.factors : [];
    const revisions = asList(d.revisions, "revisions");
    const share = d.share || {};
    const editing = state.edit && state.edit.id === id;

    const chips = h("div", { class: "chips" }, [
      subtypeBadge(d.subtype),
      statusBadge(d.status),
      typeof conf.score === "number"
        ? h("span", { class: "badge badge-signal", text: "confidence " + conf.score + "%" })
        : null,
      num(d.occurrence_count) > 1
        ? h("span", { class: "badge", text: "seen ×" + num(d.occurrence_count) })
        : null,
      num(d.session_count) > 1
        ? h("span", { class: "badge", text: num(d.session_count) + " sessions" })
        : null,
      h("span", { class: "badge", text: "by " + String(d.created_by || "ai") }),
      d.memory_key ? h("span", { class: "badge", text: "memory" }) : null,
      share.shared
        ? h("span", { class: "badge badge-approved", text: "shared" + (share.shared_at ? " " + fmtAgo(share.shared_at) : "") })
        : share.queued
          ? h("span", { class: "badge badge-warn", text: "share queued" })
          : h("span", { class: "badge", text: "not shared" }),
    ]);

    const body = editing ? editForm(slug, d) : readBody(d, factors, conf);

    // The shared review block (Task 07): the SAME lines `review_pending` and `contexer review`
    // print, rendered as lines rather than re-assembled here, so the console cannot phrase a
    // category its own way and cannot silently stop showing one. Lines, not the structured
    // dict, is also what keeps the uncertain paths out of this file entirely - they arrive
    // already labelled "NOT anchored on approval" and there is nothing here to route anywhere.
    const impact = asList(d.impact_lines, "impact_lines");
    const impactBlock = impact.length
      ? h("div", { class: "block" }, [
          h("div", { class: "block-label", text: "What approving this does" }),
          h(
            "ul",
            { class: "factors" },
            impact.map((line) => h("li", { text: String(line) }))
          ),
        ])
      : null;

    const proposal = d.proposed_revision
      ? h("div", { class: "block" }, [
          h("div", { class: "block-label", text: "Proposed update awaiting review" }),
          diffView(d.content, d.proposed_revision.content),
          h("div", { class: "btn-row mt-2" }, [
            h("button", {
              class: "btn btn-primary btn-sm",
              type: "button",
              text: "Approve update",
              on: { click: () => approve(slug, id, "approve") },
            }),
            h("button", {
              class: "btn btn-danger btn-sm",
              type: "button",
              text: "Reject update",
              on: { click: () => approve(slug, id, "reject") },
            }),
          ]),
        ])
      : null;

    const revBlock = h("div", { class: "block" }, [
      h("div", {
        class: "block-label",
        text: "Revisions v1..v" + (revisions.length ? num(revisions[revisions.length - 1].version_number) : 1),
      }),
      h(
        "div",
        { class: "revs" },
        revisions.length === 0
          ? [h("div", { class: "rev-meta", text: "No revision history recorded." })]
          : revisions.map((r) => {
              const isCur =
                r.is_current === true ||
                (r.is_current === undefined && num(r.version_number) === num(d.revision));
              return h("div", { class: "rev" + (isCur ? " is-current" : "") }, [
                h("div", { class: "rev-head" }, [
                  h("span", { class: "rev-v", text: "v" + num(r.version_number) }),
                  h("span", { class: "rev-meta", text: "source " + String(r.source || "—") }),
                  h("span", { class: "rev-meta", text: fmtStamp(r.created_at) }),
                  typeof r.confidence_score === "number"
                    ? h("span", { class: "rev-meta", text: r.confidence_score + "%" })
                    : null,
                  isCur ? h("span", { class: "badge badge-signal", text: "current" }) : null,
                  r.approved_at ? h("span", { class: "badge badge-approved", text: "approved" }) : null,
                ]),
                h("div", { class: "rev-body", text: String(r.content || "") }),
              ]);
            })
      ),
    ]);

    const actions = editing
      ? null
      : h("div", { class: "block btn-row" }, [
          h("button", {
            class: "btn btn-ghost btn-sm",
            type: "button",
            text: "Edit",
            on: {
              click: () => {
                state.edit = {
                  id,
                  content: String(d.content || ""),
                  title: String(d.title || ""),
                  subtype: String(d.subtype || ""),
                  // What the store holds, kept beside the draft so a save can tell an actual
                  // subtype change from the field merely being on the form.
                  stored: String(d.subtype || ""),
                };
                render();
              },
            },
          }),
          state.confirm === id
            ? null
            : h("button", {
                class: "btn btn-danger btn-sm",
                type: "button",
                text: "Delete",
                on: {
                  click: () => {
                    state.confirm = id;
                    render();
                  },
                },
              }),
          d.status === "pending_approval"
            ? h("button", {
                class: "btn btn-primary btn-sm",
                type: "button",
                text: "Approve",
                on: { click: () => approve(slug, id, "approve") },
              })
            : null,
        ]);

    const confirmRow =
      state.confirm === id
        ? h("div", { class: "block confirm" }, [
            h("span", {
              class: "confirm-text",
              text: "Delete this decision? It moves to Deleted as a tombstone and stops reaching your agents.",
            }),
            h("button", {
              class: "btn btn-danger btn-sm",
              type: "button",
              text: "Delete",
              on: {
                click: async () => {
                  // Both the confirmation and the navigation are gated on the write: leaving the
                  // detail view for a decision that is still live told the developer it was gone.
                  const ok = await act(
                    "/api/store/" + encodeURIComponent(slug) + "/decisions/" + encodeURIComponent(id),
                    "DELETE",
                    undefined,
                    "Deleted — restorable from Deleted.",
                    () => {
                      state.confirm = "";
                    }
                  );
                  if (ok) go(hrefFor("decisions", slug));
                },
              },
            }),
            h("button", {
              class: "btn btn-ghost btn-sm",
              type: "button",
              text: "Cancel",
              on: {
                click: () => {
                  state.confirm = "";
                  render();
                },
              },
            }),
          ])
        : null;

    return h("div", { class: "detail" }, [
      h("div", { class: "detail-head" }, [
        h("h2", { class: "detail-title", text: titleOf(d) }),
        closeButton(slug),
      ]),
      chips,
      h("div", { class: "head-meta" }, [
        h("span", { text: "id " + shortId(id) }),
        h("span", { text: "created " + (fmtStamp(d.timestamp) || "—") }),
        h("span", { text: "updated " + (fmtStamp(d.updated_at) || "—") }),
        h("span", { text: "v" + num(d.revision) }),
      ]),
      body,
      impactBlock,
      proposal,
      revBlock,
      actions,
      confirmRow,
    ]);
  }

  function readBody(d, factors, conf) {
    const files = Array.isArray(d.source_files) ? d.source_files : [];
    return frag([
      h("div", { class: "block" }, [
        h("div", { class: "block-label", text: "Content" }),
        h("p", { class: "prose", text: String(d.content || "") }),
      ]),
      files.length
        ? h("div", { class: "block" }, [
            h("div", { class: "block-label", text: "Anchored files" }),
            h(
              "ul",
              { class: "factors" },
              files.map((path) => h("li", { text: String(path) }))
            ),
          ])
        : null,
      d.rationale
        ? h("div", { class: "block" }, [
            h("div", { class: "block-label", text: "Rationale" }),
            h("p", { class: "prose is-quiet", text: String(d.rationale) }),
          ])
        : null,
      factors.length
        ? h("div", { class: "block" }, [
            h("div", {
              class: "block-label",
              text:
                "Confidence factors" +
                (typeof conf.score === "number" ? " · " + conf.score + "%" : ""),
            }),
            h(
              "ul",
              { class: "factors" },
              factors.map((f) => h("li", { text: String(f) }))
            ),
          ])
        : null,
    ]);
  }

  function editForm(slug, d) {
    const id = String(d.id || "");
    const draft = state.edit;
    return h("div", { class: "block" }, [
      h("div", { class: "field" }, [
        h("span", { class: "field-label", text: "Title" }),
        h("input", {
          class: "input",
          type: "text",
          maxlength: "100",
          props: { value: draft.title },
          on: {
            input: (ev) => {
              draft.title = ev.target.value;
            },
          },
        }),
      ]),
      h("div", { class: "field mt-2" }, [
        h("span", { class: "field-label", text: "Subtype" }),
        h(
          "select",
          {
            class: "select",
            on: {
              change: (ev) => {
                draft.subtype = ev.target.value;
              },
            },
          },
          // An entry with no stored subtype gets an explicit "unclassified" option, selected.
          // Offering only the four real values showed "architecture" for a decision whose
          // subtype is "" — a value the store does not hold. The option is not offered once a
          // subtype IS set: "" means "leave it alone" on the wire, so it cannot clear one.
          (draft.stored === NO_SUBTYPE ? [NO_SUBTYPE].concat(SUBTYPES) : SUBTYPES).map((s) =>
            h("option", {
              value: s,
              text: s === NO_SUBTYPE ? "unclassified" : s,
              props: { selected: draft.subtype === s },
            })
          )
        ),
      ]),
      h("div", { class: "field mt-2" }, [
        h("span", { class: "field-label", text: "Content" }),
        h("textarea", {
          class: "textarea",
          maxlength: "8000",
          props: { value: draft.content },
          on: {
            input: (ev) => {
              draft.content = ev.target.value;
            },
          },
        }),
      ]),
      h("div", { class: "btn-row mt-3" }, [
        h("button", {
          class: "btn btn-primary btn-sm",
          type: "button",
          text: "Save revision",
          on: {
            click: async () => {
              const payload = {
                content: draft.content,
                title: draft.title,
                if_version: num(d.revision),
              };
              // Only when the developer actually moved the <select>. Sending the field on every
              // save is what made an unsubtyped decision uneditable: the payload carried "" while
              // the form displayed a subtype that was never stored.
              if (draft.subtype !== draft.stored) payload.subtype = draft.subtype;
              // The draft is closed in `onOk`, i.e. ONLY once the daemon has taken it. Clearing
              // it before the await threw away a rewritten decision on every rejection the write
              // can draw — a 409 from a concurrent MCP session, a 400 on empty content, a 429 off
              // the mutation budget, a 500, a NetworkError that req() deliberately does not retry.
              await act(
                "/api/store/" + encodeURIComponent(slug) + "/decisions/" + encodeURIComponent(id),
                "PATCH",
                payload,
                "Saved as a new revision.",
                () => {
                  state.edit = null;
                }
              );
            },
          },
        }),
        h("button", {
          class: "btn btn-ghost btn-sm",
          type: "button",
          text: "Cancel",
          on: {
            click: () => {
              state.edit = null;
              render();
            },
          },
        }),
        h("span", { class: "muted", text: "Editing appends v" + (num(d.revision) + 1) + "; history is kept." }),
      ]),
    ]);
  }

  function closeButton(slug) {
    return h("button", {
      class: "btn btn-ghost btn-sm detail-close",
      type: "button",
      text: "Close",
      title: "Esc",
      on: {
        click: () => {
          state.edit = null;
          state.confirm = "";
          go(hrefFor("decisions", slug));
        },
      },
    });
  }

  // ── View: sessions ────────────────────────────────────────────────────────────────────
  /** One row per originating session: short id (monospace), a relative date range, the decision
   *  count, and — only when non-zero — an open-count badge. The `session_id: null` bucket (predates
   *  session attribution) renders as "(no session recorded)" and still links to its own transcript
   *  via the literal `"none"` id `console_api.session_transcript` reserves for it. Always the FULL
   *  `session_id` in the href, never `short_id` — short ids are not unique on prefix collisions. */
  function captureSessionRow(slug, r) {
    const sid = String(r.session_id || "");
    const label = sid ? shortId(sid) : "(no session recorded)";
    return h("a", { class: "drow", href: hrefFor("sessions", slug, sid || "none") }, [
      h("span", { class: "drow-title mono", text: label }),
      h("span", { class: "drow-meta" }, [
        h("span", { class: "drow-when", text: fmtAgo(r.first_at) + " – " + fmtAgo(r.last_at) }),
        h("span", { class: "badge", text: num(r.count) + (num(r.count) === 1 ? " decision" : " decisions") }),
        num(r.open_count) > 0
          ? h("span", { class: "badge badge-warn", text: num(r.open_count) + " open" })
          : null,
      ]),
    ]);
  }

  /** List mode (no id) shows one row per session; detail mode (id given) shows that session's
   *  transcript. `id` reaches here already through `hrefFor`, so it is always a full session id or
   *  the literal `"none"` — never a bare `short_id`. */
  async function viewSessions(slug, id) {
    if (id) {
      const data = await req(
        "/api/store/" + encodeURIComponent(slug) + "/sessions/" + encodeURIComponent(id)
      ).catch((err) => {
        if (err instanceof NetworkError) throw err;
        return { __error: (err && err.message) || "no such session" };
      });
      return sessionDetail(slug, data);
    }

    const data = (await req("/api/store/" + encodeURIComponent(slug) + "/sessions")) || {};
    const rows = asList(data, "sessions");

    const head = pageHead(
      "· sessions",
      ["Capture ", h("span", { class: "serif-em", text: "sessions" })],
      "One row per session that originated a decision in this store, newest activity first.",
      [num(data.total_decisions) + " decisions", rows.length + " sessions"]
    );

    if (rows.length === 0) {
      return frag([head, h("div", { class: "card" }, [emptyState("No sessions recorded", bootstrapHint())])]);
    }

    const importNote =
      num(data.memory_import_count) > 0
        ? h("p", {
            class: "muted",
            text:
              num(data.memory_import_count) +
              " memory-imported decisions not shown - imports, not session activity.",
          })
        : null;

    return frag([
      head,
      h("div", { class: "card" }, [
        h("div", { class: "list list-scroll" }, rows.map((r) => captureSessionRow(slug, r))),
      ]),
      importNote,
    ]);
  }

  /** The per-session transcript: an honest scope note (this is what one session captured, not a
   *  conversation log), an "Open threads" section first when anything in it is unreviewed, then
   *  the full transcript oldest-first, both built from the same `decisionRow` the Decisions view
   *  uses so each entry links to its real decision detail. */
  function sessionDetail(slug, d) {
    const back = h("a", { class: "btn btn-ghost btn-sm", href: hrefFor("sessions", slug), text: "Back to sessions" });

    if (d.__error) {
      return frag([
        pageHead("· sessions", ["Session not found"], null, null, [back]),
        notice("bad", [String(d.__error)]),
      ]);
    }

    const sid = String(d.session_id || "");
    const open = asList(d.open, "open");
    const entries = asList(d.entries, "entries");

    // Issue #261: the REAL underlying Claude Code conversation, when one exists locally for
    // this exact session - existence-gated by `transcript_available` (never constructed
    // speculatively), and always built from the full `session_id`, never `short_id`. A plain
    // same-origin link, not `hrefFor` (that builds internal hash routes; this is a direct API
    // URL the browser navigates to in a new tab, carrying the `ctx_ui` cookie automatically).
    const transcriptLink = d.transcript_available
      ? h("a", {
          class: "btn btn-ghost btn-sm",
          href: "/api/store/" + encodeURIComponent(slug) + "/sessions/" +
            encodeURIComponent(sid) + "/transcript/raw",
          target: "_blank",
          rel: "noopener",
          text: "View full transcript ↗",
        })
      : null;

    const head = pageHead(
      "· session",
      [sid ? shortId(sid) : "(no session recorded)"],
      "Capture-session transcript - decisions captured this session, not a conversation log.",
      [fmtAgo(d.first_at) + " – " + fmtAgo(d.last_at), num(d.count) + " decisions"],
      [back, transcriptLink]
    );

    if (entries.length === 0) {
      return frag([head, h("div", { class: "card" }, [emptyState("Nothing captured", ["This session has no decisions."])])]);
    }

    const openSection = open.length
      ? h("div", { class: "card" }, [
          h("div", { class: "card-head" }, [h("h3", { class: "card-title", text: "Open threads" })]),
          h("div", { class: "list" }, open.map((e) => decisionRow(slug, e, ""))),
        ])
      : null;

    const transcript = h("div", { class: "card" }, [
      h("div", { class: "card-head" }, [h("h3", { class: "card-title", text: "Transcript" })]),
      h("div", { class: "list" }, entries.map((e) => decisionRow(slug, e, ""))),
    ]);

    return frag([head, openSection, transcript]);
  }

  // ── View: review ──────────────────────────────────────────────────────────────────────
  async function viewReview(slug) {
    const data = (await req("/api/store/" + encodeURIComponent(slug))) || {};
    const counts = data.counts || {};
    state.counts.global = num(counts.global);
    state.counts.team = num(counts.team);
    const health = data.health || {};
    const pending = asList(data.pending, "pending");
    const proposals = asList(data.proposals, "proposals");

    const head = pageHead(
      "· review",
      ["Waiting on ", h("span", { class: "serif-em", text: "you" })],
      "Nothing here reaches an agent until you approve it. Rejecting maps to the store's ignore state, which is never surfaced again.",
      [pending.length + " pending", proposals.length + " proposed updates"]
    );

    if (data.ok === false || health.ok === false) {
      return frag([head, unreadableNotice(health.error || data.error)]);
    }

    if (pending.length === 0 && proposals.length === 0) {
      return frag([
        head,
        h("div", { class: "card" }, [
          emptyState("Nothing to review", [
            "New decisions your agent captures with low evidence land here first. ",
            h("a", { class: "mono", href: hrefFor("decisions", slug), text: "browse stored decisions" }),
          ]),
        ]),
      ]);
    }

    const pendingCards = pending.map((d) => {
      const id = String(d.id || "");
      return h("div", { class: "review-card" }, [
        h("div", { class: "review-head" }, [
          h("a", { class: "review-title", href: hrefFor("decisions", slug, id), text: titleOf(d) }),
          h("div", { class: "chips" }, [
            subtypeBadge(d.subtype),
            statusBadge(d.status),
            h("span", { class: "badge", text: "by " + String(d.created_by || "ai") }),
          ]),
        ]),
        h("p", { class: "prose", text: String(d.content || "") }),
        Array.isArray(d.confidence_factors) && d.confidence_factors.length
          ? h(
              "ul",
              { class: "factors" },
              d.confidence_factors.map((f) => h("li", { text: String(f) }))
            )
          : null,
        h("div", { class: "btn-row mt-3" }, [
          h("button", {
            class: "btn btn-primary btn-sm",
            type: "button",
            text: "Approve",
            on: { click: () => approve(slug, id, "approve") },
          }),
          h("button", {
            class: "btn btn-danger btn-sm",
            type: "button",
            text: "Reject",
            on: { click: () => approve(slug, id, "reject") },
          }),
          h("span", { class: "muted mono", text: shortId(id) }),
        ]),
      ]);
    });

    const proposalCards = proposals.map((p) => {
      const id = String(p.id || "");
      const cur = p.current || {};
      const prop = p.proposed || {};
      return h("div", { class: "review-card is-update" }, [
        h("div", { class: "review-head" }, [
          h("a", { class: "review-title", href: hrefFor("decisions", slug, id), text: titleOf(p) }),
          h("div", { class: "chips" }, [
            subtypeBadge(prop.subtype || p.subtype),
            h("span", { class: "badge badge-warn", text: "proposed update" }),
            h("span", { class: "badge", text: "source " + String(prop.source || "ai") }),
            typeof prop.confidence === "number"
              ? h("span", { class: "badge badge-signal", text: prop.confidence + "%" })
              : null,
          ]),
        ]),
        h("div", { class: "block-label", text: "Change against v" + num(cur.version_number || p.revision) }),
        diffView(cur.content, prop.content),
        Array.isArray(prop.confidence_factors) && prop.confidence_factors.length
          ? h(
              "ul",
              { class: "factors" },
              prop.confidence_factors.map((f) => h("li", { text: String(f) }))
            )
          : null,
        h("div", { class: "btn-row mt-3" }, [
          h("button", {
            class: "btn btn-primary btn-sm",
            type: "button",
            text: "Approve update",
            on: { click: () => approve(slug, id, "approve") },
          }),
          h("button", {
            class: "btn btn-danger btn-sm",
            type: "button",
            text: "Reject update",
            on: { click: () => approve(slug, id, "reject") },
          }),
          h("span", { class: "muted mono", text: "proposed " + (fmtStamp(prop.created_at) || "—") }),
        ]),
      ]);
    });

    return frag([
      head,
      h("div", { class: "stack" }, [
        proposals.length
          ? h("div", { class: "stack" }, [
              h("span", { class: "eyebrow", text: "Proposed updates" }),
              ...proposalCards,
            ])
          : null,
        pending.length
          ? h("div", { class: "stack" }, [
              h("span", { class: "eyebrow", text: "Pending decisions" }),
              ...pendingCards,
            ])
          : null,
      ]),
    ]);
  }

  // ── View: global ──────────────────────────────────────────────────────────────────────
  const globalDraft = { content: "", title: "", subtype: GLOBAL_SUBTYPES[0] };

  async function viewGlobal() {
    const data = await req("/api/global");
    const rules = asList(data, "rules");
    // `ok: false` is the file's own "I could not be parsed". An older daemon sends no such flag
    // and every payload it does send is readable, so the absence of the key is not a failure.
    const unreadable = !!data && data.ok === false;
    state.globalOk = !unreadable;
    state.counts.global = rules.length;

    const head = pageHead(
      "· global",
      ["Global ", h("span", { class: "serif-em", text: "rules" })],
      "Rules that apply to every repository on this machine. Kept in ~/.contexer/_global.json; only constraints and conventions are accepted.",
      [unreadable ? "rules unreadable" : rules.length + " rules"]
    );

    // No list and no Add form over a file whose contents are unknown: an empty list would claim
    // there are no rules, and the form would offer a write the store is going to refuse anyway.
    if (unreadable) {
      return frag([head, globalUnreadableNotice(data.error)]);
    }

    const list = h("div", { class: "card" }, [
      h("div", { class: "card-head" }, [h("h3", { class: "card-title", text: "Rules" })]),
      rules.length === 0
        ? emptyState("No global rules", [
            "Add one below, or tell your agent to store a rule that should hold in every repo.",
          ])
        : h(
            "div",
            { class: "list" },
            rules.map((r) => {
              const id = String(r.id || "");
              return h("div", { class: "drow is-static", "data-subtype": subtypeAttr(r.subtype) }, [
                h("span", { class: "drow-title", text: titleOf(r) }),
                h("p", { class: "prose", text: String(r.content || "") }),
                h("span", { class: "drow-meta" }, [
                  subtypeBadge(r.subtype),
                  h("span", { class: "badge", text: "by " + String(r.created_by || "ai") }),
                  h("span", { class: "drow-when", text: fmtStamp(r.timestamp) + " · " + shortId(id) }),
                  state.confirm === id
                    ? h("span", { class: "btn-row push" }, [
                        h("button", {
                          class: "btn btn-danger btn-sm",
                          type: "button",
                          text: "Confirm delete",
                          on: {
                            click: () => {
                              act("/api/global/" + encodeURIComponent(id), "DELETE", undefined,
                                  "Rule deleted.", () => {
                                    state.confirm = "";
                                  });
                            },
                          },
                        }),
                        h("button", {
                          class: "btn btn-ghost btn-sm",
                          type: "button",
                          text: "Cancel",
                          on: {
                            click: () => {
                              state.confirm = "";
                              render();
                            },
                          },
                        }),
                      ])
                    : h("button", {
                        class: "btn btn-danger btn-sm push",
                        type: "button",
                        text: "Delete",
                        on: {
                          click: () => {
                            state.confirm = id;
                            render();
                          },
                        },
                      }),
                ]),
              ]);
            })
          ),
    ]);

    const form = h("div", { class: "card" }, [
      h("div", { class: "card-head" }, [h("h3", { class: "card-title", text: "Add a global rule" })]),
      h("div", { class: "field" }, [
        h("span", { class: "field-label", text: "Rule" }),
        h("textarea", {
          class: "textarea is-short",
          id: "global-content",
          maxlength: "8000",
          placeholder: "Never commit generated files; regenerate them in CI instead.",
          props: { value: globalDraft.content },
          on: {
            input: (ev) => {
              globalDraft.content = ev.target.value;
            },
          },
        }),
      ]),
      h("div", { class: "grid-2 mt-2" }, [
        h("div", { class: "field" }, [
          h("span", { class: "field-label", text: "Title (optional)" }),
          h("input", {
            class: "input",
            type: "text",
            maxlength: "100",
            props: { value: globalDraft.title },
            on: {
              input: (ev) => {
                globalDraft.title = ev.target.value;
              },
            },
          }),
        ]),
        h("div", { class: "field" }, [
          h("span", { class: "field-label", text: "Subtype" }),
          h(
            "select",
            {
              class: "select",
              on: {
                change: (ev) => {
                  globalDraft.subtype = ev.target.value;
                },
              },
            },
            GLOBAL_SUBTYPES.map((s) =>
              h("option", { value: s, text: s, props: { selected: globalDraft.subtype === s } })
            )
          ),
        ]),
      ]),
      h("div", { class: "btn-row mt-3" }, [
        h("button", {
          class: "btn btn-primary btn-sm",
          type: "button",
          text: "Add rule",
          on: {
            click: async () => {
              if (!globalDraft.content.trim()) {
                toast("A rule needs content.", true);
                return;
              }
              const body = { content: globalDraft.content, subtype: globalDraft.subtype };
              if (globalDraft.title.trim()) body.title = globalDraft.title;
              // Emptied only once the rule is stored: a rejected POST used to leave a blank form
              // where the typed rule had been, with nothing to retry from.
              await act("/api/global", "POST", body, "Global rule added.", () => {
                globalDraft.content = "";
                globalDraft.title = "";
              });
            },
          },
        }),
      ]),
    ]);

    return frag([head, list, form]);
  }

  // ── View: team ────────────────────────────────────────────────────────────────────────
  /** The share selection for ONE repo. A selection is a set of ids that only mean anything to
   *  the store they were listed from, so asking for another slug starts an empty one rather than
   *  handing back the previous repo's — which is what let "3 selected" sit above five unchecked
   *  boxes and POST ids that repo could not resolve. Returning to the same repo keeps its ticks. */
  function shareSelectionFor(slug) {
    const key = String(slug || "");
    if (state.shareSel.slug !== key) {
      state.shareSel.slug = key;
      state.shareSel.ids = new Set();
    }
    return state.shareSel.ids;
  }

  async function viewTeam(slug) {
    const data = (await req("/api/team/" + encodeURIComponent(slug))) || {};
    const rows = asList(data.decisions, "decisions");
    const shareable = asList(data.shareable, "shareable");
    const stale = data.staleness || {};
    const last = data.last_sync || {};
    state.counts.team = rows.length;

    const head = pageHead(
      "· team",
      ["Team ", h("span", { class: "serif-em", text: "context" })],
      "Read-only team decisions cached on this machine, plus what you can share upward. The console never writes team decisions directly.",
      [
        data.repo_key ? "repo " + String(data.repo_key) : null,
        "mode " + String(data.mode || "local"),
        rows.length + " cached rows",
      ],
      [
        h("button", {
          class: "btn btn-primary btn-sm",
          type: "button",
          text: "Pull now",
          on: { click: () => pullNow(slug) },
        }),
      ]
    );

    const recovery = authRecoveryPanel(slug);

    if (data.enabled === false || String(data.mode || "local") !== "team") {
      return frag([
        head,
        recovery,
        h("div", { class: "card" }, [
          emptyState("Local mode", [
            "This machine is not connected to a team endpoint. Logging in connects it and writes the endpoint to ",
            h("code", { text: "config.toml" }),
            " — the same flow as ",
            h("code", { text: "contexer login" }),
            ".",
            h("div", { class: "btn-row mt-3" }, [loginButton("Log in", null), loginStatusLine()]),
          ]),
        ]),
      ]);
    }

    const staleNote =
      stale.stale === true
        ? notice("warn", [
            "Cache is stale — last successful sync " +
              (fmtDuration(stale.age_seconds) ? fmtDuration(stale.age_seconds) + " ago" : "unknown") +
              ".",
          ])
        : notice("", [
            "Last successful sync " +
              (fmtAgo(stale.last_ok_at) || "unknown") +
              (typeof last.duration_ms === "number" ? " · " + last.duration_ms + "ms" : "") +
              (num(last.consecutive_failures) > 0
                ? " · " + num(last.consecutive_failures) + " consecutive failures"
                : ""),
          ]);

    const cacheCard = h("div", { class: "card" }, [
      h("div", { class: "card-head" }, [
        h("h3", { class: "card-title", text: "Cached team decisions" }),
        h("span", { class: "muted mono", text: rows.length + " rows" }),
      ]),
      rows.length === 0
        ? emptyState("Nothing cached", ["Pull now, or ask a lead to approve team decisions for this repo."])
        : h(
            "div",
            { class: "list" },
            rows.map((r) =>
              h("div", { class: "drow is-static", "data-subtype": subtypeAttr(r.type) }, [
                h("span", { class: "drow-title", text: titleOf(r) }),
                h("span", { class: "drow-text", text: String(r.content || "") }),
                r.rationale
                  ? h("span", { class: "drow-text muted", text: String(r.rationale) })
                  : null,
                h("span", { class: "drow-meta" }, [
                  subtypeBadge(r.type),
                  h("span", { class: "badge badge-approved", text: String(r.scope || "team") }),
                  r.repo ? h("span", { class: "badge", text: String(r.repo) }) : null,
                  r.agent ? h("span", { class: "badge", text: String(r.agent) }) : null,
                  h("span", { class: "drow-when", text: shortId(r.id) }),
                ]),
              ])
            )
          ),
    ]);

    const selected = shareSelectionFor(slug);
    const picker = h("div", { class: "card" }, [
      h("div", { class: "card-head" }, [
        h("h3", { class: "card-title", text: "Share to team" }),
        h("span", { class: "muted mono", text: selected.size + " selected" }),
      ]),
      h("p", { class: "card-sub", text: "Sharing copies a local decision upward as a candidate; a lead approves it." }),
      shareable.length === 0
        ? emptyState("Nothing shareable", ["Approved local decisions become shareable here."])
        : frag([
            h(
              "div",
              { class: "list list-scroll" },
              shareable.map((d) => {
                const id = String(d.id || "");
                return h(
                  "label",
                  { class: "drow is-pick", "data-subtype": subtypeAttr(d.subtype || d.type) },
                  [
                    h("span", { class: "drow-meta" }, [
                      h("input", {
                        type: "checkbox",
                        props: { checked: selected.has(id) },
                        on: {
                          change: (ev) => {
                            if (ev.target.checked) selected.add(id);
                            else selected.delete(id);
                            render();
                          },
                        },
                      }),
                      h("span", { class: "drow-title", text: titleOf(d) }),
                    ]),
                    h("span", { class: "drow-text", text: String(d.content || "") }),
                    h("span", { class: "drow-meta" }, [
                      subtypeBadge(d.subtype || d.type),
                      d.shared
                        ? h("span", { class: "badge badge-approved", text: "shared " + fmtAgo(d.shared_at) })
                        : null,
                      num(d.redacted) > 0
                        ? h("span", { class: "badge badge-warn", text: num(d.redacted) + " secrets redacted" })
                        : null,
                      h("span", { class: "drow-when", text: shortId(id) }),
                    ]),
                  ]
                );
              })
            ),
            h("div", { class: "btn-row mt-3" }, [
              h("button", {
                class: "btn btn-primary btn-sm",
                type: "button",
                text: "Share selected",
                props: { disabled: selected.size === 0 },
                on: {
                  click: async () => {
                    // Re-resolved against `slug`, never read off the closure: if the repo changed
                    // between render and click, this hands back a fresh empty set rather than
                    // repo A's ids addressed to repo B.
                    const ids = Array.from(shareSelectionFor(slug));
                    if (ids.length === 0) {
                      toast("Nothing selected to share.", true);
                      await render();
                      return;
                    }
                    // Cleared in `onOk` only. Clearing first destroyed the selection even when
                    // the request was refused outright, and then toasted "Shared 5." over it.
                    await act(
                      "/api/store/" + encodeURIComponent(slug) + "/share",
                      "POST",
                      { ids },
                      "Shared " + ids.length + ".",
                      () => {
                        for (const id of ids) selected.delete(id);
                      }
                    );
                  },
                },
              }),
              selected.size
                ? h("button", {
                    class: "btn btn-ghost btn-sm",
                    type: "button",
                    text: "Clear selection",
                    on: {
                      click: () => {
                        selected.clear();
                        render();
                      },
                    },
                  })
                : null,
            ]),
          ]),
    ]);

    return frag([head, recovery, staleNote, cacheCard, picker]);
  }

  // ── View: deleted ─────────────────────────────────────────────────────────────────────
  async function viewDeleted(slug) {
    const data = await req("/api/store/" + encodeURIComponent(slug) + "/deleted");
    const rows = asList(data, "tombstones");
    // `ok: false` is the sidecar's own "I could not be parsed". Every other read of that file
    // degrades to an empty graveyard, so this flag is the only thing standing between the
    // developer and a view that says "nothing deleted" over a file full of tombstones.
    const unreadable = !!data && data.ok === false;
    state.tombstonesOk = !unreadable;

    const head = pageHead(
      "· deleted",
      ["Deleted ", h("span", { class: "serif-em", text: "decisions" })],
      "Tombstones live in a sidecar file, so a deleted decision cannot creep back in from CLAUDE.md or a mined conversation. Restoring puts it back in the live store.",
      [unreadable ? "tombstones unreadable" : rows.length + " tombstones"]
    );

    if (unreadable) {
      return frag([head, tombstonesUnreadableNotice(data.error)]);
    }

    if (rows.length === 0) {
      return frag([
        head,
        h("div", { class: "card" }, [
          emptyState("No tombstones", [
            "Deleting a decision from the ",
            h("a", { class: "mono", href: hrefFor("decisions", slug), text: "Decisions" }),
            " view puts it here.",
          ]),
        ]),
      ]);
    }

    const list = h(
      "div",
      { class: "list" },
      rows.map((d) => {
        const id = String(d.id || "");
        return h("div", { class: "drow is-static", "data-subtype": subtypeAttr(d.subtype) }, [
          h("span", { class: "drow-title", text: titleOf(d) }),
          h("p", { class: "prose is-quiet", text: String(d.content || "") }),
          h("span", { class: "drow-meta" }, [
            subtypeBadge(d.subtype),
            statusBadge(d.status),
            h("span", { class: "badge badge-rejected", text: "deleted " + fmtAgo(d.deleted_at) }),
            h("span", { class: "drow-when", text: "by " + String(d.deleted_by || "ui") + " · " + shortId(id) }),
            h("button", {
              class: "btn btn-ghost btn-sm push",
              type: "button",
              text: "Restore",
              on: {
                click: () =>
                  act(
                    "/api/store/" +
                      encodeURIComponent(slug) +
                      "/decisions/" +
                      encodeURIComponent(id) +
                      "/restore",
                    "POST",
                    undefined,
                    "Restored."
                  ),
              },
            }),
          ]),
        ]);
      })
    );

    return frag([head, h("div", { class: "card" }, [list])]);
  }

  // ── View: config ──────────────────────────────────────────────────────────────────────
  async function viewConfig() {
    const data = (await req("/api/config")) || {};
    const ui = data.ui || {};
    const profile = data.profile || {};
    const session = data.login || {};

    const head = pageHead(
      "· config",
      ["Console ", h("span", { class: "serif-em", text: "settings" })],
      "Written back to ~/.contexer/config.toml. No credential is shown or accepted here — the Teams session is started and ended, never edited.",
      [
        data.config_path ? String(data.config_path) : null,
        data.store_dir ? String(data.store_dir) : null,
        state.version ? "daemon v" + state.version : null,
      ]
    );

    const toggle = (label, hint, key, value) =>
      h("div", { class: "kv-row" }, [
        h("div", { class: "kv-key" }, [
          h("span", { class: "kv-name", text: label }),
          hint ? h("span", { class: "kv-hint" }, hint) : null,
        ]),
        h("label", { class: "switch" }, [
          h("input", {
            type: "checkbox",
            props: { checked: value === true },
            on: {
              change: (ev) => {
                const body = {};
                body[key] = ev.target.checked;
                act("/api/config", "PUT", body, "Saved.");
              },
            },
          }),
          h("span", { text: value === true ? "on" : "off" }),
        ]),
      ]);

    const numberRow = (label, hint, key, value, min, max) => {
      const draft = { v: String(num(value)) };
      return h("div", { class: "kv-row" }, [
        h("div", { class: "kv-key" }, [
          h("span", { class: "kv-name", text: label }),
          hint ? h("span", { class: "kv-hint" }, hint) : null,
        ]),
        h("div", { class: "btn-row" }, [
          h("input", {
            class: "input num-input",
            type: "number",
            min: String(min),
            max: String(max),
            props: { value: draft.v },
            on: {
              input: (ev) => {
                draft.v = ev.target.value;
              },
            },
          }),
          h("button", {
            class: "btn btn-ghost btn-sm",
            type: "button",
            text: "Save",
            on: {
              click: () => {
                const n = parseInt(draft.v, 10);
                if (!isFinite(n) || n < min || n > max) {
                  toast(label + " must be between " + min + " and " + max + ".", true);
                  return;
                }
                const body = {};
                body[key] = n;
                act("/api/config", "PUT", body, "Saved — restart the daemon to apply.");
              },
            },
          }),
        ]),
      ]);
    };

    const uiCard = h("div", { class: "card" }, [
      h("div", { class: "card-head" }, [h("h3", { class: "card-title", text: "Console [ui]" })]),
      h("div", { class: "kv" }, [
        toggle(
          "Autostart",
          [
            "Start this console automatically at session start. Off by default — installing Contexer must not imply a background listener.",
          ],
          "autostart",
          ui.autostart
        ),
        numberRow(
          "Port",
          ["Fixed so the printed URL survives restarts. Changing it needs a daemon restart."],
          "port",
          ui.port,
          1024,
          65535
        ),
        numberRow(
          "Idle timeout",
          ["Minutes of inactivity before the daemon exits. Polls from a background tab do not count."],
          "idle_timeout_minutes",
          ui.idle_timeout_minutes,
          1,
          1440
        ),
      ]),
    ]);

    const captureCard = h("div", { class: "card" }, [
      h("div", { class: "card-head" }, [h("h3", { class: "card-title", text: "Capture" })]),
      h("div", { class: "kv" }, [
        toggle(
          "Redact secrets",
          ["Scrub credential-shaped strings before anything leaves this machine. Leave on."],
          "redact_secrets",
          profile.redact_secrets
        ),
        toggle(
          "Skip share confirmation",
          ["Share without the interactive confirmation step in the CLI."],
          "skip_confirm",
          profile.skip_confirm
        ),
      ]),
    ]);

    const teamCard = h("div", { class: "card" }, [
      h("div", { class: "card-head" }, [h("h3", { class: "card-title", text: "Team connection" })]),
      h("div", { class: "kv" }, [
        kvRow("Mode", String(profile.mode || "local")),
        kvRow("Endpoint", profile.endpoint ? String(profile.endpoint) : "not set"),
        kvRow("Static token", profile.token_set ? "stored (never shown here)" : "not set"),
        sessionRow(session),
      ]),
      state.confirm === LOGOUT_CONFIRM
        ? h("div", { class: "block confirm" }, [
            h("span", {
              class: "confirm-text",
              text:
                "Log out of Contexer Teams? The stored credentials for this endpoint are removed " +
                "from this machine. Your local decisions are untouched, and pulling or sharing " +
                "needs a new login.",
            }),
            h("button", {
              class: "btn btn-danger btn-sm",
              type: "button",
              text: "Log out",
              on: {
                click: async () => {
                  await act("/api/logout", "POST", undefined, "Logged out.", () => {
                    state.confirm = "";
                  });
                },
              },
            }),
            h("button", {
              class: "btn btn-ghost btn-sm",
              type: "button",
              text: "Cancel",
              on: {
                click: () => {
                  state.confirm = "";
                  render();
                },
              },
            }),
          ])
        : null,
      notice("", [
        "This console can start a login and can log out; it never displays or accepts a token. ",
        h("code", { text: "/api/config" }),
        " carries no credential, and a ",
        h("code", { text: "PUT" }),
        " to it refuses credential keys — the token and endpoint are written by the login itself.",
      ]),
    ]);

    const caveat = notice("warn", [
      "Saving from the browser rewrites ",
      h("code", { text: "config.toml" }),
      " from its parsed values: hand-written comments are lost. A ",
      h("code", { text: "config.toml.bak" }),
      " copy is made first.",
    ]);

    return frag([head, caveat, uiCard, captureCard, teamCard]);
  }

  function kvRow(name, value) {
    return h("div", { class: "kv-row" }, [
      h("div", { class: "kv-key" }, [h("span", { class: "kv-name", text: name })]),
      h("div", { class: "kv-value", text: value }),
    ]);
  }

  /** The Teams session, as `auth_state` reports it: which of the five states, when it expires,
   *  the daemon's own sentence about it, and the two buttons. Never a token — the payload has
   *  none to render. */
  function sessionRow(session) {
    const kind = sessionState(session);
    const ms = toMs(session.expires_at);
    // An expiry in the past is the whole point of the row, so it says how long ago rather than
    // printing a stamp the reader has to date-compare themselves.
    const expiry = !session.expires_at
      ? "no expiry recorded"
      : ms !== null && ms < Date.now()
        ? "expired " + fmtStamp(session.expires_at) + " · " + fmtAgo(session.expires_at)
        : "expires " + fmtStamp(session.expires_at);
    const detail = [
      session.issuer ? "issuer " + String(session.issuer) : null,
      session.scope ? "scope " + String(session.scope) : null,
    ].filter((s) => s);

    return h("div", { class: "kv-row" }, [
      h("div", { class: "kv-key" }, [
        h("span", { class: "kv-name", text: "Teams session" }),
        session.message ? h("span", { class: "kv-hint", text: String(session.message) }) : null,
        detail.length ? h("span", { class: "kv-hint mono", text: detail.join(" · ") }) : null,
      ]),
      h("div", { class: "session-side" }, [
        h("div", { class: "chips" }, [
          h("span", {
            class: ("badge " + (LOGIN_TONE[kind] || "")).trim(),
            text: LOGIN_LABEL[kind],
          }),
          h("span", { class: "drow-when", text: expiry }),
        ]),
        h("div", { class: "btn-row" }, [
          loginButton(kind === "logged_in" ? "Log in again" : "Log in", null),
          kind === "none"
            ? null
            : h("button", {
                class: "btn btn-danger btn-sm",
                type: "button",
                text: "Log out",
                props: { disabled: state.login.polling },
                on: {
                  click: () => {
                    state.confirm = LOGOUT_CONFIRM;
                    render();
                  },
                },
              }),
          loginStatusLine(),
        ]),
      ]),
    ]);
  }

  // ── Render loop ───────────────────────────────────────────────────────────────────────
  const viewEl = document.getElementById("view");
  let renderSeq = 0;
  let debounceTimer = 0;

  function debouncedRender() {
    if (debounceTimer) window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(() => render(), 220);
  }

  /** Focused text field + caret survive a re-render (search box, edit textarea). */
  function captureFocus() {
    const a = document.activeElement;
    if (!a || !viewEl.contains(a)) return null;
    const tag = a.tagName;
    if (tag !== "INPUT" && tag !== "TEXTAREA") return null;
    return { id: a.id, start: a.selectionStart, end: a.selectionEnd };
  }

  function restoreFocus(snap) {
    if (!snap || !snap.id) return;
    const node = document.getElementById(snap.id);
    if (!node) return;
    node.focus();
    if (snap.start !== null && snap.start !== undefined && node.setSelectionRange) {
      try {
        node.setSelectionRange(snap.start, snap.end);
      } catch (err) {
        /* not a text-selectable input */
      }
    }
  }

  async function render(opts) {
    const o = opts || {};
    const seq = ++renderSeq;
    state.pollMode = !!o.poll;
    const route = parseHash();

    // Sidebar data first: it also resolves which store an empty/global route belongs to.
    let stores = state.stores;
    try {
      stores = asList(await req("/api/stores", { poll: o.poll }), "stores");
      state.stores = stores;
    } catch (err) {
      if (!(err instanceof NetworkError)) toast((err && err.message) || "Failed to list stores", true);
      if (o.poll) return;
    }

    if (route.slug) state.slug = route.slug;
    else if (!state.slug) {
      const cur = stores.find((s) => s && s.is_current) || stores[0];
      state.slug = cur ? String(cur.slug) : "";
    }

    if (!route.name) {
      // Landing: deep-link into the current repo, or config when there is no store at all.
      const target = state.slug ? hrefFor("dashboard", state.slug) : "#/config";
      if (window.location.hash !== target) {
        window.location.replace(target);
        return;
      }
      route.name = state.slug ? "dashboard" : "config";
      route.slug = state.slug;
    }
    if ((route.name === "team" || route.name === "dashboard") && !route.slug) route.slug = state.slug;
    state.route = route;
    paintSidebar();

    if (route.name !== "decisions") state.edit = null;

    const focus = captureFocus();
    const scroll = document.scrollingElement ? document.scrollingElement.scrollTop : 0;
    let node;
    try {
      if (route.name === "dashboard") node = await viewDashboard(route.slug);
      else if (route.name === "decisions") node = await viewDecisions(route.slug, route.id);
      else if (route.name === "sessions") node = await viewSessions(route.slug, route.id);
      else if (route.name === "review") node = await viewReview(route.slug);
      else if (route.name === "global") node = await viewGlobal();
      else if (route.name === "team") node = await viewTeam(route.slug);
      else if (route.name === "deleted") node = await viewDeleted(route.slug);
      else node = await viewConfig();
    } catch (err) {
      if (seq !== renderSeq) return;
      if (err instanceof NetworkError) return; // banner is up; keep the last good DOM
      node = frag([
        pageHead("· error", ["Could not load this view"], null, null, null),
        notice("bad", [
          String((err && err.message) || "Request failed"),
          err && err.data && err.data.incident
            ? h("div", { class: "mono", text: "incident " + String(err.data.incident) })
            : null,
        ]),
      ]);
    }
    if (seq !== renderSeq) return; // a newer render won
    clear(viewEl);
    viewEl.appendChild(node);
    if (document.scrollingElement && o.poll) document.scrollingElement.scrollTop = scroll;
    restoreFocus(focus);
    paintSidebar();
  }

  // ── Polling ───────────────────────────────────────────────────────────────────────────
  function pollTick() {
    if (document.visibilityState !== "visible") return;
    if (state.busy || state.edit || state.confirm) return;
    const a = document.activeElement;
    if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT")) return;
    if (switcherEl && switcherEl.open) return;
    render({ poll: true });
  }

  // ── Keyboard: only "/" and Escape ─────────────────────────────────────────────────────
  function onKeydown(ev) {
    const a = document.activeElement;
    const typing = a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT");
    if (ev.key === "/" && !typing && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      const box = document.getElementById("search");
      if (box) {
        ev.preventDefault();
        box.focus();
        box.select();
      }
      return;
    }
    if (ev.key === "Escape") {
      if (switcherEl && switcherEl.open) {
        switcherEl.open = false;
        return;
      }
      if (state.edit) {
        state.edit = null;
        render();
        return;
      }
      if (state.confirm) {
        state.confirm = "";
        render();
        return;
      }
      if (state.route.name === "decisions" && state.route.id) {
        go(hrefFor("decisions", state.route.slug));
      }
    }
  }

  // ── Boot ──────────────────────────────────────────────────────────────────────────────
  async function boot() {
    try {
      const info = await handshake();
      // The footer only needs the bound port; the address bar already knows it, so /healthz
      // returning `port` is a convenience, not a requirement.
      state.port = String((info && info.port) || (window.location && window.location.port) || "");
    } catch (err) {
      if (err instanceof NetworkError) {
        // req() has already put the real DOMException on the banner. Calling setDisconnected
        // again with no message would overwrite that detail with the generic line and throw away
        // the only evidence a transport failure ever produces.
        return;
      }
      const authFail = err && (err.status === 401 || err.status === 403);
      setDisconnected(
        true,
        authFail
          ? "This console session is not authenticated. Re-open the console URL printed at session start."
          : "The daemon returned an error" +
              (err && err.status ? " (HTTP " + err.status + ")" : "") +
              ". See ~/.contexer/ui.log."
      );
      return;
    }
    await render();
  }

  document.getElementById("banner-retry").addEventListener("click", async () => {
    setDisconnected(false);
    await boot();
  });
  window.addEventListener("hashchange", () => {
    state.confirm = "";
    // The recovery panel belongs to the pull that failed on the view being left. A login already
    // in flight is NOT cancelled — leaving the Team view mid-browser-flow is normal, and its
    // status line reappears wherever it is rendered next.
    state.authPanel = null;
    render();
  });
  window.addEventListener("keydown", onKeydown);
  // Native <details> does not close on an outside click; the switcher should.
  document.addEventListener("click", (ev) => {
    if (switcherEl && switcherEl.open && !switcherEl.contains(ev.target)) switcherEl.open = false;
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    // Clear the banner BEFORE re-rendering, not after. A stale banner over live data is worse
    // than no banner: the tab was frozen while it was hidden, so whatever failed then says
    // nothing about the daemon now. If the daemon really is gone, this render sets it again with
    // the real reason attached.
    setDisconnected(false);
    render({ poll: true });
  });
  window.setInterval(pollTick, POLL_MS);
  boot();
})();
