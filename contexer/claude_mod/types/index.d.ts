// The values the review mod keeps in `$.state` for the session, and the queue shape it reads
// from `contexer review --json` (console_api.review_queue, protocol 1).

export type ReviewItemKind = 'new' | 'update' | 'retirement' | 'reconsideration'

export type ReviewAction = 'approve' | 'edit' | 'ignore' | 'dismiss'

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
  actions: ReviewAction[]
  proposed?: { content: string; title: string }
}

export type ReviewQueue = {
  protocol: number
  repo: string
  count: number
  items: ReviewItem[]
}

declare module 'claude-code' {
  interface PluginState {
    'contexer-review': {
      queue: ReviewQueue | null
      isHidden: boolean
      editing: string | null
      note: string | null
    }
  }
}
