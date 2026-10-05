// Behaviour of the review mod against the engine, with `contexer review --json` played by a
// test hook beneath the plugin (`process.run`). Run with `claude plugin test contexer/claude_mod`.
import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, RenderElement } from 'claude-code'

import type { ReviewItem } from '../types'

const SURFACES = ['terminal', 'desktop'] as const
type Surface = (typeof SURFACES)[number]

const NEW: ReviewItem = {
  id: 'aaaa1111', kind: 'new', title: 'Never delete rows for a deleted Clerk org',
  content: 'Never delete database rows for an organisation deleted in Clerk; warn and count instead',
  subtype: 'constraint', status: 'pending_approval', created_by: 'ai',
  origin: 'captured by the assistant', timestamp: '2026-10-01T10:00:00Z',
  actions: ['approve', 'edit', 'ignore'],
}
const RETIRE: ReviewItem = {
  id: 'bbbb2222', kind: 'retirement', title: 'Use Postgres for the store', content: 'Use Postgres',
  subtype: 'architecture', status: 'approved', created_by: 'human', origin: 'your prompt',
  timestamp: '2026-09-01T10:00:00Z', actions: [],
}

// A fake `contexer review --json`: lists `items`; an action removes its item and replies with
// the queue as it stands afterwards. Records every argv it was run with.
function fakeContexer(on: On, items: ReviewItem[], protocol = 1) {
  const calls: string[][] = []
  let pending = [...items]
  const queue = () => ({ protocol, repo: '/repo', count: pending.length, items: pending })
  on('process.run', async (_$, e) => {
    calls.push([...e.argv])
    const args = e.argv.slice(e.argv.indexOf('--json') + 1)
    if (args.length > 0) pending = pending.filter(item => item.id !== args[1])
    const out = args.length === 0 ? queue() : { ok: true, message: `${args[0]} done`, queue: queue() }
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
async function start($: Engine, on: On): Promise<void> {
  const clock = mock.clock(on)
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

    test(`counts only items the pane can settle (${surface})`, async ($, on) => {
      fakeContexer(on, [RETIRE])
      await start($, on)
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeUndefined()
    })

    test(`draws nothing of its own for an unknown queue protocol (${surface})`, async ($, on) => {
      fakeContexer(on, [NEW], 99)
      await start($, on)
      expect(await (await mountBand($, surface)).find({ key: 'contexer-open' })).toBeUndefined()
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
      expect(await pane.find({ type: 'Text', text: /contexer review` in a terminal/ })).toBeDefined()
      expect(await pane.find({ key: `approve-${RETIRE.id}` })).toBeUndefined()

      const before = calls.length
      await pane.press({ key: `approve-${NEW.id}` })
      expect(calls.slice(before)).toHaveLength(1)
      expect(calls[calls.length - 1]?.slice(-2)).toEqual(['approve', NEW.id])
      expect(await pane.find({ type: 'Text', text: /approve done/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /1 decision needs your call/ })).toBeDefined()
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
