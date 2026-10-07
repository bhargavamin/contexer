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
import type { EngineInterface, Register, RenderChildren } from 'claude-code'

import type { Applicability, ConflictAction, ConflictSide, CurrentConflict, ReviewAction, ReviewItem, ReviewItemKind, ReviewQueue } from '../types'

const PROTOCOL = 1
const PANE = 'contexer-review'
const COMMAND = 'contexer-review'
// The keyboard path from the prompt: the box's dim suggestion, which Tab takes and Enter runs.
const SUGGESTION = `/${COMMAND}`
// Tall enough for most cards whole; a longer one scrolls (arrows while the pane is focused),
// and the person can still resize it. Decisions are shown in full: a review needs all of it.
const PANE_ROWS = 32
const OPEN = { id: PANE, title: 'Contexer review', focus: true, closeOnEscape: true, rows: PANE_ROWS } as const

const queue = atom({ plugin: 'contexer-review', key: 'queue' } as const, null)
const isHidden = atom({ plugin: 'contexer-review', key: 'isHidden' } as const, false)
const isHandingOff = atom({ plugin: 'contexer-review', key: 'isHandingOff' } as const, false)
const editing = atom({ plugin: 'contexer-review', key: 'editing' } as const, null)
const note = atom({ plugin: 'contexer-review', key: 'note' } as const, null)
const cursor = atom({ plugin: 'contexer-review', key: 'cursor' } as const, null)
const isPaneOpen = atom({ plugin: 'contexer-review', key: 'isPaneOpen' } as const, false)
const isOtherSuggested = atom({ plugin: 'contexer-review', key: 'isOtherSuggested' } as const, false)

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

// Queue reads and actions overlap (a turn ends while a button is pressed), and a process can
// answer late. A read takes a ticket when it starts; an action takes one when its write has
// returned, so a read that started while the write was in flight (and may have read the store
// before it) ranks below it. A reply lands only if nothing ranked later has landed already, so
// an old snapshot never brings a settled card back. The counters live in `$.state`, not in
// module variables: a hot reload starts the module over, and a read in flight across it must
// still rank against what landed before.
const order = atom({ plugin: 'contexer-review', key: 'order' } as const, { ticket: 0, landed: 0 })

async function take($: EngineInterface): Promise<number> {
  return (await update($, order, o => ({ ...o, ticket: o.ticket + 1 }))).ticket
}

// The snapshot the ranking last accepted. Set inside the same synchronous updater that takes
// the ranking's decision, and read inside the queue write's own updater, so the write always
// stores the newest accepted snapshot: an older reply that passed its check, then lost the
// race to a newer one's write, rewrites the newer snapshot instead of its own.
let accepted: ReviewQueue | null = null

async function land($: EngineInterface, mine: number, next: ReviewQueue | null): Promise<boolean> {
  let isLatest = false
  await update($, order, o => {
    isLatest = mine >= o.landed
    if (!isLatest) return o
    accepted = next
    return { ...o, landed: mine }
  })
  if (!isLatest) return false
  await update($, queue, () => accepted)
  return true
}

async function refresh($: EngineInterface): Promise<void> {
  const mine = await take($)
  const out = await contexer($, [])
  if (!(await land($, mine, isQueue(out) ? out : null))) return
  await suggestAgain($)
}

// Offer the review as the box's suggestion while decisions wait, unless another plugin's
// suggestion is in the box since the last prompt: ours would replace it.
async function suggestAgain($: EngineInterface): Promise<void> {
  if (await read($, isOtherSuggested)) return
  if (await wantsSuggestion($)) await $.prompt.suggest({ text: SUGGESTION }).catch(() => undefined)
}

// However the pane closed: the band comes back, a half-done edit is dropped, and the empty
// prompt offers the review again.
async function paneClosed($: EngineInterface): Promise<void> {
  await update($, isPaneOpen, () => false)
  await update($, editing, () => null)
  await suggestAgain($)
}

// Our own close does not pass through our own `ui.close` hook, so it settles the pane itself.
async function closePane($: EngineInterface): Promise<void> {
  await $.ui.close({ id: PANE })
  await paneClosed($)
}

