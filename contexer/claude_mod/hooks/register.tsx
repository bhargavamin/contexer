// Contexer's in-session review: a band above the prompt while decisions wait on the developer,
// and a pane to settle them one at a time without leaving Claude Code.
//
// The mod holds no review logic. It reads the queue from `contexer review --json` and settles
// one item per click through `contexer review --json <action> <id>`, the same store calls the
// terminal `contexer review` makes. A click is the developer's own act, so the human-approval
// rule holds: nothing here approves on Claude's behalf, and Claude never sees these buttons.
// Retirements and reconsiderations are listed but left to `contexer review`, which asks for
// the reason or wording they need.
//
// It fails silent: no `contexer` answering, or a queue shape this mod does not know
// (`protocol` mismatch after an upgrade mid-session), means no band and no pane content.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ReviewAction, ReviewItem, ReviewItemKind, ReviewQueue } from '../types'

const PROTOCOL = 1
const PANE = 'contexer-review'
const COMMAND = 'contexer-review'
const PREVIEW_CHARS = 480

const queue = atom({ plugin: 'contexer-review', key: 'queue' } as const, null)
const isHidden = atom({ plugin: 'contexer-review', key: 'isHidden' } as const, false)
const editing = atom({ plugin: 'contexer-review', key: 'editing' } as const, null)
const note = atom({ plugin: 'contexer-review', key: 'note' } as const, null)

// The `contexer` that installed this mod: the console script of the same tool venv
// (<venv>/lib/pythonX.Y/site-packages/contexer/claude_mod -> <venv>/bin/contexer), so another
// contexer earlier on PATH never answers for this package. Any other layout (a source
// checkout) falls back to PATH, and the protocol check catches a mismatch.
const VENV_LAYOUT = /^(.*)[\\/]lib[\\/]python[^\\/]+[\\/]site-packages[\\/]contexer[\\/]claude_mod[\\/]?$/

let resolvedBinary: Promise<string> | undefined

function binary($: EngineInterface): Promise<string> {
  resolvedBinary ??= resolveBinary($)
  return resolvedBinary
}

async function resolveBinary($: EngineInterface): Promise<string> {
  const match = VENV_LAYOUT.exec($.plugin.root)
  if (match) {
    const candidate = `${match[1]}/bin/contexer`
    try {
      if ((await $.fs.stat(candidate)).kind === 'file') return candidate
    } catch {
      // not there: fall through to PATH
    }
  }
  return 'contexer'
}

