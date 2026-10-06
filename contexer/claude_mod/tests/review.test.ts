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

// A fake `contexer review --json`: lists `items`; an action removes its item and replies with
// the queue as it stands afterwards. Records every argv it was run with.
// What reached the prompt box's suggestion, beneath the plugin.
const suggested: string[] = []
// The mocked clock the timer-driven queue reads run on; made before the test's first `$` call.
let clock: ReturnType<typeof mock.clock>

// `held` holds the next queue read's reply (already snapshotted) until it resolves, to play a
// process that answers late. `action` holds the next action before its write lands, to play a
// store lock another process holds.
type Gate = { held?: Promise<void>; action?: Promise<void> }

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
  on('process.run', async (_$, e) => {
    calls.push([...e.argv])
    const args = e.argv.slice(e.argv.indexOf('--json') + 1)
    if (args.length > 0 && gate?.action) {
      const held = gate.action
      gate.action = undefined
      await held
    }
    if (args.length > 0) pending = pending.filter(item => item.id !== args[1])
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
  on('fs.stat', async () => ({ deny: 'no such file' }))
  on('command.register', async (_$, e) => ({ value: { command: e.name } }))
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  on('ui.close', async () => ({ value: undefined }))
  on('ui.render', async ($, e) => {
    const { Box } = $.ui.resolve(e)
    return h(Box, {}) as RenderElement
  })
  return calls
}

// Starts the session and lets the timer-driven queue read land.
async function start($: Engine, _on?: On): Promise<void> {
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true } as never)
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
      await start($, on)
      const band = await mountBand($, surface)
      expect(await band.find({ type: 'Text', text: /1 decision needs your call/ })).toBeDefined()
      await band.press({ key: 'contexer-later' })
      expect(await band.find({ key: 'contexer-open' })).toBeUndefined()
    })

    test(`steps aside while the pane is open (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, on)
      await $.command.run({ command: 'contexer-review', args: '' } as never)
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeUndefined()
      const pane = await mountPane($, surface)
      await pane.press({ key: 'close' })
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeDefined()
    })

    test(`counts only items the pane can settle (${surface})`, async ($, on) => {
      fakeContexer(on, [RETIRE])
      await start($, on)
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeUndefined()
    })

    test(`counts contradictions the pane can settle (${surface})`, async ($, on) => {
      fakeContexer(on, [], 1, [PAIR])
      await start($, on)
      const band = await mountBand($, surface)
      expect(await band.find({ type: 'Text', text: /1 decision needs your call/ })).toBeDefined()
    })

    test(`draws nothing of its own for an unknown queue protocol (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW], 99)
      await start($, on)
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeUndefined()
    })
  }
})