// Suggest the review only while the pane has something to settle and the developer has not
// said Later; core itself declines while the box holds text or a turn runs.
async function wantsSuggestion($: EngineInterface): Promise<boolean> {
  return actionable(await read($, queue)) > 0 && !(await read($, isHidden))
}

// One process per click: the action's reply carries the queue as it stands afterwards. `expect`
// is the card's `basis`: the action is refused if the decision no longer reads as shown.
type ActOptions = { content?: string; over?: string; expect?: string }

async function act($: EngineInterface, action: ReviewAction | ConflictAction, id: string, options: ActOptions = {}): Promise<void> {
  const args = [action, id]
  if (options.content) args.push('--content', options.content)
  if (options.over) args.push('--over', options.over)
  if (options.expect) args.push('--expect', options.expect)
  const out = await contexer($, args)
  const mine = await take($)
  const message = typeof out?.message === 'string' ? out.message : 'Contexer did not answer, so nothing changed.'
  await update($, note, () => message)
  await update($, editing, () => null)
  if (isQueue(out?.queue)) await land($, mine, out.queue as ReviewQueue)
  else await refresh($)
  await showCardTop($)
}

// A card is read from its title down, so a new card on screen starts at the top of the pane.
// The item actions and the ‹/› nav are drawn above the card for the same reason: the surface
// keeps the focused element in view on each redraw, and a ring at the bottom would pull a long
// card's window down past its title. A contradiction card's Keep buttons, one per keepable side,
// sit in that same row above the card.
async function showCardTop($: EngineInterface): Promise<void> {
  await $.ui.scroll({ in: PANE, to: 'start' }).catch(() => undefined)
}

// Opens at once on the queue the last turn left, then redraws if it changed since. Called from
// the person's own command or press, so the surface seats it at any width; an open made later
// from a timer counts as the plugin's own and waits undrawn on a narrow terminal. The pane only
// counts as open (hiding the band) once it is drawn. `onSeated` runs once it is drawn, before
// the queue read (a Python process) is waited on. Says whether the pane is drawn.
async function openPane($: EngineInterface, onSeated?: () => void): Promise<boolean> {
  await update($, note, () => null)
  await update($, editing, () => null)
  await update($, cursor, () => null)
  const opened = await $.ui.open(OPEN)
  await update($, isPaneOpen, () => opened.isPlaced)
  if (!opened.isPlaced) return false
  onSeated?.()
  await refresh($)
  return true
}

// Whether the surface draws the pane now, by the engine's record: a pane left waiting undrawn
// is seated later when the terminal widens, with no call of ours to see it. A failed read
// counts as no pane drawn, so the band stays.
async function isPaneDrawn($: EngineInterface): Promise<boolean> {
  try {
    return (await $.ui.panes()).some(pane => pane.id === PANE && pane.isPlaced)
  } catch {
    return false
  }
}

// The engine refuses a pane's focus request while the band holds the keys, so a pane opened by
// the band's own button would open without the keyboard. The press opens the pane itself (so it
// counts as asked); the band steps aside (drawing nothing hands the keys back to the prompt),
// and a timer re-asks for focus until the surface reports it focused, bounded, instead of
// guessing how long the band takes to redraw. The re-asks start as soon as the pane is seated,
// not after the queue read, so keys typed meanwhile reach the pane, and only for a seated pane:
// a re-ask from a timer is the plugin's own open, which must never seat a pane by itself.
const HANDOFF_TRIES = 20
const HANDOFF_STEP_MS = 25

async function focusPane($: EngineInterface): Promise<void> {
  try {
    for (let i = 0; i < HANDOFF_TRIES; i++) {
      const pane = (await $.ui.panes()).find(open => open.id === PANE)
      if (!pane || pane.isFocused) return
      await $.clock.sleep(HANDOFF_STEP_MS)
      await $.ui.open(OPEN)
    }
  } finally {
    await update($, isHandingOff, () => false)
  }
}

// The handoff ends in `focusPane` once the pane is seated, else here: the band comes back for a
// pane left waiting undrawn or an open that failed. A failure stays silent.
async function openPaneFromBand($: EngineInterface): Promise<void> {
  let isSeated = false
  try {
    await update($, isHandingOff, () => true)
    await openPane($, () => {
      isSeated = true
      $.clock.after(0, () => {
        focusPane($).catch(() => undefined)
      })
    })
  } catch {
    // fail silent, as the whole mod does
  }
  if (!isSeated) await update($, isHandingOff, () => false).catch(() => undefined)
}

