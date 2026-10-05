// The values the review mod keeps in `$.state` for the session, and the queue shape it reads
// from `contexer review --json` (console_api.review_queue, protocol 1).

export type ReviewItemKind = 'new' | 'update' | 'retirement' | 'reconsideration'

export type ReviewAction = 'approve' | 'edit' | 'ignore' | 'dismiss'

/** Settles a pair of contradicting current decisions (review.item_actions('current_conflict')). */
export type ConflictAction = 'keep'

/** When a decision applies (`applies_when`); empty means always. */
export type Applicability = string[]

export type ReviewItem = {
  id: string
  kind: ReviewItemKind
  title: string
  content: string
  subtype: string
  status: string
  created_by: string
  /** How it was captured, in the developer's terms (review_impact.ORIGIN_LABELS). */
  origin: string
  timestamp: string | null
  /** Absent from a contexer older than the applicability change: read it as always. */
  applies_when?: Applicability
  actions: ReviewAction[]
  /** `applies_when` null: the proposal inherits the current applicability on approval. */
  proposed?: { content: string; title: string; applies_when?: Applicability | null }
  /** A Suggested Update whose content differs from the approved version (conflicts.has_open_conflict). */
  conflict?: boolean
  /** The side picked earlier with the developer (conflicts.memo_pick), if any. */
  pick?: 'update' | 'standing' | null
}

/** One side of a pair of contradicting current decisions. */
export type ConflictSide = {
  id: string
  title: string
  content: string
  status: string
  timestamp: string | null
  applies_when?: Applicability
  /** Only an approved decision may be kept over its contradiction (conflicts.can_keep). */
  can_keep: boolean
}

/** Two current decisions that prescribe incompatible things (console_api.current_conflicts). */
export type CurrentConflict = {
  kind: 'current_conflict'
  reason: string
  decisions: [ConflictSide, ConflictSide]
  actions: ConflictAction[]
}

export type ReviewQueue = {
  protocol: number
  repo: string
  count: number
  items: ReviewItem[]
  conflicts?: CurrentConflict[]
}

declare module 'claude-code' {
  interface PluginState {
    'contexer-review': {
      queue: ReviewQueue | null
      isHidden: boolean
      /** True for the moment the band steps aside so the pane it opens can take the keyboard. */
      isHandingOff: boolean
      editing: string | null
      note: string | null
      /** The card the pane shows: an index into its deck, clamped as cards are settled. */
      cursor: number
      /** The pane is up, so the band above the prompt steps aside. */
      isPaneOpen: boolean
    }
  }
}