async function contexer($: EngineInterface, args: string[]): Promise<Record<string, unknown> | undefined> {
  try {
    const { stdout } = await $.process.run([await binary($), 'review', '--json', ...args], {
      cwd: await $.session.cwd(),
      timeoutMs: 15_000,
    })
    const parsed: unknown = JSON.parse(stdout)
    return parsed !== null && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

function isQueue(value: unknown): value is ReviewQueue {
  const v = value as ReviewQueue | undefined
  return !!v && v.protocol === PROTOCOL && Array.isArray(v.items) && typeof v.count === 'number'
}

async function refresh($: EngineInterface): Promise<void> {
  const out = await contexer($, [])
  await update($, queue, () => (isQueue(out) ? out : null))
}

// One process per click: the action's reply carries the queue as it stands afterwards.
async function act($: EngineInterface, action: ReviewAction, id: string, content?: string): Promise<void> {
  const args = content ? [action, id, '--content', content] : [action, id]
  const out = await contexer($, args)
  const message = typeof out?.message === 'string' ? out.message : 'Contexer did not answer, so nothing changed.'
  await update($, note, () => message)
  await update($, editing, () => null)
  if (isQueue(out?.queue)) await update($, queue, () => out.queue as ReviewQueue)
  else await refresh($)
}

// Opens at once on the queue the last turn left, then redraws if it changed since.
async function openPane($: EngineInterface): Promise<void> {
  await update($, note, () => null)
  await $.ui.open({ id: PANE, title: 'Contexer review', focus: true, closeOnEscape: true })
  await refresh($)
}

// A queue read is a Python process (~150ms). Never make the session start or a turn's end wait
// for it: a timer runs it after the dispatch, and the band appears when it lands.
function refreshSoon($: EngineInterface): void {
  $.clock.after(0, () => void refresh($))
}

const KIND_LABEL: Record<ReviewItemKind, string> = {
  new: 'new',
  update: 'suggested update',
  retirement: 'retirement proposed',
  reconsideration: 'reconsideration proposed',
}

const ACTION_LABEL: Record<ReviewAction, string> = {
  approve: 'Approve',
  edit: 'Edit',
  ignore: 'Ignore',
  dismiss: 'Dismiss update',
}

const FIRST_ITEM_HOTKEY: Record<ReviewAction, string> = { approve: 'a', edit: 'e', ignore: 'i', dismiss: 'd' }

function preview(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > PREVIEW_CHARS ? `${flat.slice(0, PREVIEW_CHARS - 1)}…` : flat
}

function meta(item: ReviewItem): string {
  const date = item.timestamp ? item.timestamp.slice(0, 10) : ''
  return [item.subtype || 'decision', KIND_LABEL[item.kind], item.origin, date].filter(Boolean).join(' · ')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: COMMAND,
      description: 'Review pending Contexer decisions in a pane',
    })
    const started = await next(e)
    refreshSoon($)
    return started
  })

  // Decisions are captured during turns (MCP calls, prompt directives), so the count is
  // re-read when the main loop's turn ends. A subagent's turn ending changes nothing here.
  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId === undefined) refreshSoon($)
    return done
  })

  on('command.run', { command: COMMAND }, async $ => {
    await openPane($)
    return { text: 'Opened the Contexer review pane.' }
  })

  // The band counts only what the pane can settle. Retirements and reconsiderations still show
  // in the pane, but alone they would point the developer at a pane with no buttons in it.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const q = await read($, queue)
    const n = q ? q.items.filter(item => item.actions.length > 0).length : 0
    if (e.props.hasSurvey || n === 0 || (await read($, isHidden))) return next(e)

    const { Box, Text, Button } = $.ui.resolve(e)
    return (
      <Box key="contexer-band" gap={1}>
        <Text>{`Contexer · ${n} decision${n === 1 ? ' needs' : 's need'} your call`}</Text>
        <Button key="contexer-open" label="Review" variant="primary" onPress={() => openPane($)} />
        <Button key="contexer-later" label="Later" dimColor onPress={() => update($, isHidden, () => true)} />
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    const Input = 'Input' in els ? els.Input : undefined
    const q = await read($, queue)
    const message = await read($, note)
    const editingId = await read($, editing)
    const close = <Button key="close" label="Close" role="dismiss" onPress={() => $.ui.close({ id: PANE })} />

    if (!q) {
      return (
        <Box flexDirection="column" gap={1}>
          <Text dimColor>Contexer's review queue could not be read here. Run `contexer review` in a terminal.</Text>
          {close}
        </Box>
      )
    }
    if (q.count === 0) {
      return (
        <Box flexDirection="column" gap={1}>
          {message ? <Text dimColor>{message}</Text> : <Text dimColor>{' '}</Text>}
          <Text>Nothing waits on you. Every decision is reviewed.</Text>
          {close}
        </Box>
      )
    }

    const cards = q.items.map((item, index) => {
      const lines = [
        <Text key={`title-${item.id}`} bold>{`${index + 1}. ${item.title}`}</Text>,
        <Text key={`meta-${item.id}`} dimColor>{meta(item)}</Text>,
      ]
      if (item.kind === 'update' && item.proposed) {
        lines.push(<Text key={`now-${item.id}`}>{`Now: ${preview(item.content)}`}</Text>)
        lines.push(<Text key={`new-${item.id}`}>{`Proposed: ${preview(item.proposed.content)}`}</Text>)
      } else {
        lines.push(<Text key={`body-${item.id}`}>{preview(item.content)}</Text>)
      }

      if (editingId === item.id && Input) {
        lines.push(
          <Input
            key={`edit-${item.id}`}
            label="New wording: "
            value={item.proposed?.content ?? item.content}
            submitLabel="approve"
            autoFocus
            onSubmit={value => (value.trim()
              ? act($, 'edit', item.id, value.trim())
              : update($, note, () => 'Type the new wording, then press Enter.'))}
          />,
        )
        lines.push(<Button key={`cancel-${item.id}`} label="Cancel" dimColor onPress={() => update($, editing, () => null)} />)
      } else if (item.actions.length === 0) {
        lines.push(<Text key={`terminal-${item.id}`} dimColor>Run `contexer review` in a terminal to decide this one.</Text>)
      } else {
        const buttons = item.actions
          .filter(action => action !== 'edit' || Input)
          .map(action => (
            <Button
              key={`${action}-${item.id}`}
              label={ACTION_LABEL[action]}
              variant={action === 'approve' ? 'primary' : undefined}
              hotkey={index === 0 ? FIRST_ITEM_HOTKEY[action] : undefined}
              onPress={() => (action === 'edit' ? update($, editing, () => item.id) : act($, action, item.id))}
            />
          ))
        lines.push(<Box key={`actions-${item.id}`} gap={1}>{buttons}</Box>)
      }
      return <Box key={`item-${item.id}`} flexDirection="column">{lines}</Box>
    })

    return (
      <Box flexDirection="column" gap={1}>
        <Text bold>{`${q.count} decision${q.count === 1 ? ' needs' : 's need'} your call`}</Text>
        {message ? <Text dimColor>{message}</Text> : <Text dimColor>Settle each one here. Nothing changes until you press a button.</Text>}
        {cards}
        {close}
      </Box>
    )
  })
}