describe('keyboard path from the prompt', () => {
  for (const surface of SURFACES) {
    test(`suggests the review command while something can be settled (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, on)
      expect(suggested).toContain('/contexer-review')
    })

    test(`suggests nothing for items only the terminal can settle (${surface})`, async ($, on) => {
      fakeContexer(on, [RETIRE])
      await start($, on)
      expect(suggested).toEqual([])
    })

    test(`replaces Claude Code's own suggestion while decisions wait (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, on)
      suggested.length = 0
      await $.prompt.suggest({ text: 'fix lint errors', origin: { kind: 'suggestion' } })
      expect(suggested).toEqual(['/contexer-review'])
    })

    test(`never rewrites another plugin's suggestion (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, on)
      suggested.length = 0
      await $.prompt.suggest({ text: 'run the linter', origin: { kind: 'plugin', name: 'other' } })
      expect(suggested).toEqual(['run the linter'])
    })

    test(`offers the review again when the pane closes (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, on)
      const pane = await mountPane($, surface)
      suggested.length = 0
      await pane.press({ key: 'close' })
      expect(suggested).toEqual(['/contexer-review'])
    })

    test(`stops suggesting after Later (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, on)
      const band = await mountBand($, surface)
      await band.press({ key: 'contexer-later' })
      suggested.length = 0
      await start($)
      await $.prompt.suggest({ text: 'fix lint errors', origin: { kind: 'suggestion' } })
      expect(suggested).toEqual(['fix lint errors'])
    })
  }
})

describe('review pane', () => {
  for (const surface of SURFACES) {
    test(`approves one item with one contexer call (${surface})`, async ($, on) => {
      const calls = fakeContexer(on, [NEW, RETIRE])
      await start($, on)
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
      await start($, on)
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
      await start($, on)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'deleting organisations' })).toBeDefined()
      await pane.press({ key: 'next' })
      expect(await pane.find({ type: 'Text', text: 'the decision store → the decision store; team sync' })).toBeDefined()
    })

    test(`a conflicting update offers Take update and Keep current (${surface})`, async ($, on) => {
      const calls = fakeContexer(on, [UPDATE])
      await start($, on)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: / CONFLICT / })).toBeDefined()
      expect((await pane.find({ key: `approve-${UPDATE.id}` }))?.text).toContain('Take update')
      expect(await pane.find({ type: 'Text', text: /You picked the update earlier/ })).toBeDefined()
      await pane.press({ key: `dismiss-${UPDATE.id}` })
      expect(calls[calls.length - 1]?.slice(-2)).toEqual(['dismiss', UPDATE.id])
    })

    test(`keeping one side of a contradiction names the other (${surface})`, async ($, on) => {
      const calls = fakeContexer(on, [], 1, [PAIR])
      await start($, on)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: / CONTRADICTION / })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /These current decisions/ })).toBeDefined()
      await pane.press({ key: 'keep-dddd4444-eeee5555-eeee5555' })
      expect(calls[calls.length - 1]?.slice(-4)).toEqual(['keep', 'eeee5555', '--over', 'dddd4444'])
      expect(await pane.find({ type: 'Text', text: /All clear/ })).toBeDefined()
    })

    test(`a side the developer did not ratify cannot be kept (${surface})`, async ($, on) => {
      const mixed: CurrentConflict = { ...PAIR, decisions: [side('dddd4444', 'Prefix versions with v'), side('eeee5555', 'Publish bare versions', false)] }
      fakeContexer(on, [], 1, [mixed])
      await start($, on)
      const pane = await mountPane($, surface)
      expect(await pane.find({ key: 'keep-dddd4444-eeee5555-dddd4444' })).toBeDefined()
      expect(await pane.find({ key: 'keep-dddd4444-eeee5555-eeee5555' })).toBeUndefined()
    })

    test(`an item from an older contexer without applicability still draws (${surface})`, async ($, on) => {
      const { applies_when: _dropped, ...older } = NEW
      fakeContexer(on, [older as ReviewItem])
      await start($, on)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'always' })).toBeDefined()
    })

    test(`one decision in two contradictions keeps against the pair pressed (${surface})`, async ($, on) => {
      const second: CurrentConflict = { ...PAIR, decisions: [side('dddd4444', 'Prefix versions with v'), side('ffff6666', 'Use bare tags')] }
      const calls = fakeContexer(on, [], 1, [PAIR, second])
      await start($, on)
      const pane = await mountPane($, surface)
      await pane.press({ key: 'next' })
      await pane.press({ key: 'keep-dddd4444-ffff6666-dddd4444' })
      expect(calls[calls.length - 1]?.slice(-4)).toEqual(['keep', 'dddd4444', '--over', 'ffff6666'])
    })

    test(`an inherited proposal applicability shows the current scope (${surface})`, async ($, on) => {
      const inherits: ReviewItem = { ...UPDATE, proposed: { content: 'Use DynamoDB', title: 'DynamoDB', applies_when: null } }
      fakeContexer(on, [inherits])
      await start($, on)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'the decision store' })).toBeDefined()
    })

    test(`shows a long decision in full (${surface})`, async ($, on) => {
      const long = `${'Read the store directory only through store.store_dir(). '.repeat(20)}The end.`
      fakeContexer(on, [{ ...UPDATE, content: long, proposed: { ...UPDATE.proposed!, content: `${long} Also sidecars.` } }])
      await start($, on)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: long })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /The end\. Also sidecars\.$/ })).toBeDefined()
    })

    test(`approval names the files it would anchor (${surface})`, async ($, on) => {
      fakeContexer(on, [{ ...NEW, anchors: ['src/orgs/delete.py', 'src/orgs/sync.py'] }])
      await start($, on)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'ANCHORS' })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: 'src/orgs/delete.py\nsrc/orgs/sync.py' })).toBeDefined()
    })

    test(`a terminal-only card shows what is proposed (${surface})`, async ($, on) => {
      const retire: ReviewItem = { ...RETIRE, retirement: { reason: 'Superseded by DynamoDB', replacement_id: 'cccc3333-x' } }
      const recon: ReviewItem = { ...RETIRE, id: 'bbbb9999', kind: 'reconsideration', reconsideration: { content: 'Use Postgres again' } }
      fakeContexer(on, [retire, recon])
      await start($, on)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'Superseded by DynamoDB' })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: 'cccc3333' })).toBeDefined()
      await pane.press({ key: 'next' })
      expect(await pane.find({ type: 'Text', text: 'Use Postgres again' })).toBeDefined()
    })

    test(`a contradiction side shows its unreviewed update (${surface})`, async ($, on) => {
      const pending = { ...side('dddd4444', 'Prefix versions with v'), proposed: { content: 'Prefix with v; annotate tags', title: 'x' } }
      fakeContexer(on, [], 1, [{ ...PAIR, decisions: [pending, side('eeee5555', 'Publish bare versions')] }])
      await start($, on)
      const pane = await mountPane($, surface)
      expect(await pane.find({ type: 'Text', text: 'Unreviewed update: Prefix with v; annotate tags' })).toBeDefined()
    })

    test(`reopening the pane drops a half-done edit (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW])
      await start($, on)
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
      await start($, on)
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
      await start($, on)
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

    test(`edit sends the developer's wording (${surface})`, async ($, on) => {
      const calls = fakeContexer(on, [NEW])
      await start($, on)
      const pane = await mountPane($, surface)
      await pane.press({ key: `edit-${NEW.id}` })
      await pane.input({ key: `edit-${NEW.id}`, text: 'Log a warning instead of deleting rows' })
      expect(calls[calls.length - 1]?.slice(-4))
        .toEqual(['edit', NEW.id, '--content', 'Log a warning instead of deleting rows'])
    })
  }
})