// A queue read is a Python process (~150ms). Never make the session start or a turn's end wait
// for it: a timer runs it after the dispatch, and the band appears when it lands.
function refreshSoon($: EngineInterface): void {
  $.clock.after(0, () => void refresh($))
}

// Each card wears one badge naming what is asked, in the colour of its frame.
type Badge = { label: string; color: string }

const KIND_BADGE: Record<ReviewItemKind, Badge> = {
  new: { label: 'NEW', color: 'green' },
  update: { label: 'UPDATE', color: 'cyan' },
  retirement: { label: 'RETIRE?', color: 'gray' },
  reconsideration: { label: 'RECONSIDER?', color: 'gray' },
}
const CONFLICT_BADGE: Badge = { label: 'CONFLICT', color: 'yellow' }
const CONTRADICTION_BADGE: Badge = { label: 'CONTRADICTION', color: 'red' }

const ACTION_LABEL: Record<ReviewAction, string> = {
  approve: 'Approve',
  edit: 'Edit',
  ignore: 'Ignore',
  dismiss: 'Dismiss update',
}

const ACTION_HOTKEY: Record<ReviewAction, string> = { approve: 'a', edit: 'e', ignore: 'i', dismiss: 'd' }

// Whole text, trailing space trimmed per line; paragraph breaks kept.
function full(text: string): string {
  return text.split('\n').map(line => line.trimEnd()).join('\n').trim()
}

// A button names the side it keeps; a long title is cut to keep both buttons on one line.
const KEEP_TITLE_CHARS = 32

function clip(text: string, max: number): string {
  const line = text.replace(/\s+/g, ' ').trim()
  return line.length <= max ? line : `${line.slice(0, max - 1).trimEnd()}…`
}

function applicability(when: Applicability | undefined): string {
  return when?.length ? when.join('; ') : 'always'
}

// An earlier pick, worded for the pane's own buttons rather than the terminal's.
const PICK_LINE: Record<'update' | 'standing', string> = {
  update: 'You picked the update earlier.',
  standing: 'You kept the current version earlier.',
}

// A Suggested Update whose wording differs from the approved one (conflicts.has_open_conflict).
function isOpenConflict(item: ReviewItem): boolean {
  return item.kind === 'update' && !!item.conflict
}

// A conflicting update is the developer choosing between two versions, so its buttons say so.
function actionLabel(item: ReviewItem, action: ReviewAction): string {
  if (isOpenConflict(item) && action === 'approve') return 'Take update'
  if (isOpenConflict(item) && action === 'dismiss') return 'Keep current'
  return ACTION_LABEL[action]
}

// What the pane can settle: pending items with buttons, plus contradictions with a keepable side.
// The band counts this, so it always matches the cards that have buttons.
function actionable(q: ReviewQueue | null): number {
  if (!q) return 0
  return q.items.filter(item => item.actions.length > 0).length
    + (q.conflicts ?? []).filter(pair => pair.actions.length > 0).length
}

// The pane shows one card at a time. Cards the pane can settle come first, so the first card
// always has buttons; the ones only the terminal can settle wait at the end.
type Card = { kind: 'item'; item: ReviewItem } | { kind: 'pair'; pair: CurrentConflict }

function deck(q: ReviewQueue): Card[] {
  const items: Card[] = q.items.map(item => ({ kind: 'item', item }))
  const pairs: Card[] = (q.conflicts ?? []).map(pair => ({ kind: 'pair', pair }))
  const settles = (card: Card) => (card.kind === 'item' ? card.item.actions : card.pair.actions).length > 0
  const all = [...items, ...pairs]
  return [...all.filter(settles), ...all.filter(card => !settles(card))]
}

// The card the pane shows, by key: a refresh can shift the deck under it (another session
// settles an earlier card), and the hotkeys must stay on the decision the developer is reading.
// Gone (settled), it falls back to the same position, the next card in line.
function cardAt(cards: Card[], want: { key: string; at: number } | null): number {
  const found = want ? cards.findIndex(card => cardKey(card) === want.key) : -1
  return found >= 0 ? found : Math.min(want?.at ?? 0, cards.length - 1)
}

