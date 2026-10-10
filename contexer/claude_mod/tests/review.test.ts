// Behaviour of the review mod against the engine, with `contexer review --json` played by a
// test hook beneath the plugin (`process.run`). Run with `claude plugin test contexer/claude_mod`.
import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, RenderElement } from 'claude-code'

import type { CurrentConflict, ReviewItem } from '../types'

const SURFACES = ['terminal', 'desktop'] as const
type Surface = (typeof SURFACES)[number]

const NEW: ReviewItem = {
  id: 'aaaa1111', kind: 'new', title: 'Never delete rows for a deleted Clerk org',
  content: 'Never delete database rows for an organisation deleted in Clerk; warn and count instead',
  subtype: 'constraint', status: 'pending_approval', created_by: 'ai',
  origin: 'captured by the assistant', timestamp: '2026-10-01T10:00:00Z',
  applies_when: ['deleting organisations'], actions: ['approve', 'edit', 'ignore'],
}
const RETIRE: ReviewItem = {
  id: 'bbbb2222', kind: 'retirement', title: 'Use Postgres for the store', content: 'Use Postgres',
  subtype: 'architecture', status: 'approved', created_by: 'human', origin: 'your prompt',
  timestamp: '2026-09-01T10:00:00Z', applies_when: [], actions: [],
}
const UPDATE: ReviewItem = {
  id: 'cccc3333', kind: 'update', title: 'Store decisions in Postgres', content: 'Use Postgres',
  subtype: 'architecture', status: 'approved', created_by: 'human', origin: 'your prompt',
  timestamp: '2026-09-02T10:00:00Z', applies_when: ['the decision store'],
  actions: ['approve', 'edit', 'dismiss'], conflict: true, pick: 'update',
  proposed: { content: 'Use DynamoDB', title: 'Store decisions in DynamoDB', applies_when: ['the decision store', 'team sync'] },
}
const side = (id: string, title: string, can_keep = true) => ({
  id, title, content: title, status: can_keep ? 'approved' : 'suggested',
  timestamp: '2026-09-03T10:00:00Z', applies_when: [], can_keep,
})
const PAIR: CurrentConflict = {
  kind: 'current_conflict',
  reason: 'These current decisions prescribe incompatible version formats.',
  decisions: [side('dddd4444', 'Prefix versions with v'), side('eeee5555', 'Publish bare versions')],
  actions: ['keep'],
}

// The runtime's own timer, beneath the mocked clock; the mod's typings carry no DOM or Node lib.
const realTimer = (globalThis as unknown as { setTimeout: (done: () => void, ms: number) => unknown }).setTimeout

// What reached the prompt box's suggestion, beneath the plugin.
const suggested: string[] = []
// The mocked clock the timer-driven queue reads run on; made before the test's first `$` call.
let clock: ReturnType<typeof mock.clock>
// Settles an item from outside the pane (another session), for the next queue read to see.
let settleElsewhere: (id: string) => void = () => {}

// `held` holds the next queue read's reply (already snapshotted) until it resolves, to play a
// process that answers late. `action` holds the next action before its write lands, to play a
// store lock another process holds.
type Gate = { held?: Promise<void>; action?: Promise<void> }

// Every pane open the plugin made, and whether the surface seats it: a narrow terminal leaves
// an open the plugin makes on its own undrawn. `isOpenRefused` plays a `ui.open` that fails, and
// `isFocusRefused` a pane the engine reports unfocused (the band still holds the keys).
// `ui.panes` lists the pane from its first open until a close, placed while `placed` holds.
const opens: string[] = []
let isPaneUp = false
let isPanesRefused = false
let placed = true
let isOpenRefused = false
let isFocusRefused = false

