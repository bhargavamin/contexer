# Contexer demo video - shot list and script

This is the production script for the main demo video (~2.5 min) and the 15-second README GIF.
Every on-screen string below is the real output of the current release, so the recording never has to fake terminal output.
Run `docs/demo/seed.sh` first - it builds a clean, reproducible demo repo so takes can be re-shot without leftover state.

## Recording setup

- Terminal at 120x30, font 16-18pt, a high-contrast theme (record once in dark; the README GIF can stay dark).
- Hide personal paths: record inside `~/contexer-demo/taskflow` (the seed script creates it) and set a demo git identity (the seed script does).
- Recommended tools: Screen Studio or Cursorful for the narrated video; asciinema or `vhs` for the CLI-only inserts; the Claude Code scenes must be recorded live (real model output builds trust).
- Before each full take: `docs/demo/seed.sh --reset` for Act 1-2 scene A, or `docs/demo/seed.sh --seeded` to jump straight to the "second session" scenes.

## The story in one line

Your AI coding agent forgets every decision when the session ends; Contexer captures them as you work and replays them into every future session - across Claude Code, Cursor, Codex, and Gemini CLI - and, with Teams, across your whole team.

---

## Act 0 - the problem (0:00-0:15)

Cold open, fresh Claude Code session in the demo repo, no Contexer yet.

Type:

> why do we use Postgres here instead of DynamoDB?

The agent starts reading files and guessing.
Narration:

> "Every new AI session starts from zero. Your architecture decisions, your constraints, the reasons behind them - gone. You re-explain, or the agent guesses."

Cut on the agent mid-file-reading (do not wait for its full answer).

## Act 1 - install (0:15-0:30)

```bash
uv tool install contexer
contexer install
```

Expected output to keep in frame:

```
  ✓ MCP server registered in ~/.claude.json
  ✓ Hooks and permissions written to ~/.claude/settings.json
```

Narration:

> "Contexer is one command. It wires itself into Claude Code, Cursor, Codex, and Gemini CLI - whichever you have."

Restart Claude Code (show the relaunch, it is honest and fast).

## Act 2 - capture and replay: the aha (0:30-1:45)

### Scene A - session one, decisions happen naturally

In a Claude Code session, do real work that contains a decision:

> Let's use Postgres for task storage instead of DynamoDB - we need relational integrity across task/project joins and the team already runs RDS.

The agent implements and stores the decision (Contexer's hook reminds it silently; nothing interrupts the flow).

Then type a constraint as you naturally would:

> never log request bodies, they can contain PII

On-screen moment to zoom: the agent acknowledges with

> Stored as a constraint in Contexer.

Narration:

> "You just work. Decisions and constraints are captured as they happen - no forms, no wiki, no discipline required."

Close the session. Title card: "Next day. New session. Empty context window."

### Scene B - session two, the agent already knows

Start a fresh Claude Code session in the same repo.
Zoom on the session-start banner:

```
Contexer: 1 constraint, 1 convention, 1 pattern loaded. 1 architecture decision will be loaded on demand.
```

(That is the verified banner for `seed.sh --seeded`; live-captured decisions from scene A shift the counts, and any global rules you keep add a "N global rules" prefix.)

Ask the cold-open question again:

> why do we use Postgres here instead of DynamoDB?

This time the decision is injected instantly - the agent answers with the stored rationale and cites it, without reading a single file.
Narration:

> "A brand-new session - and it already knows. The decision, and the why. That question from the cold open? Answered from memory, not from guessing."

### Scene C - you stay in control (quick beat)

```bash
contexer review
```

Show the pending-approval flow for one AI-suggested decision (approve it on camera).
Narration:

> "AI-suggested decisions stay provisional until you approve them. You own the record."

## Act 3 - Teams (1:45-2:30)

Title card: "Your team's standards, in every agent."

### Scene A - lead publishes

Browser: app.contexer.ai - sign-up, create team "acme" (10 seconds max, just enough to show it is real).

```bash
contexer login        # browser OAuth flashes, credential stored
contexer share --all
```

Expected output to keep in frame:

```
Shared 4 decision(s) to your team's cloud context - teammates' agents pick them up once they clear your team's approval workflow.
```

Approve them in the app as the lead (one click each - show the role-based workflow briefly).

### Scene B - teammate's machine

Switch to a second machine or user account already logged in to the same team, same repo cloned.
Start a session; zoom the banner suffix:

```
Contexer: ... loaded. | team: 4 synced
```

Teammate asks the agent to add an endpoint that logs the request - and the agent refuses to log the body, citing the team constraint.
Narration:

> "One person captures it, a lead approves it, and every teammate's agent - in every tool - starts the session already knowing. Standards travel to the point of work."

## Outro (2:30-2:45)

Static card:

```
uv tool install contexer
contexer install
```

> "Contexer - the engineering decision layer for AI coding agents. Free and open source. Teams from $10 a month at contexer.ai."

---

## The 15-second README GIF

One cut, two scenes, no audio (this fills the TODO at the top of README.md):

1. Session 1 (5s): user types "mock at the service boundary, not the DB layer" - agent replies "Stored as a constraint in Contexer."
2. Hard cut, caption "next session" (1s).
3. Session 2 (9s): session-start banner "Contexer: 1 constraint loaded." - user asks for a test - agent mocks at the service boundary unprompted, with a one-line callout circling the injected constraint.

Keep it under 2 MB: 12-15 fps, terminal-only frame, no browser.

## Pre-flight checklist

- [ ] `contexer version` matches the latest PyPI release.
- [ ] `docs/demo/seed.sh --reset` run, store empty for the demo repo (`contexer status`).
- [ ] Demo git identity active (`git config user.email` inside the repo → demo@contexer.ai).
- [ ] Global rules trimmed: your real `~/.contexer/_global.json` leaks into every banner - move it aside for recording (`seed.sh` warns about this).
- [ ] Teams: demo team created on app.contexer.ai, second persona logged in, repo cloned there.
- [ ] Terminal notifications and personal dock/menubar hidden.