function cardKey(card: Card): string {
  return card.kind === 'item' ? card.item.id : pairKey(card.pair)
}

function pairKey(pair: CurrentConflict): string {
  return `${pair.decisions[0].id}-${pair.decisions[1].id}`
}

// Dots while they fit on one line, then a plain count.
function progress(at: number, total: number): string {
  const count = `${at + 1}/${total}`
  if (total > 12) return count
  return `${Array.from({ length: total }, (_, i) => (i === at ? '●' : '○')).join(' ')}  ${count}`
}

function meta(item: ReviewItem): string {
  const date = item.timestamp ? item.timestamp.slice(0, 10) : ''
  return [item.subtype || 'decision', item.origin, date].filter(Boolean).join(' · ')
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

  // Claude Code proposes its own next prompt after a turn, which would replace ours. While
  // decisions wait (and not after Later), its proposal becomes the review command instead.
  // Another plugin's suggestion is noted, so a queue read landing later does not replace it.
  on('prompt.suggest', async ($, e, next) => {
    if (e.origin.kind === 'suggestion' && (await wantsSuggestion($))) return next({ ...e, text: SUGGESTION })
    const shown = await next(e)
    if (e.origin.kind === 'plugin' && shown.isShown) await update($, isOtherSuggested, () => true)
    return shown
  })

  // Sending a prompt clears the box, and with it any other plugin's suggestion.
  on('prompt.submit', async ($, e, next) => {
    await update($, isOtherSuggested, () => false)
    return next(e)
  })

  // The person's close (Esc, the pane's own close mark): offer the review again.
  on('ui.close', { id: PANE }, async ($, e, next) => {
    const closed = await next(e)
    await paneClosed($)
    return closed
  })

  on('command.run', { command: COMMAND }, async $ => {
    const isPlaced = await openPane($)
    return { text: isPlaced ? 'Opened the Contexer review pane.' : 'The Contexer review pane is open but not drawn here yet.' }
  })

  // The band counts only what the pane can settle, and steps aside while the pane is drawn (it
  // stays for a pane the surface leaves waiting undrawn).
  // Retirements and reconsiderations still show in the pane, but alone they would point the
  // developer at a pane with no buttons in it.
  // `isPaneOpen` (state, so writing it redraws the band) covers an open that drew at once; the
  // engine's own record covers a pane the surface drew later, when the terminal widened (a
  // resize redraws the band too).
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const q = await read($, queue)
    const n = actionable(q)
    if (e.props.hasSurvey || n === 0 || (await read($, isHidden)) || (await read($, isHandingOff))
      || (await read($, isPaneOpen)) || (await isPaneDrawn($))) {
      return next(e)
    }

    // The band gets the keyboard from the prompt by Claude Code's `abovePrompt:focus` (ctrl+x tab
    // by default); there Tab/arrows move, Enter presses, Esc leaves. The ring starts on Review,
    // and r/l press the buttons while the band holds the keys. No digit hotkeys: a bare digit in
    // an empty prompt presses a band button, and this band stays up while decisions wait.
    const { Box, Text, Button } = $.ui.resolve(e)
    return (
      <Box key="contexer-band" gap={1}>
        <Text>{`Contexer · ${n} decision${n === 1 ? ' needs' : 's need'} your call`}</Text>
        <Button key="contexer-open" label="Review" variant="primary" hotkey="r" autoFocus onPress={() => openPaneFromBand($)} />
        <Button key="contexer-later" label="Later" hotkey="l" dimColor onPress={() => update($, isHidden, () => true)} />
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
    const close = <Button key="close" label="Close" role="dismiss" dimColor onPress={() => closePane($)} />

    if (!q) {
      return (
        <Box flexDirection="column" gap={1} paddingX={1}>
          <Text color="yellow">{"Contexer's review queue could not be read here."}</Text>
          <Text dimColor>Run `contexer review` in a terminal.</Text>
          {close}
        </Box>
      )
    }
    const cards = deck(q)
    if (cards.length === 0) {
      return (
        <Box flexDirection="column" gap={1} paddingX={1}>
          {message ? <Text dimColor>{message}</Text> : null}
          <Text bold color="green">✓ All clear</Text>
          <Text dimColor>Nothing waits on you. Every decision is reviewed.</Text>
          {close}
        </Box>
      )
    }

    const at = cardAt(cards, await read($, cursor))
    const card = cards[at] as Card
    const key = cardKey(card)

    const badge = ({ label, color }: Badge) => (
      <Text key={`badge-${label}`} bold color="black" backgroundColor={color}>{` ${label} `}</Text>
    )
    // A label column keeps the values aligned: NOW, PROPOSED, APPLIES.
    const field = (name: string, value: string, dim = false) => (
      <Box key={`${name}-${key}`} flexDirection="row">
        <Box width={10} flexShrink={0}><Text dimColor>{name}</Text></Box>
        <Box flexShrink={1}><Text dimColor={dim}>{value}</Text></Box>
      </Box>
    )

    let frame: string
    let badges: Badge[]
    let body: RenderChildren[]
    let actions: RenderChildren
    let anchors: RenderChildren = null
    if (card.kind === 'item') {
      const { item } = card
      const isConflict = isOpenConflict(item)
      frame = isConflict ? CONFLICT_BADGE.color : KIND_BADGE[item.kind].color
      badges = isConflict ? [CONFLICT_BADGE, KIND_BADGE[item.kind]] : [KIND_BADGE[item.kind]]
      body = [<Text key={`title-${key}`} bold>{item.title}</Text>]
      if (item.kind === 'update' && item.proposed) {
        const before = applicability(item.applies_when)
        const after = applicability(item.proposed.applies_when ?? item.applies_when)
        body.push(
          <Box key={`fields-${key}`} flexDirection="column">
            {field('NOW', full(item.content), true)}
            {field('PROPOSED', full(item.proposed.content))}
            {field('APPLIES', before === after ? before : `${before} → ${after}`, true)}
          </Box>,
        )
        if (item.pick) body.push(<Text key={`pick-${key}`} color="yellow">{PICK_LINE[item.pick]}</Text>)
      } else {
        body.push(
          <Box key={`fields-${key}`} flexDirection="column">
            <Text>{full(item.content)}</Text>
            {field('APPLIES', applicability(item.applies_when), true)}
          </Box>,
        )
      }
      // What is being asked, for the cards only the terminal settles: the proposal itself.
      if (item.retirement) {
        body.push(
          <Box key={`proposal-${key}`} flexDirection="column">
            {field('RETIRE', full(item.retirement.reason) || 'no reason given')}
            {item.retirement.replacement_id ? field('REPLACED', item.retirement.replacement_id.slice(0, 8), true) : null}
          </Box>,
        )
      }
      if (item.reconsideration) {
        body.push(<Box key={`proposal-${key}`} flexDirection="column">{field('RESTATED', full(item.reconsideration.content))}</Box>)
      }
      // Approving signs these anchors, so they sit outside the card, directly above the button
      // that signs them: in view whenever it is focused, however long the card.
      if (item.actions.includes('approve') && item.anchors?.length) {
        anchors = <Box key={`anchors-${key}`} flexDirection="column">{field('ANCHORS', item.anchors.join('\n'))}</Box>
      }

      if (editingId === item.id && Input) {
        actions = (
          <Box key={`editing-${key}`} flexDirection="column">
            <Input
              key={`edit-${item.id}`}
              label="New wording: "
              value={item.proposed?.content ?? item.content}
              submitLabel="approve"
              autoFocus
              onSubmit={value => (value.trim()
                ? act($, 'edit', item.id, { content: value.trim(), expect: item.basis })
                : update($, note, () => 'Type the new wording, then press Enter.'))}
            />
            <Button key={`cancel-${item.id}`} label="Cancel" dimColor onPress={() => update($, editing, () => null)} />
          </Box>
        )
      } else if (item.actions.length === 0) {
        actions = <Text key={`terminal-${key}`} dimColor>Decide this one with `contexer review` in a terminal.</Text>
      } else {
        actions = (
          <Box key={`actions-${key}`} gap={1}>
            {item.actions
              .filter(action => action !== 'edit' || Input)
              .map(action => (
                <Button
                  key={`${action}-${item.id}`}
                  label={actionLabel(item, action)}
                  variant={action === 'approve' ? 'primary' : undefined}
                  hotkey={ACTION_HOTKEY[action]}
                  onPress={() => (action === 'edit' ? update($, editing, () => item.id) : act($, action, item.id, { expect: item.basis }))}
                />
              ))}
          </Box>
        )
      }
    } else {
      // Keep one side; the other is retired as superseded by it, with the reason recorded. Only a
      // human-ratified side (one the developer stated or approved) can be kept, so an unratified
      // capture never replaces a decision the developer ratified.
      const { pair } = card
      const [left, right] = pair.decisions
      const sides: [ConflictSide, ConflictSide][] = [[left, right], [right, left]]
      frame = CONTRADICTION_BADGE.color
      badges = [CONTRADICTION_BADGE]
      body = [
        <Text key={`reason-${key}`} dimColor>{pair.reason}</Text>,
        <Box key={`sides-${key}`} flexDirection="row" gap={1}>
          {sides.map(([one]) => (
            <Box key={`side-${key}-${one.id}`} width="50%" flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1}>
              <Text bold>{one.title}</Text>
              <Text>{full(one.content)}</Text>
              <Text dimColor>{`${one.status} · applies: ${applicability(one.applies_when)}`}</Text>
              {one.proposed
                ? <Text color="yellow">{`Unreviewed update: ${full(one.proposed.content)}`}</Text>
                : null}
              {one.can_keep
                ? null
                : <Text key={`unkeepable-${key}-${one.id}`} dimColor>Not approved by you, so it cannot replace the other.</Text>}
            </Box>
          ))}
        </Box>,
      ]
      // One Keep per keepable side, named by its title, in the action row above the card like
      // every other card's buttons: a ring inside a side, below its content, pulled the card's
      // window past its reason line. The sides themselves are text only.
      actions = pair.actions.length === 0
        ? <Text key={`terminal-${key}`} dimColor>{'Neither side is one you approved. Retire one with `contexer retire <id> --reason <why>`, or edit one so they agree.'}</Text>
        : (
          <Box key={`actions-${key}`} gap={1} flexWrap="wrap">
            {sides.filter(([one]) => one.can_keep).map(([one, other]) => (
              // Keyed by pair too: one decision can contradict two others, and presses are
              // resolved by key, so a side-only key would let one press run another pair's Keep.
              <Button
                key={`keep-${key}-${one.id}`}
                label={`Keep ${clip(one.title, KEEP_TITLE_CHARS)}`}
                variant="primary"
                onPress={() => act($, 'keep', one.id, { over: other.id, expect: pair.basis })}
              />
            ))}
          </Box>
        )
    }

    const subtitle = card.kind === 'item' ? meta(card.item) : 'two current decisions disagree'
    const many = cards.length > 1
    const go = async (to: number) => {
      await update($, cursor, () => ({ key: cardKey(cards[to] as Card), at: to }))
      await showCardTop($)
    }
    return (
      <Box flexDirection="column" gap={1} paddingX={1}>
        <Box key="top" flexDirection="row" justifyContent="space-between">
          <Box gap={1}>
            {badges.map(badge)}
            <Text dimColor>{subtitle}</Text>
          </Box>
          <Box gap={1}>
            {many ? <Button key="prev" label="‹" hotkey="p" dimColor onPress={() => go((at + cards.length - 1) % cards.length)} /> : null}
            <Text dimColor>{progress(at, cards.length)}</Text>
            {many ? <Button key="next" label="›" hotkey="n" dimColor onPress={() => go((at + 1) % cards.length)} /> : null}
          </Box>
        </Box>
        {anchors}
        {actions}
        {message ? <Text key="note" color="green">{`› ${message}`}</Text> : null}
        <Box key={`card-${key}`} flexDirection="column" borderStyle="round" borderColor={frame} paddingX={1} gap={1}>
          {body}
        </Box>
        <Box key="bottom" flexDirection="row" justifyContent="space-between">
          <Text dimColor>{many ? 'tab move · enter press · ↑↓ scroll · n/p next/previous · esc close' : 'tab move · enter press · ↑↓ scroll · esc close'}</Text>
          {close}
        </Box>
      </Box>
    )
  })
}