// A fake `contexer review --json`: lists `items`; an action removes its item and replies with
// the queue as it stands afterwards. Records every argv it was run with.
function fakeContexer(on: On, items: ReviewItem[], protocol = 1, pairs: CurrentConflict[] = [], gate?: Gate) {
  const calls: string[][] = []
  suggested.length = 0
  clock = mock.clock(on)
  on('prompt.suggest', async (_$, e) => {
    suggested.push(e.text)
    return { isShown: true }
  })
  let pending = [...items]
  let open = [...pairs]
  const queue = () => ({ protocol, repo: '/repo', count: pending.length, items: pending, conflicts: open })
  settleElsewhere = id => { pending = pending.filter(item => item.id !== id) }
  on('process.run', async (_$, e) => {
    calls.push([...e.argv])
    const args = e.argv.slice(e.argv.indexOf('--json') + 1)
    if (args.length > 0 && gate?.action) {
      const held = gate.action
      gate.action = undefined
      await held
    }
    if (args.length > 0 && args[0] !== 'summarize') pending = pending.filter(item => item.id !== args[1])
    if (args[0] === 'keep') open = open.filter(pair => !pair.decisions.some(d => d.id === args[1]))
    const out = args.length === 0 ? queue() : { ok: true, message: `${args[0]} done`, queue: queue() }
    if (args.length === 0 && gate?.held) {
      const held = gate.held
      gate.held = undefined
      await held
    }
    return { value: { exitCode: 0, stdout: JSON.stringify(out), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.start', async (_$, e) => ({ cwd: e.cwd }))
  on('session.cwd', async () => ({ value: '/repo' }))
  on('prompt.submit', async (_$, e) => ({ text: e.text }))
  on('fs.stat', async () => ({ deny: 'no such file' }))
  on('command.register', async (_$, e) => ({ value: { command: e.name } }))
  opens.length = 0
  placed = true
  isOpenRefused = false
  isFocusRefused = false
  isPaneUp = false
  isPanesRefused = false
  on('ui.open', async (_$, e) => {
    if (isOpenRefused) return { deny: 'refused' }
    opens.push(e.id)
    isPaneUp = true
    return { value: placed ? { isPlaced: true } : { isPlaced: false, reason: 'narrow terminal' } }
  })
  on('ui.panes', async () => (isPanesRefused ? { deny: 'refused' } : {
    value: !isPaneUp ? [] : [{
      id: 'contexer-review', title: 'Contexer review', isShown: placed, isFocused: !isFocusRefused, isPlaced: placed,
    }],
  }))
  on('ui.close', async () => {
    isPaneUp = false
    return { value: undefined }
  })
  on('ui.render', async ($, e) => {
    const { Box } = $.ui.resolve(e)
    return h(Box, {}) as RenderElement
  })
  return calls
}

// Starts the session on `surface` and lets the timer-driven queue read land.
async function start($: Engine, surface: Surface): Promise<void> {
  await $.session.start({ cwd: '/repo', surface, isInteractive: true } as never)
  await clock.settle()
}

const mountBand = ($: Engine, surface: Surface) => $.ui.mount({
  plugin: 'contexer-review', surface, component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 4, columns: 100 } as never,
})

const mountPane = ($: Engine, surface: Surface) => $.ui.mount({
  plugin: 'contexer-review', surface, component: 'Pane', requestId: 'contexer-review',
  props: { title: 'Contexer review', isFocused: true, bodyColumns: 80 } as never,
})

describe('band above the prompt', () => {
  for (const surface of SURFACES) {
    test(`shows what the pane can settle and hides on Later (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW, RETIRE])
      await start($, surface)
      const band = await mountBand($, surface)
      expect(await band.find({ type: 'Text', text: /1 decision needs your call/ })).toBeDefined()
      await band.press({ key: 'contexer-later' })
      expect(await band.find({ key: 'contexer-open' })).toBeUndefined()
    })

    test(`steps aside while the pane is open (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      await $.command.run({ command: 'contexer-review', args: '' } as never)
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeUndefined()
      const pane = await mountPane($, surface)
      await pane.press({ key: 'close' })
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeDefined()
    })

    test(`Review opens the pane during the press, so it is seated at any width (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      const band = await mountBand($, surface)
      await band.press({ key: 'contexer-open' })
      // No clock.settle: an open made later from a timer counts as the plugin's own and waits
      // undrawn below 144 columns, which hid the band and showed nothing.
      expect(opens).toContain('contexer-review')
    })

    test(`the band stays when the pane waits undrawn (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      placed = false
      const band = await mountBand($, surface)
      await band.press({ key: 'contexer-open' })
      await clock.settle()
      expect(await band.find({ key: 'contexer-open' })).toBeDefined()
    })

    test(`the band steps aside once a pane left undrawn is drawn by a resize (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      placed = false
      const band = await mountBand($, surface)
      await band.press({ key: 'contexer-open' })
      await clock.settle()
      expect(await band.find({ key: 'contexer-open' })).toBeDefined()
      // The terminal widens: the surface seats the waiting pane, with no call of the mod's.
      placed = true
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeUndefined()
    })

    test(`the band stays when the pane list cannot be read (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      isPanesRefused = true
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeDefined()
    })

    test(`Review asks for the keyboard before the queue read answers (${surface})`, async ($, on) => {
      const gate: Gate = {}
      fakeContexer(on, [NEW], 1, [], gate)
      await start($, surface)
      isFocusRefused = true
      let release = () => {}
      gate.held = new Promise<void>(resolve => { release = resolve })
      const band = await mountBand($, surface)
      const pressing = band.press({ key: 'contexer-open' })
      await clock.advance(100)
      // The pane is seated and re-asked for focus while the read is still out.
      expect(opens.length).toBeGreaterThan(1)
      release()
      await pressing
    })

    test(`Review never re-opens a pane left waiting undrawn (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      placed = false
      isFocusRefused = true
      const band = await mountBand($, surface)
      await band.press({ key: 'contexer-open' })
      await clock.advance(1_000)
      // A re-ask from a timer is the plugin's own open; only the press may seat the pane.
      expect(opens).toEqual(['contexer-review'])
    })

    test(`a failed open stays silent and brings the band back (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      isOpenRefused = true
      const band = await mountBand($, surface)
      await band.press({ key: 'contexer-open' })
      await clock.settle()
      expect(await band.find({ key: 'contexer-open' })).toBeDefined()
    })

    test(`the command says when the pane is not drawn (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      placed = false
      const ran = await $.command.run({ command: 'contexer-review', args: '' } as never)
      expect(ran?.text).not.toMatch(/Opened/)
      placed = true
      expect((await $.command.run({ command: 'contexer-review', args: '' } as never))?.text).toMatch(/Opened/)
    })

    test(`a pane left waiting undrawn still reads the queue (${surface})`, async ($, on) => {
      // A resize draws the waiting pane later with no call of the mod's; it must not show the
      // last turn's queue then.
      const calls = fakeContexer(on, [NEW])
      await start($, surface)
      placed = false
      const before = calls.length
      await $.command.run({ command: 'contexer-review', args: '' } as never)
      expect(calls.slice(before).some(argv => argv[argv.length - 1] === '--json')).toBe(true)
    })

    test(`counts only items the pane can settle (${surface})`, async ($, on) => {
      fakeContexer(on, [RETIRE])
      await start($, surface)
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeUndefined()
    })

    test(`counts contradictions the pane can settle (${surface})`, async ($, on) => {
      fakeContexer(on, [], 1, [PAIR])
      await start($, surface)
      const band = await mountBand($, surface)
      expect(await band.find({ type: 'Text', text: /1 decision needs your call/ })).toBeDefined()
    })

    test(`draws nothing of its own for an unknown queue protocol (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW], 99)
      await start($, surface)
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeUndefined()
    })
  }
})

describe('keyboard path from the prompt', () => {
  for (const surface of SURFACES) {
    test(`suggests the review command while something can be settled (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      expect(suggested).toContain('/contexer-review')
    })

    test(`suggests nothing for items only the terminal can settle (${surface})`, async ($, on) => {
      fakeContexer(on, [RETIRE])
      await start($, surface)
      expect(suggested).toEqual([])
    })

    test(`replaces Claude Code's own suggestion while decisions wait (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      suggested.length = 0
      await $.prompt.suggest({ text: 'fix lint errors', origin: { kind: 'suggestion' } })
      expect(suggested).toEqual(['/contexer-review'])
    })

    test(`never rewrites another plugin's suggestion (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      suggested.length = 0
      await $.prompt.suggest({ text: 'run the linter', origin: { kind: 'plugin', name: 'other' } })
      expect(suggested).toEqual(['run the linter'])
    })

    test(`a later queue read never replaces another plugin's suggestion (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      suggested.length = 0
      await $.prompt.suggest({ text: 'run the linter', origin: { kind: 'plugin', name: 'other' } })
      await start($, surface)
      expect(suggested).toEqual(['run the linter'])
    })

    test(`offers the review again after the next prompt is sent (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      await $.prompt.suggest({ text: 'run the linter', origin: { kind: 'plugin', name: 'other' } })
      await $.prompt.submit({ text: 'run the linter' } as never)
      suggested.length = 0
      await start($, surface)
      expect(suggested).toEqual(['/contexer-review'])
    })

    test(`offers the review again when the pane closes (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      const pane = await mountPane($, surface)
      suggested.length = 0
      await pane.press({ key: 'close' })
      expect(suggested).toEqual(['/contexer-review'])
    })

    test(`stops suggesting after Later (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      const band = await mountBand($, surface)
      await band.press({ key: 'contexer-later' })
      suggested.length = 0
      await start($, surface)
      await $.prompt.suggest({ text: 'fix lint errors', origin: { kind: 'suggestion' } })
      expect(suggested).toEqual(['fix lint errors'])
    })
  }
})

describe('review pane', () => {
  for (const surface of SURFACES) {
    test(`approves one item with one contexer call (${surface})`, async ($, on) => {
      const calls = fakeContexer(on, [NEW, RETIRE])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: /Never delete rows/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /captured by the assistant/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /● ○ {2}1\/2/ })).toBeDefined()
      expect(await pane.find({ key: `approve-${RETIRE.id}` })).toBeUndefined()

      const before = calls.length
      await pane.press({ key: `approve-${NEW.id}` })
      expect(calls.slice(before)).toHaveLength(1)
      expect(calls[calls.length - 1]?.slice(-2)).toEqual(['approve', NEW.id])
      expect(await pane.find({ type: 'Text', text: /approve done/ })).toBeDefined()
      // The settled card leaves; the terminal-only one is all that is left.
      expect(await pane.find({ type: 'Text', text: /contexer review` in a terminal/ })).toBeDefined()
      expect(await pane.find({ key: 'next' })).toBeUndefined()
    })

    test(`shows one card at a time, settleable ones first (${surface})`, async ($, on) => {
      fakeContexer(on, [RETIRE, NEW, UPDATE])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ key: `approve-${NEW.id}` })).toBeDefined()
      expect(await pane.find({ key: `approve-${UPDATE.id}` })).toBeUndefined()
      await pane.press({ key: 'next' })
      expect(await pane.find({ key: `approve-${UPDATE.id}` })).toBeDefined()
      await pane.press({ key: 'next' })
      expect(await pane.find({ type: 'Text', text: /contexer review` in a terminal/ })).toBeDefined()
      await pane.press({ key: 'next' })
      expect(await pane.find({ key: `approve-${NEW.id}` })).toBeDefined()
      await pane.press({ key: 'prev' })
      expect(await pane.find({ type: 'Text', text: /3\/3/ })).toBeDefined()
    })

    test(`shows when each decision applies (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW, UPDATE])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'deleting organisations' })).toBeDefined()
      await pane.press({ key: 'next' })
      expect(await pane.find({ type: 'Text', text: 'the decision store → the decision store; team sync' })).toBeDefined()
    })

    test(`a conflicting update offers Take update and Keep current (${surface})`, async ($, on) => {
      const calls = fakeContexer(on, [UPDATE])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: / CONFLICT / })).toBeDefined()
      expect((await pane.find({ key: `approve-${UPDATE.id}` }))?.text).toContain('Take update')
      expect(await pane.find({ type: 'Text', text: /You picked the update earlier/ })).toBeDefined()
      await pane.press({ key: `dismiss-${UPDATE.id}` })
      expect(calls[calls.length - 1]?.slice(-2)).toEqual(['dismiss', UPDATE.id])
    })

    test(`an action sends back the basis of the card it was taken on (${surface})`, async ($, on) => {
      const shown = { ...UPDATE, basis: '0123456789abcdef' }
      const fresh = { ...NEW, basis: 'fedcba9876543210' }
      const calls = fakeContexer(on, [fresh, shown])
      await start($, surface)
      const pane = await mountPane($, surface)
      await pane.press({ key: `ignore-${fresh.id}` })
      expect(calls[calls.length - 1]?.slice(-4)).toEqual(['ignore', fresh.id, '--expect', fresh.basis])
      expect((await pane.find({ key: `approve-${shown.id}` }))?.text).toContain('Take update')
      await pane.press({ key: `approve-${shown.id}` })
      expect(calls[calls.length - 1]?.slice(-4)).toEqual(['approve', shown.id, '--expect', shown.basis])
    })

    test(`keeping one side of a contradiction names the other (${surface})`, async ($, on) => {
      const calls = fakeContexer(on, [], 1, [PAIR])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: / CONTRADICTION / })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /These current decisions/ })).toBeDefined()
      expect((await pane.find({ key: 'keep-dddd4444-eeee5555-eeee5555' }))?.text).toContain('Keep Publish bare versions')
      await pane.press({ key: 'keep-dddd4444-eeee5555-eeee5555' })
      expect(calls[calls.length - 1]?.slice(-4)).toEqual(['keep', 'eeee5555', '--over', 'dddd4444'])
      expect(await pane.find({ type: 'Text', text: /All clear/ })).toBeDefined()
    })

    test(`a side the developer did not ratify cannot be kept (${surface})`, async ($, on) => {
      const mixed: CurrentConflict = { ...PAIR, decisions: [side('dddd4444', 'Prefix versions with v'), side('eeee5555', 'Publish bare versions', false)] }
      fakeContexer(on, [], 1, [mixed])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ key: 'keep-dddd4444-eeee5555-dddd4444' })).toBeDefined()
      expect(await pane.find({ key: 'keep-dddd4444-eeee5555-eeee5555' })).toBeUndefined()
      expect(await pane.find({ type: 'Text', text: /cannot replace the other/ })).toBeDefined()
    })

    test(`an item from an older contexer without applicability still draws (${surface})`, async ($, on) => {
      const { applies_when: _dropped, ...older } = NEW
      fakeContexer(on, [older as ReviewItem])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'always' })).toBeDefined()
    })

    test(`one decision in two contradictions keeps against the pair pressed (${surface})`, async ($, on) => {
      const second: CurrentConflict = { ...PAIR, decisions: [side('dddd4444', 'Prefix versions with v'), side('ffff6666', 'Use bare tags')] }
      const calls = fakeContexer(on, [], 1, [PAIR, second])
      await start($, surface)
      const pane = await mountPane($, surface)
      await pane.press({ key: 'next' })
      await pane.press({ key: 'keep-dddd4444-ffff6666-dddd4444' })
      expect(calls[calls.length - 1]?.slice(-4)).toEqual(['keep', 'dddd4444', '--over', 'ffff6666'])
    })

    test(`an inherited proposal applicability shows the current scope (${surface})`, async ($, on) => {
      const inherits: ReviewItem = { ...UPDATE, proposed: { content: 'Use DynamoDB', title: 'DynamoDB', applies_when: null } }
      fakeContexer(on, [inherits])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'the decision store' })).toBeDefined()
    })

    test(`shows a long decision in full (${surface})`, async ($, on) => {
      const long = `${'Read the store directory only through store.store_dir(). '.repeat(20)}The end.`
      fakeContexer(on, [{ ...UPDATE, content: long, proposed: { ...UPDATE.proposed!, content: `${long} Also sidecars.` } }])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: long })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /The end\. Also sidecars\.$/ })).toBeDefined()
    })

    test(`approval names the files it would anchor (${surface})`, async ($, on) => {
      fakeContexer(on, [{ ...NEW, anchors: ['src/orgs/delete.py', 'src/orgs/sync.py'] }])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'ANCHORS' })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: 'src/orgs/delete.py\nsrc/orgs/sync.py' })).toBeDefined()
    })

    test(`a terminal-only card shows what is proposed (${surface})`, async ($, on) => {
      const retire: ReviewItem = { ...RETIRE, retirement: { reason: 'Superseded by DynamoDB', replacement_id: 'cccc3333-x' } }
      const recon: ReviewItem = { ...RETIRE, id: 'bbbb9999', kind: 'reconsideration', reconsideration: { content: 'Use Postgres again' } }
      fakeContexer(on, [retire, recon])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'Superseded by DynamoDB' })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: 'cccc3333' })).toBeDefined()
      await pane.press({ key: 'next' })
      expect(await pane.find({ type: 'Text', text: 'Use Postgres again' })).toBeDefined()
    })

    test(`a contradiction side shows its unreviewed update (${surface})`, async ($, on) => {
      const pending = { ...side('dddd4444', 'Prefix versions with v'), proposed: { content: 'Prefix with v; annotate tags', title: 'x' } }
      fakeContexer(on, [], 1, [{ ...PAIR, decisions: [pending, side('eeee5555', 'Publish bare versions')] }])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'Unreviewed update: Prefix with v; annotate tags' })).toBeDefined()
    })

    test(`reopening the pane drops a half-done edit (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, surface)
      const pane = await mountPane($, surface)
      await pane.press({ key: `edit-${NEW.id}` })
      expect(await pane.find({ key: `cancel-${NEW.id}` })).toBeDefined()
      await pane.press({ key: 'close' })
      await $.command.run({ command: 'contexer-review', args: '' } as never)
      expect(await pane.find({ key: `approve-${NEW.id}` })).toBeDefined()
    })

    test(`a late queue read never brings a settled card back (${surface})`, async ($, on) => {
      let release = () => {}
      const gate: Gate = {}
      fakeContexer(on, [NEW, UPDATE], 1, [], gate)
      await start($, surface)
      gate.held = new Promise<void>(resolve => { release = resolve })
      const opening = $.command.run({ command: 'contexer-review', args: '' } as never)
      const pane = await mountPane($, surface)
      await pane.press({ key: `approve-${NEW.id}` })
      expect(await pane.find({ key: `approve-${NEW.id}` })).toBeUndefined()
      release()
      await opening
      expect(await pane.find({ key: `approve-${NEW.id}` })).toBeUndefined()
      expect(await pane.find({ key: `approve-${UPDATE.id}` })).toBeDefined()
    })

    test(`a queue read started while an action runs never brings its card back (${surface})`, async ($, on) => {
      let release = () => {}
      const gate: Gate = {}
      fakeContexer(on, [NEW, UPDATE], 1, [], gate)
      await start($, surface)
      const pane = await mountPane($, surface)
      gate.action = new Promise<void>(resolve => { release = resolve })
      const pressing = pane.press({ key: `approve-${NEW.id}` })
      // A turn ends mid-press: its read answers first, from before the action's write.
      await $.command.run({ command: 'contexer-review', args: '' } as never)
      release()
      await pressing
      expect(await pane.find({ key: `approve-${NEW.id}` })).toBeUndefined()
      expect(await pane.find({ key: `approve-${UPDATE.id}` })).toBeDefined()
    })

    test(`the pane stays on its card when another session settles an earlier one (${surface})`, async ($, on) => {
      const LATER: ReviewItem = { ...NEW, id: 'gggg7777', title: 'Log every skipped org' }
      fakeContexer(on, [NEW, UPDATE, LATER])
      await start($, surface)
      const pane = await mountPane($, surface)
      await pane.press({ key: 'next' })
      expect(await pane.find({ key: `approve-${UPDATE.id}` })).toBeDefined()
      settleElsewhere(NEW.id)
      await start($, surface)
      expect(await pane.find({ key: `approve-${UPDATE.id}` })).toBeDefined()
      expect(await pane.find({ key: `approve-${LATER.id}` })).toBeUndefined()
    })

    test(`Keep sends back the basis of the pair it was pressed on (${surface})`, async ($, on) => {
      const calls = fakeContexer(on, [], 1, [{ ...PAIR, basis: 'feedc0de12345678' }])
      await start($, surface)
      const pane = await mountPane($, surface)
      await pane.press({ key: 'keep-dddd4444-eeee5555-eeee5555' })
      expect(calls[calls.length - 1]?.slice(-6))
        .toEqual(['keep', 'eeee5555', '--over', 'dddd4444', '--expect', 'feedc0de12345678'])
    })

    test(`the focusable controls sit above the card's text (${surface})`, async ($, on) => {
      // The surface keeps the focused element in view on each redraw; a ring below a long card
      // pulled the window past its title after every action.
      fakeContexer(on, [NEW, UPDATE])
      await start($, surface)
      const pane = await mountPane($, surface)
      const drawn = JSON.stringify(await pane.drawn())
      const title = drawn.indexOf(NEW.title)
      for (const key of [`approve-${NEW.id}`, `ignore-${NEW.id}`, 'next', 'prev']) {
        expect(drawn.indexOf(`"${key}"`)).toBeGreaterThan(-1)
        expect(drawn.indexOf(`"${key}"`)).toBeLessThan(title)
      }
    })

    test(`a contradiction's Keep buttons sit above its sides (${surface})`, async ($, on) => {
      fakeContexer(on, [], 1, [PAIR])
      await start($, surface)
      const pane = await mountPane($, surface)
      const drawn = JSON.stringify(await pane.drawn())
      const pair = 'dddd4444-eeee5555'
      const text = [drawn.indexOf(PAIR.reason), drawn.indexOf(`"side-${pair}-dddd4444"`), drawn.indexOf(`"side-${pair}-eeee5555"`)]
      for (const at of text) expect(at).toBeGreaterThan(-1)
      for (const id of ['dddd4444', 'eeee5555']) {
        const keep = drawn.indexOf(`"keep-${pair}-${id}"`)
        expect(keep).toBeGreaterThan(-1)
        expect(keep).toBeLessThan(Math.min(...text))
      }
    })

    test(`the files approval anchors sit directly above Approve (${surface})`, async ($, on) => {
      // Approve signs them, so they stay in view whenever it holds the focus ring.
      fakeContexer(on, [{ ...NEW, anchors: ['src/orgs/delete.py'] }])
      await start($, surface)
      const pane = await mountPane($, surface)
      const drawn = JSON.stringify(await pane.drawn())
      const anchors = drawn.indexOf('src/orgs/delete.py')
      expect(anchors).toBeGreaterThan(-1)
      expect(anchors).toBeLessThan(drawn.indexOf(`"approve-${NEW.id}"`))
    })

    test(`a card leads with its summary and shows the full text on f (${surface})`, async ($, on) => {
      const long = `${NEW.content} ${'More detail about the deletion path. '.repeat(10)}`
      fakeContexer(on, [{ ...NEW, content: long, summary: 'Do not delete rows for a deleted org. Warn instead.', needs_summary: false }])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'Do not delete rows for a deleted org. Warn instead.' })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /More detail about the deletion path/ })).toBeUndefined()
      await pane.press({ key: 'full' })
      expect(await pane.find({ type: 'Text', text: /More detail about the deletion path/ })).toBeDefined()
      expect((await pane.find({ key: 'full' }))?.text).toContain('Summary')
    })

    test(`a card starts on its summary after moving away or reopening (${surface})`, async ($, on) => {
      const long = `${NEW.content} ${'More detail about the deletion path. '.repeat(10)}`
      const LATER: ReviewItem = { ...NEW, id: 'gggg7777', title: 'Log every skipped org', summary: 'Log it.', needs_summary: false }
      fakeContexer(on, [{ ...NEW, content: long, summary: 'Warn instead.', needs_summary: false }, LATER])
      await start($, surface)
      const pane = await mountPane($, surface)
      const isFullShown = async () => (await pane.find({ type: 'Text', text: /More detail about the deletion path/ })) !== undefined
      await pane.press({ key: 'full' })
      expect(await isFullShown()).toBe(true)
      await pane.press({ key: 'next' })
      await pane.press({ key: 'prev' })
      expect(await isFullShown()).toBe(false)
      await pane.press({ key: 'full' })
      expect(await isFullShown()).toBe(true)
      await $.command.run({ command: 'contexer-review', args: '' } as never)
      expect(await isFullShown()).toBe(false)
    })

    test(`short content with no stored summary gets no toggle (${surface})`, async ($, on) => {
      const reflowed = 'Never delete database rows\nfor an organisation deleted in Clerk'
      fakeContexer(on, [{ ...NEW, content: reflowed, summary: null, needs_summary: false }])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ key: 'full' })).toBeUndefined()
    })

    test(`a stored summary that reads like the content is still a summary (${surface})`, async ($, on) => {
      fakeContexer(on, [{ ...NEW, summary: NEW.content, needs_summary: false }])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ key: 'full' })).toBeDefined()
    })

    test(`an update shows both summaries (${surface})`, async ($, on) => {
      fakeContexer(on, [{
        ...UPDATE, summary: 'Store decisions in Postgres.', needs_summary: false,
        proposed: { ...UPDATE.proposed!, summary: 'Store decisions in DynamoDB.', needs_summary: false },
      }])
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'Store decisions in Postgres.' })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: 'Store decisions in DynamoDB.' })).toBeDefined()
    })

    test(`a long decision with no summary gets one from a small model (${surface})`, async ($, on) => {
      const SCRUBBED = 'Never delete rows for a deleted org. Key [REDACTED:api-key] stays out.'
      const LATER: ReviewItem = { ...NEW, id: 'gggg7777', title: 'Log every skipped org', summary: 'Log it.', needs_summary: false }
      const calls = fakeContexer(on, [{ ...NEW, basis: 'cafe0123cafe0123', summary: null, needs_summary: true, summary_source: SCRUBBED }, LATER])
      const asks: string[] = []
      on('model.complete', async (_$, e) => {
        asks.push(String(e.prompt))
        return { value: { isAnswered: true, text: 'Warn and count. Do not delete rows.', usage: {} } as never }
      })
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: /No summary|Writing a summary/ })).toBeDefined()
      await clock.settle()
      // The scrubbed egress copy, never the verbatim stored content.
      expect(asks).toEqual([SCRUBBED])
      const write = calls.find(argv => argv.includes('summarize'))
      expect(write?.slice(-6)).toEqual(['summarize', NEW.id, '--summary', 'Warn and count. Do not delete rows.', '--expect', 'cafe0123cafe0123'])
      // Once per card text: drawing the card again does not ask again.
      await pane.press({ key: 'next' })
      await pane.press({ key: 'prev' })
      expect(await pane.find({ type: 'Text', text: /No summary|Writing a summary/ })).toBeDefined()
      await clock.settle()
      expect(asks).toHaveLength(1)
    })

    test(`a summary write marked in session state holds off another across a reload (${surface})`, async ($, on) => {
      // A hot reload resets the module's own one-write guard while the write it started is
      // still in flight; the `summarizing` mark in `$.state` survives the reload.
      const calls = fakeContexer(on, [{ ...NEW, basis: 'cafe0123cafe0123', summary: null, needs_summary: true, summary_source: NEW.content }])
      const asks: string[] = []
      on('model.complete', async (_$, e) => {
        asks.push(String(e.prompt))
        return { value: { isAnswered: true, text: 'Warn and count. Do not delete rows.', usage: {} } as never }
      })
      // The mark a write from before the reload left, until something writes the value again.
      let isMarked = true
      on('state.get', { plugin: 'contexer-review', key: 'summarizing' } as never, async (_$, e, next) =>
        (isMarked ? { value: { value: 'card-from-before-the-reload', version: 1 } } : next(e)) as never)
      on('state.set', { plugin: 'contexer-review', key: 'summarizing' } as never, async (_$, e, next) => {
        isMarked = false
        return next(e)
      })
      await start($, surface)
      const pane = await mountPane($, surface)
      await clock.settle()
      expect(asks).toEqual([])
      expect(calls.some(argv => argv.includes('summarize'))).toBe(false)
      // Opening the pane starts unmarked, so a mark the reloaded write never cleared does not
      // stop summaries for good.
      await pane.press({ key: 'close' })
      await $.command.run({ command: 'contexer-review', args: '' } as never)
      expect(await pane.find({ type: 'Text', text: /No summary|Writing a summary/ })).toBeDefined()
      await clock.settle()
      expect(asks).toEqual([NEW.content])
    })

    test(`a failed summary mark does not stop later summary writes (${surface})`, async ($, on) => {
      // The one-write guard is reset in a finally, so a state call that fails before the
      // model is asked cannot hold off every later write until the mod reloads.
      const long = (what: string) => `${what} ${'More detail about the deletion path. '.repeat(10)}`
      const LATER: ReviewItem = { ...NEW, id: 'gggg7777', title: 'Log every skipped org', content: long('Log it.'), basis: 'beef0123beef0123', summary: null, needs_summary: true, summary_source: long('Log it.') }
      const calls = fakeContexer(on, [{ ...NEW, content: long('Warn.'), basis: 'cafe0123cafe0123', summary: null, needs_summary: true, summary_source: long('Warn.') }, LATER])
      const asks: string[] = []
      on('model.complete', async (_$, e) => {
        asks.push(String(e.prompt).split(' ')[0] ?? '')
        return { value: { isAnswered: true, text: 'Warn and count. Do not delete rows.', usage: {} } as never }
      })
      // The first write's mark is refused; every other state call goes through.
      let isFailing = true
      on('state.set', { plugin: 'contexer-review', key: 'summarizing' } as never, async (_$, e, next) => {
        if (isFailing && (e as { value?: unknown }).value != null) {
          isFailing = false
          return { deny: 'state store unavailable' } as never
        }
        return next(e)
      })
      await start($, surface)
      const pane = await mountPane($, surface)
      await clock.settle()
      expect(isFailing).toBe(false)
      expect(asks).toEqual([])
      await pane.press({ key: 'next' })
      await clock.settle()
      expect(asks).toEqual(['Log'])
      expect(calls.some(argv => argv.includes('summarize') && argv.includes(LATER.id))).toBe(true)
    })

    test(`while a summary is written the card hides the long text (${surface})`, async ($, on) => {
      const long = `${NEW.content} ${'More detail about the deletion path. '.repeat(10)}`
      const calls = fakeContexer(on, [{ ...NEW, content: long, basis: 'cafe0123cafe0123', summary: null, needs_summary: true, summary_source: long }])
      let answer: (() => void) | undefined
      on('model.complete', async () => {
        await new Promise<void>(resolve => { answer = resolve })
        return { value: { isAnswered: true, text: 'Warn and count. Do not delete rows.', usage: {} } as never }
      })
      await start($, surface)
      const pane = await mountPane($, surface)
      const settling = clock.settle()
      // Wait until the model is actually being asked, so the check runs mid-write.
      for (let i = 0; i < 100 && !answer; i++) await new Promise<void>(resolve => realTimer(resolve, 5))
      expect(answer).toBeDefined()
      // The placeholder stands where the text goes; the long text is not drawn.
      expect(await pane.find({ type: 'Text', text: 'Writing a summary…' })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /More detail about the deletion path/ })).toBeUndefined()
      // Full text stays one press away for a reviewer who wants it now.
      await pane.press({ key: 'full' })
      expect(await pane.find({ type: 'Text', text: /More detail about the deletion path/ })).toBeDefined()
      answer?.()
      await settling
      // Let the held write finish inside the test: it saves, then redraws.
      for (let i = 0; i < 100 && !calls.some(argv => argv.includes('summarize')); i++) {
        await new Promise<void>(resolve => realTimer(resolve, 5))
      }
      await clock.settle()
      expect(calls.some(argv => argv.includes('summarize'))).toBe(true)
    })

    test(`a failed write shows the full text with the tag at the top (${surface})`, async ($, on) => {
      const long = `${NEW.content} ${'More detail about the deletion path. '.repeat(10)}`
      fakeContexer(on, [{ ...NEW, content: long, basis: 'cafe0123cafe0123', summary: null, needs_summary: true, summary_source: long }])
      on('model.complete', async () => ({ value: { isAnswered: false, reason: 'empty-reply' } as never }))
      await start($, surface)
      const pane = await mountPane($, surface)
      await clock.settle()
      const drawn = JSON.stringify(await pane.drawn())
      const tag = drawn.indexOf('"No summary"')
      expect(tag).toBeGreaterThan(-1)
      expect(tag).toBeLessThan(drawn.indexOf('More detail about the deletion path'))
      expect(drawn).not.toContain('Writing a summary')
    })

    test(`an update's two summaries are written one at a time (${surface})`, async ($, on) => {
      const long = (what: string) => `${what} ${'More detail about the store. '.repeat(12)}`
      const calls = fakeContexer(on, [{
        ...UPDATE, basis: 'beef0123beef0123', content: long('Use Postgres.'), summary: null, needs_summary: true, summary_source: long('Use Postgres.'),
        proposed: { ...UPDATE.proposed!, content: long('Use DynamoDB.'), summary: null, needs_summary: true, summary_source: long('Use DynamoDB.') },
      }])
      const asks: string[] = []
      let inFlight = 0
      let most = 0
      on('model.complete', async (_$, e) => {
        asks.push(String(e.prompt).split(' ')[1] ?? '')
        most = Math.max(most, ++inFlight)
        // A real (unmocked) delay, so a second ask sent beside this one would overlap it.
        await new Promise<void>(resolve => realTimer(resolve, 5))
        inFlight--
        return { value: { isAnswered: true, text: 'Store decisions in one database.', usage: {} } as never }
      })
      await start($, surface)
      await mountPane($, surface)
      await clock.settle()
      expect(most).toBe(1)
      expect(asks).toEqual(['Postgres.', 'DynamoDB.'])
      const writes = calls.filter(argv => argv.includes('summarize'))
      expect(writes.map(argv => argv.includes('--proposal'))).toEqual([false, true])
    })

    test(`only a card with a basis gets a summary written (${surface})`, async ($, on) => {
      const long = `${NEW.content} ${'More detail about the deletion path. '.repeat(10)}`
      const wordy = { ...side('dddd4444', 'Prefix versions with v'), content: long, summary: null, needs_summary: true, summary_source: long }
      const calls = fakeContexer(on, [{ ...NEW, content: long, summary: null, needs_summary: true, summary_source: long }], 1,
        [{ ...PAIR, basis: 'feedc0de12345678', decisions: [wordy, side('eeee5555', 'Publish bare versions')] }])
      const asks: string[] = []
      on('model.complete', async (_$, e) => {
        asks.push(String(e.prompt))
        return { value: { isAnswered: true, text: 'Warn and count.', usage: {} } as never }
      })
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'No summary' })).toBeDefined()
      await pane.press({ key: 'next' })
      expect(await pane.find({ type: 'Text', text: 'No summary' })).toBeDefined()
      await clock.settle()
      expect(asks).toEqual([])
      expect(calls.some(argv => argv.includes('summarize'))).toBe(false)
    })

    test(`a card with no scrubbed copy sends nothing to the summary model (${surface})`, async ($, on) => {
      const long = `${NEW.content} ${'More detail about the deletion path. '.repeat(10)}`
      const calls = fakeContexer(on, [{ ...NEW, content: long, basis: 'cafe0123cafe0123', summary: null, needs_summary: true }])
      const asks: string[] = []
      on('model.complete', async (_$, e) => {
        asks.push(String(e.prompt))
        return { value: { isAnswered: true, text: 'Warn and count.', usage: {} } as never }
      })
      await start($, surface)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'No summary' })).toBeDefined()
      await clock.settle()
      expect(asks).toEqual([])
      expect(calls.some(argv => argv.includes('summarize'))).toBe(false)
    })

    test(`edit sends the developer's wording (${surface})`, async ($, on) => {
      const calls = fakeContexer(on, [NEW])
      await start($, surface)
      const pane = await mountPane($, surface)
      await pane.press({ key: `edit-${NEW.id}` })
      await pane.input({ key: `edit-${NEW.id}`, text: 'Log a warning instead of deleting rows' })
      expect(calls[calls.length - 1]?.slice(-4))
        .toEqual(['edit', NEW.id, '--content', 'Log a warning instead of deleting rows'])
    })

    test(`an edit sends back the basis of the card it was typed on (${surface})`, async ($, on) => {
      const shown = { ...NEW, basis: '0123456789abcdef' }
      const calls = fakeContexer(on, [shown])
      await start($, surface)
      const pane = await mountPane($, surface)
      await pane.press({ key: `edit-${shown.id}` })
      await pane.input({ key: `edit-${shown.id}`, text: 'Log a warning instead of deleting rows' })
      expect(calls[calls.length - 1]?.slice(-6)).toEqual(
        ['edit', shown.id, '--content', 'Log a warning instead of deleting rows', '--expect', shown.basis])
    })
  }
})
