/**
 * Hermes Newswire — breaking-news ticker + newswire page.
 *
 * Unified agent+desktop package (electron/desktop-plugins-root.ts):
 *   SOURCE     ~/.hermes/plugins/hermes-newswire/desktop/plugin.js  (this file)
 *   app copy   ~/.hermes/desktop-plugins/hermes-newswire/plugin.js
 *              + .hermes-package.json marker (materialized by the app)
 *   backend    ~/.hermes/plugins/hermes-newswire/dashboard/plugin_api.py
 *              mounted at /api/plugins/hermes-newswire/ (plugins.enabled).
 *
 * Surfaces:
 *   - Bottom pane strip: continuous scrolling headlines in a thin (28px)
 *     persistent strip docked to the workspace's bottom edge — sits above
 *     the statusbar, inserts one layout row, never overlaps other panes.
 *   - ROUTES_AREA /newswire page: Latest / Sources / Settings.
 *   - SIDEBAR_NAV_AREA row + 5 PALETTE_AREA commands.
 *
 * Zero LLM usage: the renderer reads normalized cached state from the plugin
 * backend (SQLite), which alone performs feed polling. Feed HTML never reaches
 * this file — the backend strips tags/entities before storage; the renderer
 * only ever renders plain-text fields as React text children.
 *
 * Plain ESM loaded uncompiled: UI is jsx() calls, NOT JSX syntax; only
 * @hermes/plugin-sdk, react, react/jsx-runtime resolve.
 */

import {
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  ErrorState,
  GlyphSpinner,
  host,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  ScrollArea,
  SearchField,
  SegmentedControl,
  Separator,
  SIDEBAR_NAV_AREA,
  Switch,
  atom,
  queryClient,
  useMutation,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useMemo, useRef, useState } from 'react'

const ID = 'hermes-newswire'
const PAGE_PATH = '/newswire'

// Assigned in register(ctx) — components can't see ctx directly.
let rest = null
let openExternalFn = null
let applyTickerSettingsFn = null
let storageGet = null
let storageSet = null

// Renderer-owned ticker state (this window's presentation, not engine truth).
const $tickerPaused = atom(false)
// Palette "Add Source" signals the page to open the Sources tab + focus add.
const $addSourceSignal = atom(0)
let addFocusArmed = false
// Signal-lane shared state (renderer presentation, not engine truth).
const $lastItem = atom(null)    // last focused ticker/page item (any lane)
const $pinTab = atom(null)      // pin-chip click → page tab to surface
const __seenWatch = new Set()   // watch-article ids already notified (cap 250)
let __notifiedCrit = {}         // agent signal id -> crit-notified until ok

// ─────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────

const SPEED_DURATIONS = { slow: 240, normal: 150, fast: 80 } // seconds per loop (slowed ~2.5x on Tony's read-feedback)
const ROTATE_MS = 8000 // reduced-motion: static headline rotation
const TICKER_POLL_MS = 30_000
const PAGE_POLL_MS = 60_000

// Backend setting ranges (dashboard/plugin_api.py):
//   max_article_age_hours: int >= 0 (0 = keep forever)
//   max_headlines: int >= 0 (0 = keep everything)
//   refresh_interval: int in [30, 86400] seconds
const AGE_OPTIONS = [
  { id: '0', label: 'All' }, { id: '6', label: '6h' }, { id: '12', label: '12h' },
  { id: '24', label: '1d' }, { id: '72', label: '3d' }, { id: '168', label: '7d' }, { id: '720', label: '30d' }
]
const LIMIT_OPTIONS = [
  { id: '50', label: '50' }, { id: '100', label: '100' }, { id: '200', label: '200' },
  { id: '500', label: '500' }, { id: '0', label: 'All' }
]
const INTERVAL_OPTIONS = [
  { id: '60', label: '1m' }, { id: '120', label: '2m' }, { id: '300', label: '5m' },
  { id: '600', label: '10m' }, { id: '900', label: '15m' }, { id: '1800', label: '30m' }, { id: '3600', label: '1h' }
]
const SPEED_OPTIONS = [
  { id: 'slow', label: 'Slow' }, { id: 'normal', label: 'Normal' }, { id: 'fast', label: 'Fast' }
]
const FONT_OPTIONS = [
  { id: '9', label: '9' }, { id: '10', label: '10' }, { id: '11', label: '11' }, { id: '12', label: '12' },
  { id: '13', label: '13' }, { id: '14', label: '14' }, { id: '16', label: '16' }, { id: '18', label: '18' }
]
// Strip height follows the font so larger text never clips: a comfortable
// px gap above the tallest glyph (cap-height + descender + breathing room).
const fontToHeight = px => Math.max(28, Math.round(px * 2.1) + 6)

// First-run suggestions — OFFERED only, never auto-added. Neutral, stable,
// public feeds; no political bundles.
const STARTER_FEEDS = [
  { name: 'Hacker News', feed_url: 'https://hnrss.org/frontpage', category: 'developer' },
  { name: 'Ars Technica', feed_url: 'https://feeds.arstechnica.com/arstechnica/index', category: 'technology' },
  { name: 'The Verge', feed_url: 'https://www.theverge.com/rss/index.xml', category: 'technology' },
  { name: 'NASA News', feed_url: 'https://www.nasa.gov/news-release/feed/', category: 'science' },
  { name: 'TechCrunch', feed_url: 'https://techcrunch.com/feed/', category: 'technology' },
  { name: 'VentureBeat AI', feed_url: 'https://venturebeat.com/category/ai/feed/', category: 'ai' }
]

// ─────────────────────────────────────────────────────────────────────────
// Styles (content-compared, hot-reload safe)
// ─────────────────────────────────────────────────────────────────────────

function ensureStyles() {
  const css = [
    /* Ticker strip — pane body is our root: own the full box. */
    `.${ID}-ticker { display: flex; align-items: center; width: 100%; height: 100%; min-width: 0; overflow: hidden; background: var(--ui-bg-sidebar, var(--ui-bg-secondary)); border-top: 1px solid var(--ui-stroke-secondary); }`,
    `.${ID}-brand { display: inline-flex; align-items: center; gap: 0.25rem; flex: none; height: 100%; padding: 0 0.5rem; font-size: 0.625rem; font-weight: 700; letter-spacing: 0.08em; color: var(--ui-accent); cursor: pointer; user-select: none; background: none; border: 0; font-family: inherit; }`,
    `.${ID}-brand:hover { background: var(--chrome-action-hover); }`,
    `.${ID}-refresh { display: inline-flex; align-items: center; justify-content: center; flex: none; width: 1.25rem; height: 100%; background: none; border: 0; padding: 0; font-size: 0.6875rem; color: var(--ui-text-quaternary); cursor: pointer; }`,
    `.${ID}-refresh:hover { background: var(--chrome-action-hover); color: var(--ui-text-primary); }`,
    `.${ID}-refresh:focus-visible { outline: 1px solid var(--ui-accent); outline-offset: -1px; }`,
    `@keyframes ${ID}-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`,
    `.${ID}-refresh[data-busy="1"] { animation: ${ID}-spin 1s linear infinite; color: var(--ui-accent); }`,
    `.${ID}-viewport { flex: 1 1 0%; min-width: 0; height: 100%; overflow: hidden; }`,
    `.${ID}-track { display: flex; width: max-content; height: 100%; align-items: center; }`,
    `.${ID}-half { display: inline-flex; align-items: center; white-space: nowrap; }`,
    `.${ID}-item { display: inline-flex; align-items: center; gap: 0.375rem; padding: 0 1rem; height: 100%; background: none; border: 0; font: inherit; font-size: var(--nw-font, 11px); line-height: 1; color: var(--ui-text-tertiary); cursor: pointer; text-decoration: none; white-space: nowrap; }`,
    `.${ID}-item:hover { background: var(--chrome-action-hover); color: var(--ui-text-primary); }`,
    `.${ID}-item:focus-visible { outline: 1px solid var(--ui-accent); outline-offset: -1px; }`,
    `.${ID}-item[data-read="1"] .${ID}-headline { color: var(--ui-text-quaternary); }`,
    `.${ID}-src { color: var(--ui-text-quaternary); }`,
    `.${ID}-dot { color: var(--ui-accent); flex: none; }`,
    `.${ID}-favicon { flex: none; width: 14px; height: 14px; border-radius: 3px; object-fit: contain; background: none; }`,
    `.${ID}-section { display: flex; align-items: center; gap: 0.5rem; padding: 0.625rem 1rem 0.25rem; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ui-text-secondary); position: sticky; top: 0; background: var(--ui-bg-editor); border-bottom: 1px solid var(--ui-stroke-tertiary); }`,
    `.${ID}-sectioncount { color: var(--ui-text-quaternary); font-weight: 400; }`,
    `.${ID}-srcselect { background: var(--ui-bg-elevated); color: var(--ui-text-primary); }`,
    `.${ID}-srcselect option { background: var(--ui-bg-elevated); color: var(--ui-text-primary); }`,
    `.${ID}-divider { flex: none; padding: 0 0.75rem 0 0.25rem; font-size: 0.625rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ui-accent); white-space: nowrap; }`,
    `.${ID}-page .${ID}-favicon { width: 16px; height: 16px; }`,
    `.${ID}-age { color: var(--ui-text-quaternary); flex: none; }`,
    `.${ID}-marquee { animation: ${ID}-scroll var(--nw-duration, 55s) linear infinite; }`,
    `.${ID}-ticker:not(.${ID}-no-hover):hover .${ID}-marquee, .${ID}-ticker[data-paused="1"] .${ID}-marquee { animation-play-state: paused; }`,
    `@keyframes ${ID}-scroll { from { transform: translateX(0); } to { transform: translateX(-50%); } }`,
    `@media (prefers-reduced-motion: reduce) { .${ID}-marquee { animation: none; } }`,
    `.${ID}-page { display: flex; flex-direction: column; height: 100%; min-height: 0; }`,
    `.${ID}-tabs { display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem 1rem; border-bottom: 1px solid var(--ui-stroke-secondary); flex: none; flex-wrap: wrap; }`,
    `.${ID}-tab { background: none; border: 0; padding: 0.25rem 0.5rem; font-size: 0.8125rem; color: var(--ui-text-secondary); cursor: pointer; border-radius: 0.25rem; font-family: inherit; }`,
    `.${ID}-tab[data-active="1"] { color: var(--ui-text-primary); background: var(--ui-bg-tertiary); font-weight: 600; }`,
    `.${ID}-tab:hover { color: var(--ui-text-primary); }`,
    `.${ID}-scrollwrap { flex: 1 1 0%; min-height: 0; }`,
    `.${ID}-list { display: flex; flex-direction: column; }`,
    `.${ID}-row { display: flex; gap: 0.75rem; padding: 0.625rem 1rem; border-bottom: 1px solid var(--ui-stroke-tertiary, var(--ui-stroke-secondary)); align-items: flex-start; }`,
    `.${ID}-row:hover { background: var(--ui-bg-tertiary); }`,
    `.${ID}-row[data-read="1"] .${ID}-rowtitle { color: var(--ui-text-tertiary); }`,
    `.${ID}-rowmain { flex: 1 1 0%; min-width: 0; display: flex; flex-direction: column; gap: 0.25rem; }`,
    `.${ID}-rowtitle { font-size: 0.875rem; line-height: 1.3; color: var(--ui-text-primary); }`,
    `.${ID}-rowsum { font-size: 0.75rem; line-height: 1.4; color: var(--ui-text-secondary); overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }`,
    `.${ID}-meta { display: flex; gap: 0.5rem; align-items: center; font-size: 0.6875rem; color: var(--ui-text-quaternary); flex-wrap: wrap; }`,
    `.${ID}-srcrow { display: flex; align-items: center; gap: 0.75rem; padding: 0.625rem 1rem; border-bottom: 1px solid var(--ui-stroke-tertiary, var(--ui-stroke-secondary)); }`,
    `.${ID}-srcrow:hover { background: var(--ui-bg-tertiary); }`,
    `.${ID}-err { font-size: 0.6875rem; color: var(--ui-red, #e5484d); }`,
    `.${ID}-card { margin: 1rem; padding: 1rem; border: 1px solid var(--ui-stroke-secondary); border-radius: 0.5rem; display: flex; flex-direction: column; gap: 0.75rem; background: var(--ui-bg-card); }`,
    `.${ID}-setrow { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: 0.375rem 0; }`,
    `.${ID}-setlabel { font-size: 0.8125rem; color: var(--ui-text-secondary); }`,
    `.${ID}-setgroup { font-size: 0.6875rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ui-text-quaternary); margin: 0.75rem 0 0.25rem; }`,
    `.${ID}-chips { display: flex; flex-wrap: wrap; gap: 0.5rem; }`,
    `.${ID}-chip { display: inline-flex; align-items: center; gap: 0.375rem; padding: 0.25rem 0.625rem; border: 1px solid var(--ui-stroke-secondary); border-radius: 999px; font-size: 0.75rem; color: var(--ui-text-secondary); cursor: pointer; background: none; font-family: inherit; }`,
    `.${ID}-chip:hover { border-color: var(--ui-accent); color: var(--ui-text-primary); }`,
    `.${ID}-cand { display: flex; align-items: center; gap: 0.5rem; padding: 0.375rem 0.5rem; border: 1px solid var(--ui-stroke-tertiary, var(--ui-stroke-secondary)); border-radius: 0.375rem; cursor: pointer; font-size: 0.75rem; color: var(--ui-text-secondary); background: none; font-family: inherit; text-align: left; width: 100%; }`,
    `.${ID}-cand[data-picked="1"] { border-color: var(--ui-accent); color: var(--ui-text-primary); }`,
    `.${ID}-pager { display: flex; align-items: center; gap: 0.5rem; justify-content: center; padding: 0.75rem; }`,
    /* Keyboard a11y: every plugin-owned button (tabs, brand, chips, candidates,
       page-row titles) gets a visible focus ring built from theme vars. */
    `.${ID}-page button:focus-visible, .${ID}-ticker button:focus-visible { outline: 1px solid var(--ui-accent); outline-offset: -1px; }`,
    `.${ID}-page select:focus-visible { outline: 1px solid var(--ui-accent); outline-offset: 1px; }`,
    /* Signal lanes (news · trades · agent) + health rail + pins */
    `.${ID}-rail { display: inline-flex; align-items: center; gap: 0.25rem; flex: none; height: 100%; padding: 0 0.25rem 0 0.5rem; }`,
    `.${ID}-dotbtn { width: 0.5rem; height: 0.5rem; border-radius: 999px; border: 0; padding: 0; cursor: pointer; display: inline-block; flex: none; box-shadow: 0 0 0 1px var(--ui-bg-sidebar, var(--ui-bg-secondary)); }`,
    `.${ID}-dotbtn:hover { transform: scale(1.45); }`,
    `.${ID}-pins { display: inline-flex; align-items: center; gap: 0.25rem; flex: none; height: 100%; padding: 0 0.5rem; background: none; border: 0; font-size: 0.6875rem; font-weight: 700; color: var(--ui-red, #e5484d); cursor: pointer; font-family: inherit; }`,
    `.${ID}-pins:hover { background: var(--chrome-action-hover); }`,
    `.${ID}-watch { color: var(--ui-accent); font-weight: 700; }`
  ].join('\n')
  let style = document.getElementById(`${ID}-styles`)
  if (!style) {
    style = document.createElement('style')
    style.id = `${ID}-styles`
    document.head.appendChild(style)
  }
  if (style.textContent !== css) {
    style.textContent = css
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────

function relTime(iso) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return ''
  const s = Math.max(0, (Date.now() - t) / 1000)
  if (s < 60) return 'now'
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

function absTime(iso) {
  if (!iso) return ''
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return ''
  return t.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

// The bridge surfaces backend HTTPException details as "409: {"detail":...}" —
// pull the human message out of the JSON blob when it's there.
function errText(e) {
  const raw = String(e?.message || e || '')
  const m = /"message"\s*:\s*"((?:[^"\\]|\\.)*)"/.exec(raw)
  if (m) {
    try { return JSON.parse(`"${m[1]}"`) } catch { /* fall through */ }
  }
  return raw.slice(0, 300)
}

// open_article_behavior: 'internal' (default, Tony's pref) opens in the
// Hermes preview pane via the plugin backend's SSRF-gated /preview route,
// which emits the same preview.open gateway event the app's own
// open_preview tool uses. 'external' keeps the OS browser.
let openArticleMode = 'internal'
async function openArticle(url, articleId) {
  if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return
  if (openArticleMode === 'internal') {
    try {
      await rest('/preview', { method: 'POST', body: { url } })
    } catch {
      // Preview unavailable (non-desktop gateway / pane closed) — fall back
      // to the external browser rather than doing nothing.
      try { await openExternalFn(url) } catch { /* result-shaped; ignore */ }
    }
  } else {
    try { await openExternalFn(url) } catch { /* result-shaped; ignore */ }
  }
  if (articleId != null) {
    try {
      await rest(`/articles/${articleId}/read`, { method: 'POST', body: { read: true } })
      queryClient.invalidateQueries({ queryKey: [ID] })
    } catch { /* non-fatal */ }
  }
}

// "Ask Hermes about this" — official SDK path: host.request is the gateway
// JSON-RPC door (same one the app itself uses) and prompt.submit is the
// documented submit method. Sends into the FOCUSED chat session. The item
// shape is lane-aware: articles summarize, trades ask about the position,
// agent events ask what to do.
async function askHermes(it) {
  if (!it) return
  let prompt
  if (it.kind === 'trade') {
    prompt = `My Hyperliquid lane shows: ${it.title}${it.detail ? ` (${it.detail})` : ''}. Explain what this means and whether my position is at risk.`
  } else if (it.kind === 'agent') {
    prompt = `My Hermes agent reports: ${it.title}${it.detail ? ` (${it.detail})` : ''}. What should I do about it?`
  } else {
    prompt = `Summarize this article and tell me why it matters:\n\n${it.title || '(untitled)'}\n${it.url || it.canonical_url || ''}`
  }
  const sid = host.state?.focusedSessionId?.get?.() || host.state?.activeSessionId?.get?.() || null
  try {
    if (!sid) throw new Error('No active chat session')
    if (host.state?.busyBySession?.get?.()?.[sid]) {
      host.notify({ kind: 'info', message: 'Chat is busy — try again when the current turn finishes.' })
      return
    }
    const out = await host.request('prompt.submit', { session_id: sid, text: prompt })
    if (out && typeof out === 'object' && out.status && out.status !== 'streaming') {
      throw new Error(`Unexpected submit status: ${out.status}`)
    }
    host.notify({ kind: 'success', message: 'Asked Hermes — see the chat.' })
  } catch (e) {
    host.notifyError(e, 'Could not send to chat')
  }
}

function useReducedMotion() {
  const [reduced, setReduced] = useState(() =>
    typeof window !== 'undefined' && window.matchMedia
      ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false
  )
  useEffect(() => {
    if (!window.matchMedia) return
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = e => setReduced(e.matches)
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])
  return reduced
}

// One shared minute tick so relative ages refresh without per-row timers.
function useAgeTick(ms = 30_000) {
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick(n => n + 1), ms)
    return () => clearInterval(t)
  }, [ms])
}

// ─────────────────────────────────────────────────────────────────────────
// Shared data hooks (unwrapped backend envelopes)
// ─────────────────────────────────────────────────────────────────────────

function useSettings() {
  const q = useQuery({
    queryKey: [ID, 'settings'],
    queryFn: async () => ((await rest('/settings')) || {}).settings || null,
    refetchInterval: TICKER_POLL_MS,
    staleTime: 15_000,
    retry: 1
  })
  return [q, q.data]
}

function useSources() {
  const q = useQuery({
    queryKey: [ID, 'sources'],
    queryFn: async () => ((await rest('/sources')) || {}).sources || [],
    refetchInterval: PAGE_POLL_MS,
    staleTime: 10_000,
    retry: 1
  })
  return [q, Array.isArray(q.data) ? q.data : []]
}

function useTrades() {
  return useQuery({
    queryKey: [ID, 'trades'],
    queryFn: async () => ((await rest('/trades')) || {}),
    refetchInterval: 45_000,
    staleTime: 40_000,
    retry: 1
  })
}

function useAgentHealth() {
  return useQuery({
    queryKey: [ID, 'agent'],
    queryFn: async () => ((await rest('/agent/health')) || { signals: [], pins: [] }),
    refetchInterval: 45_000,
    staleTime: 40_000,
    retry: 1
  })
}

// ─────────────────────────────────────────────────────────────────────────
// Ticker (statusbar)
// ─────────────────────────────────────────────────────────────────────────

// One renderer for every lane. Articles carry article fields; trade/agent
// items carry {kind, lane, label, sym, title, detail, url, watch}.
function TickerItem({ it, settings }) {
  const age = it.published_at && settings?.relative_time !== false ? relTime(it.published_at) : (it.age || '')
  const isArticle = it.kind !== 'trade' && it.kind !== 'agent'
  return jsx('button', {
    className: `${ID}-item`,
    'data-read': it.read ? '1' : '0',
    title: `${it.label || it.source_name || ''} — ${it.title}${it.detail ? ` (${it.detail})` : ''}`,
    'aria-label': `${it.label || it.source_name || ''}: ${it.title} — activate to open, or right-click / long-press to ask Hermes`,
    onClick: () => {
      $lastItem.set(it)
      if (it.url) void openArticle(it.url, isArticle ? it.id : null)
    },
    onContextMenu: e => {
      e.preventDefault()
      $lastItem.set(it)
      void askHermes(it)
    },
    children: jsxs('span', {
      style: { display: 'inline-flex', alignItems: 'center', gap: '0.375rem' },
      children: [
        it.favicon_url
          ? jsx('img', { src: it.favicon_url, className: `${ID}-favicon`, alt: '',
              onError: e => { e.currentTarget.style.display = 'none' } })
          : jsx('span', { className: `${ID}-dot`, children: it.sym || '◆' }),
        settings?.show_source !== false ? jsx('span', { className: `${ID}-src`, children: `${it.label || it.source_name}:` }) : null,
        jsx('span', { className: `${ID}-headline${it.watch ? ` ${ID}-watch` : ''}`, children: it.title }),
        age ? jsx('span', { className: `${ID}-age`, children: `· ${age}` }) : null
      ]
    })
  })
}

function TickerHalf({ items, settings }) {
  return jsx('div', {
    className: `${ID}-half`,
    'aria-hidden': 'true',
    children: items.map((it, i) => it.__divider
      ? jsx('span', { className: `${ID}-divider`, key: `d${i}`, children: `${it.label} —` })
      : jsx(TickerItem, { it, settings }, `t${it.key || i}`))
  })
}

// Ticker grouping: how the strip orders its stories.
//  newest      — global chronological mix (the classic wire)
//  source      — one block per source, most-recently-updated source first,
//                newest within each block; a ' • ' divider between blocks
//  unread_first— catch-up mode: unread stories lead (newest first), then the
//                rest chronologically
function groupTickerArticles(articles, mode) {
  if (!Array.isArray(articles) || articles.length === 0) return []
  if (mode === 'source') {
    const bySrc = new Map()
    for (const a of articles) {
      const k = a.source_id ?? a.source_name
      if (!bySrc.has(k)) bySrc.set(k, { name: a.source_name, list: [] })
      bySrc.get(k).list.push(a)
    }
    const blocks = [...bySrc.values()].map(b => {
      b.list.sort((x, y) => Date.parse(y.published_at || y.discovered_at || 0) - Date.parse(x.published_at || x.discovered_at || 0))
      b.newest = Date.parse(b.list[0].published_at || b.list[0].discovered_at || 0)
      return b
    })
    blocks.sort((x, y) => y.newest - x.newest)
    const out = []
    blocks.forEach((b, i) => {
      if (i > 0) out.push({ __divider: true, source_name: b.name })
      out.push(...b.list)
    })
    return out
  }
  if (mode === 'unread_first') {
    const byTime = (x, y) => Date.parse(y.published_at || y.discovered_at || 0) - Date.parse(x.published_at || x.discovered_at || 0)
    const unread = articles.filter(a => !a.read).sort(byTime)
    const read = articles.filter(a => a.read).sort(byTime)
    return [...unread, ...read]
  }
  return articles // newest: backend already orders chronologically
}

// Tiny ticker-end control: force-fetch all feeds now. Module-level busy flag
// dedupes rapid clicks across the ticker's remounts.
let __tickerRefreshBusy = false
function TickerRefresh() {
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState(null) // 'ok' | 'err' | null
  const onClick = async () => {
    if (__tickerRefreshBusy) return
    __tickerRefreshBusy = true
    setBusy(true); setFlash(null)
    try {
      await rest('/refresh-all', { method: 'POST', body: {} })
      await queryClient.invalidateQueries({ queryKey: [ID] })
      setFlash('ok')
    } catch {
      setFlash('err')
    } finally {
      __tickerRefreshBusy = false
      setBusy(false)
      setTimeout(() => setFlash(null), 2000)
    }
  }
  return jsx('button', {
    className: `${ID}-refresh`,
    'data-busy': busy ? '1' : '0',
    title: busy ? 'Fetching feeds…' : 'Refresh all feeds now',
    'aria-label': 'Refresh all newswire feeds now',
    onClick: () => void onClick(),
    children: busy ? '⟳' : (flash === 'ok' ? '✓' : flash === 'err' ? '!' : '⟳')
  })
}

// Build the flat ticker item stream: [trades lane] [agent lane] [news lane].
function buildTickerItems({ articles, grouping, trades, health, lanes, paused }) {
  const out = []
  const laneOn = k => !lanes || lanes[k] !== false
  if (laneOn('trades') && trades) {
    const t = trades
    if (Array.isArray(t.positions) && t.positions.length) {
      t.positions.slice(0, 4).forEach(p => {
        const sign = p.upnl >= 0 ? '+' : ''
        const liqTxt = p.liq_pct != null ? ` · liq ${p.liq_pct}%` : ''
        const cvTxt = p.conviction != null ? ` · cv ${p.conviction}` : ''
        out.push({
          kind: 'trade', lane: 'trades', key: `t${p.coin}`, label: 'TRADES',
          sym: p.side === 'LONG' ? '▲' : '▼',
          title: `${p.coin} ${p.side} ${p.size} · ${sign}$${p.upnl} · ${p.lev || '?'}x${liqTxt}${cvTxt}`,
          detail: `entry ${p.entry_px} mark ${p.mark_px}`, url: null
        })
      })
    } else if (t.total_value != null || t.account_value != null) {
      const last = (t.fills && t.fills[0]) || null
      const lastTxt = last ? ` · last ${last.dir} ${last.sz} ${last.coin} @ ${last.px}` : ''
      // total_value = perp margin + spot cash. account_value alone reads $0 on a
      // flat-but-funded account (Hyperliquid keeps the two ledgers separate).
      const acct = t.total_value ?? t.account_value
      const split = t.spot_value != null ? ` · perp $${t.account_value} + spot $${t.spot_value}` : ''
      out.push({
        kind: 'trade', lane: 'trades', key: 'tflat', label: 'TRADES', sym: '◆',
        title: `Flat · acct $${acct}${split}${lastTxt}`,
        detail: t.error || '', url: null
      })
    } else if (t.error) {
      out.push({ kind: 'trade', lane: 'trades', key: 'terr', label: 'TRADES', sym: '!', title: `HL: ${t.error}`, detail: '', url: null })
    }
  }
  if (laneOn('agent') && health) {
    const pinItems = (health.pins || []).slice(0, 2)
    const warnSigs = (health.signals || []).filter(s => s.level !== 'ok').slice(0, 2)
    pinItems.forEach(p => out.push({ kind: 'agent', lane: 'agent', key: `a${p.kind}${p.ts || p.title}`, label: 'AGENT', sym: '●', title: p.title, detail: p.ts || '', url: null }))
    warnSigs.forEach(s => out.push({ kind: 'agent', lane: 'agent', key: `s${s.id}`, label: 'AGENT', sym: '●', title: `${s.label}: ${s.detail}`, detail: '', url: null }))
    if (!pinItems.length && !warnSigs.length && (health.signals || []).length) {
      out.push({ kind: 'agent', lane: 'agent', key: 'aok', label: 'AGENT', sym: '●', title: 'Agent: all systems green', detail: '', url: null })
    }
  }
  const newsItems = groupTickerArticles(paused ? [] : (articles || []), grouping)
    .map(a => ({ kind: 'article', lane: 'news', key: `n${a.id}`, label: a.source_name, title: a.title, age: '', published_at: a.published_at, url: a.canonical_url, id: a.id, read: a.read, watch: a.watch, favicon_url: a.favicon_url }))
  out.push(...newsItems)
  return out
}

const LEVEL_COLOR = { ok: 'var(--ui-green, #46a758)', warn: 'var(--ui-amber, #f5a623)', crit: 'var(--ui-red, #e5484d)' }

function HealthRail({ signals, onOpen }) {
  if (!Array.isArray(signals) || !signals.length) return null
  return jsx('div', { className: `${ID}-rail`, 'aria-label': 'Agent health', children: signals.map(s =>
    jsx('button', {
      key: s.id, className: `${ID}-dotbtn`,
      title: `${s.label}: ${s.detail}`,
      'aria-label': `${s.label}: ${s.detail}`,
      style: { background: LEVEL_COLOR[s.level] || 'var(--ui-text-quaternary)' },
      onClick: () => onOpen(s.id)
    })
  ) })
}

function PinsCluster({ pins, onOpen }) {
  const high = (pins || []).filter(p => p.severity === 'high')
  if (!high.length) return null
  return jsx('button', {
    className: `${ID}-pins`,
    title: high.map(p => p.title).join(' · '),
    'aria-label': `${high.length} high-priority signal${high.length === 1 ? '' : 's'}`,
    onClick: () => onOpen(),
    children: [`⚠ ${high.length}`]
  })
}

function NewswireTicker() {
  const [settingsQ, settings] = useSettings()
  const paused = useValue($tickerPaused)
  const enabled = settings ? settings.ticker_enabled !== false : false
  const duration = SPEED_DURATIONS[settings?.ticker_speed] || 150
  const lanes = settings?.ticker_lanes || { news: true, trades: true, agent: true }

  const articlesQ = useQuery({
    queryKey: [ID, 'ticker', settings?.only_unread === true],
    queryFn: async () => {
      const unread = settings?.only_unread === true ? '&unread=1' : ''
      const out = await rest(`/articles?limit=50&include_summary=false${unread}`)
      return Array.isArray(out?.items) ? out.items : []
    },
    refetchInterval: TICKER_POLL_MS,
    staleTime: TICKER_POLL_MS - 5_000,
    retry: false
  })
  const tradesQ = useTrades()
  const healthQ = useAgentHealth()
  const reduced = useReducedMotion()
  useAgeTick()
  const fontSizePx = Math.min(20, Math.max(9, Number(settings?.ticker_font_size) || 11))
  const grouping = settings?.ticker_grouping || 'newest'

  const flatItems = useMemo(
    () => buildTickerItems({ articles: articlesQ.data || [], grouping, trades: tradesQ.data, health: healthQ.data, lanes, paused }),
    [settings, articlesQ.data, tradesQ.data, healthQ.data, paused, grouping, lanes]
  )
  openArticleMode = settings?.open_article_behavior === 'external' ? 'external' : 'internal'

  // Pane height tracks the font setting from ANY settings refresh (poll,
  // UI save, or another surface). applyTickerSettings is idempotent on
  // unchanged shape (guarded by lastPaneKey) — the ticker's own remount
  // re-runs this effect but the second call is a no-op, so no loop.
  useEffect(() => {
    if (settings && applyTickerSettingsFn) applyTickerSettingsFn(settings)
  }, [settings?.ticker_font_size, settings?.ticker_enabled])

  // Watchlist match → desktop notification (deduped per article id).
  const notifyEnabled = settings?.notify_on_watch !== false
  useEffect(() => {
    if (!notifyEnabled) return
    const list = articlesQ.data || []
    for (let i = 0; i < list.length; i++) {
      const a = list[i]
      if (a.watch && !a.read && !__seenWatch.has(a.id)) {
        __seenWatch.add(a.id)
        if (__seenWatch.size > 250) __seenWatch.delete(__seenWatch.values().next().value)
        if (i < 8) host.notify({ kind: 'info', message: `📰 ${a.source_name}: ${a.title}` })
      }
    }
  }, [articlesQ.data, notifyEnabled])

  // Agent crit level → notify once per signal until it returns to ok.
  useEffect(() => {
    const sigs = healthQ.data?.signals || []
    sigs.forEach(s => {
      if (s.level === 'crit' && !__notifiedCrit[s.id]) {
        __notifiedCrit[s.id] = true
        host.notify({ kind: 'info', message: `⚠ ${s.label}: ${s.detail}` })
      }
      if (s.level === 'ok') __notifiedCrit[s.id] = false
    })
  }, [healthQ.data])

  // Pins = watch-matched news + agent high-severity events (Alert pins).
  const pins = useMemo(() => {
    const out = []
    for (const a of (articlesQ.data || [])) if (a.watch) out.push({ lane: 'news', severity: 'high', title: a.title })
    for (const p of (healthQ.data?.pins || [])) out.push(p)
    return out.slice(0, 9)
  }, [articlesQ.data, healthQ.data])

  const onPinOpen = () => {
    const agentPin = pins.find(p => p.lane === 'agent')
    $pinTab.set(agentPin ? 'agent' : 'watchlist')
    host.navigate(PAGE_PATH)
  }
  const onHealthOpen = () => { $pinTab.set('agent'); host.navigate(PAGE_PATH) }

  // Reduced motion: rotate ONE static headline instead of a marquee.
  const [rotIdx, setRotIdx] = useState(0)
  useEffect(() => {
    if (!reduced || flatItems.length === 0) return
    const t = setInterval(() => setRotIdx(i => (i + 1) % flatItems.length), ROTATE_MS)
    return () => clearInterval(t)
  }, [reduced, flatItems.length])

  if (!enabled) return null
  if (settingsQ.isLoading && !settings) {
    return jsx('div', { className: `${ID}-ticker`, style: { '--nw-font': `${fontSizePx}px` }, children: jsx('span', { className: `${ID}-brand`, children: 'NEWSWIRE' }) })
  }
  if (flatItems.length === 0) {
    return jsxs('div', {
      className: `${ID}-ticker`,
      style: { '--nw-font': `${fontSizePx}px` },
      role: 'region',
      'aria-label': 'Newswire ticker',
      children: [
        jsx('button', {
          className: `${ID}-brand`,
          onClick: () => host.navigate(PAGE_PATH),
          title: 'Open Newswire',
          children: 'NEWSWIRE'
        }),
        jsx('button', {
          className: `${ID}-item`,
          onClick: () => host.navigate(PAGE_PATH),
          children: paused ? 'Ticker paused' : 'Add a news source →'
        })
      ]
    })
  }

  const content = reduced
    ? jsx('div', { className: `${ID}-viewport`, children: jsx(TickerItem, { it: flatItems[rotIdx % flatItems.length], settings }) })
    : jsx('div', { className: `${ID}-viewport`, children:
        jsx('div', {
          className: `${ID}-track ${ID}-marquee`,
          style: { '--nw-duration': `${duration}s` },
          children: [
            jsx(TickerHalf, { items: flatItems, settings, key: 'a' }),
            jsx(TickerHalf, { items: flatItems, settings, key: 'b' })
          ]
        })})

  return jsxs('div', {
    className: `${ID}-ticker${settings?.pause_on_hover === false ? ` ${ID}-no-hover` : ''}`,
    'data-paused': paused ? '1' : '0',
    role: 'region',
    'aria-label': 'Newswire ticker',
    style: { '--nw-font': `${fontSizePx}px` },
    children: [
      jsx('button', {
        className: `${ID}-brand`,
        onClick: () => host.navigate(PAGE_PATH),
        title: 'Open Newswire',
        children: 'NEWSWIRE'
      }),
      jsx(TickerRefresh, {}),
      settings?.pins_enabled !== false ? jsx(PinsCluster, { pins, onOpen: onPinOpen }) : null,
      jsx(HealthRail, { signals: healthQ.data?.signals, onOpen: onHealthOpen }),
      content
    ]
  })
}

// ─────────────────────────────────────────────────────────────────────────
// Page — Latest
// ─────────────────────────────────────────────────────────────────────────

function ArticleRow({ a }) {
  return jsxs('div', { className: `${ID}-row`, 'data-read': a.read ? '1' : '0', children: [
    jsx('div', {
      className: `${ID}-rowmain`,
      children: [
        jsx('button', {
          className: `${ID}-rowtitle`,
          style: { textAlign: 'left', background: 'none', border: 0, padding: 0, cursor: 'pointer', font: 'inherit' },
          title: a.canonical_url || '',
          onClick: () => {
            $lastItem.set({ kind: 'article', title: a.title, url: a.canonical_url, id: a.id })
            void openArticle(a.canonical_url || '', a.id)
          },
          children: a.title || '(untitled)'
        }),
        a.summary ? jsx('div', { className: `${ID}-rowsum`, children: a.summary }) : null,
        jsxs('div', { className: `${ID}-meta`, children: [
          a.favicon_url ? jsx('img', { src: a.favicon_url, className: `${ID}-favicon`, alt: '',
            onError: e => { e.currentTarget.style.display = 'none' } }) : null,
          jsx('span', { children: a.source_name }),
          a.read ? jsx('span', { children: '· read' }) : null,
          jsx('span', { title: absTime(a.published_at), children: relTime(a.published_at) || '—' })
        ]})
      ]
    }),
    jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: '0.25rem', flex: 'none' }, children: [
      jsx(Button, {
        size: 'xs', variant: 'ghost',
        onClick: () => void openArticle(a.canonical_url || '', a.id),
        children: 'Open'
      }),
      jsx(Button, {
        size: 'xs', variant: 'ghost',
        title: 'Send this headline to the focused Hermes chat',
        onClick: () => { $lastItem.set({ kind: 'article', title: a.title, url: a.canonical_url, id: a.id }); void askHermes(a) },
        children: 'Ask…'
      }),
      jsx(Button, {
        size: 'xs', variant: 'ghost',
        onClick: async () => {
          try {
            await rest(`/articles/${a.id}/read`, { method: 'POST', body: { read: !a.read } })
            queryClient.invalidateQueries({ queryKey: [ID] })
          } catch { /* surfaced by refetch */ }
        },
        children: a.read ? 'Unread' : 'Read'
      })
    ]})
  ]})
}

const PAGE_SIZE = 50

function LatestTab({ sources, prefs, setPrefs }) {
  const [q, setQ] = useState(prefs.search || '')
  const [sourceId, setSourceId] = useState(prefs.sourceId || 'all')
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [offset, setOffset] = useState(0)
  useAgeTick(60_000)

  // Client-side search over fetched pages (backend has no q param); debounce.
  const [debouncedQ, setDebouncedQ] = useState(q)
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 250)
    return () => clearTimeout(t)
  }, [q])

  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) })
  if (sourceId !== 'all') params.set('source_id', sourceId)
  if (unreadOnly) params.set('unread', '1')

  const arts = useQuery({
    queryKey: [ID, 'articles', sourceId, unreadOnly, offset],
    queryFn: () => rest(`/articles?${params.toString()}`),
    refetchInterval: PAGE_POLL_MS,
    staleTime: 15_000,
    retry: 1
  })

  // Force a REAL backend refresh (POST /refresh-all fetches every enabled
  // feed), then invalidate so both the list and the ticker see new articles
  // immediately — the user shouldn't wait after adding feeds.
  const hardRefresh = useMutation({
    mutationFn: () => rest('/refresh-all', { method: 'POST', body: {} }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [ID] }),
    onError: () => queryClient.invalidateQueries({ queryKey: [ID] })
  })

  const markAll = useMutation({
    mutationFn: () => rest('/articles/read-all', { method: 'POST', body: {} }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [ID] })
  })

  const fetched = Array.isArray(arts.data?.items) ? arts.data.items : []
  const [grouping, setGrouping] = useState(() => storageGet?.('latest.grouping', 'newest') || 'newest')
  useEffect(() => { storageSet?.('latest.grouping', grouping) }, [grouping])
  const needle = debouncedQ.trim().toLowerCase()
  const filtered = needle
    ? fetched.filter(a =>
        (a.title || '').toLowerCase().includes(needle) ||
        (a.summary || '').toLowerCase().includes(needle) ||
        (a.source_name || '').toLowerCase().includes(needle))
    : fetched
  // Page grouping: section headers between source blocks on the page (unlike
  // the ticker's inline divider, there's room for a real header row here).
  const sections = useMemo(() => {
    if (grouping === 'source') {
      const bySrc = new Map()
      for (const a of filtered) {
        const k = a.source_id ?? a.source_name
        if (!bySrc.has(k)) bySrc.set(k, { name: a.source_name, favicon: a.favicon_url, list: [] })
        bySrc.get(k).list.push(a)
      }
      const blocks = [...bySrc.values()].map(b => {
        b.list.sort((x, y) => Date.parse(y.published_at || y.discovered_at || 0) - Date.parse(x.published_at || x.discovered_at || 0))
        b.newest = Date.parse(b.list[0].published_at || b.list[0].discovered_at || 0)
        return b
      })
      blocks.sort((x, y) => y.newest - x.newest)
      return blocks
    }
    if (grouping === 'unread_first') {
      const byTime = (x, y) => Date.parse(y.published_at || y.discovered_at || 0) - Date.parse(x.published_at || x.discovered_at || 0)
      return [{ name: null, list: [...filtered.filter(a => !a.read).sort(byTime), ...filtered.filter(a => a.read).sort(byTime)] }]
    }
    return [{ name: null, list: filtered }]
  }, [filtered, grouping])
  const total = arts.data?.total ?? 0

  // Persist the last filter for next visit (UI pref, ctx.storage).
  useEffect(() => {
    setPrefs({ search: q, sourceId })
  }, [q, sourceId]) // eslint-disable-line react-hooks/exhaustive-deps

  return jsxs('div', { className: `${ID}-page`, children: [
    jsxs('div', { className: `${ID}-tabs`, children: [
      jsx(SearchField, {
        value: q,
        onChange: v => { setQ(typeof v === 'string' ? v : v?.target?.value ?? ''); setOffset(0) },
        placeholder: 'Search headlines…',
        containerClassName: 'w-64'
      }),
      jsx('select', {
        value: sourceId,
        onChange: e => { setSourceId(e.target.value); setOffset(0) },
        'aria-label': 'Filter articles by source',
        className: `${ID}-srcselect rounded-md border border-(--ui-stroke-secondary) px-2 py-1 text-xs`,
        children: [jsx('option', { value: 'all', children: 'All sources' })].concat(
          sources.map(s => jsx('option', { value: String(s.id), children: s.name }, s.id))
        )
      }),
      jsxs('label', { className: `${ID}-setlabel`, style: { gap: '0.375rem', display: 'inline-flex', alignItems: 'center' }, children: [
        jsx(Switch, { size: 'xs', checked: unreadOnly, onCheckedChange: v => { setUnreadOnly(v); setOffset(0) } }),
        'Unread only'
      ]}),
      jsx(SegmentedControl, {
        value: grouping,
        onChange: setGrouping,
        options: [
          { id: 'newest', label: 'Newest' },
          { id: 'source', label: 'By source' },
          { id: 'unread_first', label: 'Unread first' }
        ]
      }),
      jsx('span', { style: { flex: 1 } }),
      jsx(Button, {
        size: 'xs', variant: 'ghost',
        title: 'Fetch all feeds now (don\'t wait for the next scheduled poll)',
        onClick: () => void hardRefresh.mutate(),
        disabled: hardRefresh.isPending,
        children: hardRefresh.isPending ? 'Refreshing feeds…' : 'Refresh now'
      }),
      jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => markAll.mutate(), disabled: markAll.isPending, children: 'Mark all read' })
    ]}),
    jsx('div', { className: `${ID}-scrollwrap`, children:
      jsx(ScrollArea, { className: 'h-full', children:
        arts.isLoading
          ? jsx('div', { className: 'grid h-full place-items-center p-4', children: jsx(GlyphSpinner, {}) })
          : arts.isError
            ? jsx('div', { className: 'grid h-full place-items-center p-4', children: jsx(ErrorState, { title: 'Could not load articles', description: errText(arts.error) }) })
            : sections.every(s => s.list.length === 0)
              ? jsx('div', { className: 'grid h-full place-items-center p-4', children: jsx(EmptyState, { title: needle ? 'No matching headlines' : 'No articles yet', description: needle ? 'Try a different search.' : 'Add a source and refresh.' }) })
              : jsx('div', { className: `${ID}-list`, children: sections.map((sec, si) => jsxs('div', { 'data-section': si, children: [
                  sec.name ? jsxs('div', { className: `${ID}-section`, children: [
                    sec.favicon ? jsx('img', { src: sec.favicon, className: `${ID}-favicon`, alt: '' }) : null,
                    jsx('span', { children: sec.name }),
                    jsx('span', { className: `${ID}-sectioncount`, children: `${sec.list.length}` })
                  ] }) : null,
                  ...sec.list.map(a => jsx(ArticleRow, { a, key: a.id }))
                ] }, `sec${si}`)) })
      })
    }),
    jsxs('div', { className: `${ID}-pager`, children: [
      jsx(Button, { size: 'xs', variant: 'outline', disabled: offset === 0, onClick: () => setOffset(Math.max(0, offset - PAGE_SIZE)), children: '← Newer' }),
      jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: `${total} article${total === 1 ? '' : 's'}` }),
      jsx(Button, { size: 'xs', variant: 'outline', disabled: fetched.length < PAGE_SIZE, onClick: () => setOffset(offset + PAGE_SIZE), children: 'Older →' })
    ]})
  ]})
}

// ─────────────────────────────────────────────────────────────────────────
// Page — Watchlist / Trades / Agent (signal-lane detail tabs)
// ─────────────────────────────────────────────────────────────────────────

function WatchlistTab({ sources, prefs, setPrefs }) {
  const [settingsQ, settings] = useSettings()
  const arts = useQuery({
    queryKey: [ID, 'articles', 'watch'],
    queryFn: () => rest('/articles?watch=1&limit=100'),
    refetchInterval: PAGE_POLL_MS,
    staleTime: 15_000,
    retry: 1
  })
  const notify = useMutation({
    mutationFn: v => rest('/settings', { method: 'PATCH', body: { notify_on_watch: v } }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [ID, 'settings'] })
  })
  useAgeTick(60_000)
  const items = Array.isArray(arts.data?.items) ? arts.data.items : []
  return jsxs('div', { className: `${ID}-page`, children: [
    jsxs('div', { className: `${ID}-tabs`, children: [
      jsx('span', { className: 'text-sm text-(--ui-text-primary)', children: `Watchlist (${arts.data?.total ?? 0})` }),
      jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: (settings?.watchlist || []).join(', ') }),
      jsx('span', { style: { flex: 1 } }),
      jsx('label', { className: `${ID}-setlabel`, style: { gap: '0.375rem', display: 'inline-flex', alignItems: 'center' }, children: [
        jsx(Switch, { size: 'xs', checked: settings?.notify_on_watch !== false, onCheckedChange: v => notify.mutate(v) }),
        'Notify on match'
      ]})
    ]}),
    jsx('div', { className: `${ID}-scrollwrap`, children:
      jsx(ScrollArea, { className: 'h-full', children:
        arts.isLoading
          ? jsx('div', { className: 'grid h-full place-items-center p-4', children: jsx(GlyphSpinner, {}) })
          : items.length === 0
            ? jsxs('div', { className: `${ID}-card`, children: [
                jsx('span', { className: `${ID}-setlabel`, style: { fontSize: '0.8125rem', color: 'var(--ui-text-primary)', fontWeight: 600 }, children: 'No watchlist hits yet' }),
                jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Matched headlines appear here, light up in the ticker, and can fire a desktop notification. Edit keywords in Settings → Signals.' })
              ]})
            : jsx('div', { className: `${ID}-list`, children: items.map(a => jsx(ArticleRow, { a, key: a.id })) })
      })
    })
  ]})
}

function TradesTab() {
  const q = useTrades()
  const t = q.data || {}
  const refresh = () => queryClient.invalidateQueries({ queryKey: [ID, 'trades'] })
  const pos = t.positions || []
  const spot = t.spot || []
  const fills = t.fills || []
  return jsxs('div', { className: `${ID}-page`, children: [
    jsxs('div', { className: `${ID}-tabs`, children: [
      jsx('span', { className: 'text-sm text-(--ui-text-primary)', children: 'Hyperliquid' }),
      (t.total_value ?? t.account_value) != null ? jsx(Badge, { variant: 'outline', children: `acct $${t.total_value ?? t.account_value}${t.spot_value != null ? ` (perp $${t.account_value} + spot $${t.spot_value})` : ''}${t.withdrawable != null ? ` · withdrawable $${t.withdrawable}` : ''}` }) : null,
      t.fetched_at ? jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: `as of ${relTime(t.fetched_at)} ago` }) : null,
      jsx('span', { style: { flex: 1 } }),
      jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => refresh(), disabled: q.isFetching, children: q.isFetching ? 'Refreshing…' : 'Refresh' })
    ]}),
    t.error ? jsx('div', { className: `${ID}-card`, children: jsx('span', { className: `${ID}-err`, children: `Hyperliquid: ${t.error}` }) }) : null,
    jsx('div', { className: `${ID}-scrollwrap`, children:
      jsx(ScrollArea, { className: 'h-full', children:
        q.isLoading
          ? jsx('div', { className: 'grid h-full place-items-center p-4', children: jsx(GlyphSpinner, {}) })
          : jsxs('div', { children: [
              pos.length === 0 && !t.error
                ? jsxs('div', { className: `${ID}-card`, children: [
                    jsx('span', { className: `${ID}-setlabel`, style: { fontSize: '0.8125rem', color: 'var(--ui-text-primary)', fontWeight: 600 }, children: 'No open positions' }),
                    jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Account is flat. The ticker trades lane shows account value + last fill instead.' })
                  ]})
                : jsx('div', { className: `${ID}-list`, children: pos.map(p => jsxs('div', { className: `${ID}-row`, children: [
                    jsx('div', { className: `${ID}-rowmain`, children: [
                      jsxs('span', { className: 'text-sm text-(--ui-text-primary)', children: [
                        p.coin,
                        p.dex ? jsx(Badge, { variant: 'outline', children: p.dex }) : null,
                        jsx(Badge, { variant: 'outline', children: `${p.side} ${p.lev}x` })
                      ]}),
                      jsxs('div', { className: `${ID}-meta`, children: [
                        jsx('span', { children: `${p.size} @ ${p.entry_px}` }),
                        jsx('span', { children: `mark ${p.mark_px}` }),
                        p.liq_pct != null ? jsx('span', { children: `liq -${p.liq_pct}% (${p.liq_px})` }) : null,
                        p.conviction != null ? jsx('span', { children: `conviction ${p.conviction}` }) : null,
                        jsx('span', { children: `margin $${p.margin_used}` })
                      ]})
                    ]}),
                    jsx('span', { className: 'text-sm font-semibold', style: { color: (p.upnl || 0) >= 0 ? 'var(--ui-green, #46a758)' : 'var(--ui-red, #e5484d)' }, children: `${(p.upnl || 0) >= 0 ? '+' : ''}$${p.upnl} (${(p.upnl_pct || 0) >= 0 ? '+' : ''}${p.upnl_pct}%)` })
                  ] }, `pos${p.coin}`)) }),
              spot.length ? jsxs('div', { className: `${ID}-section`, children: ['Spot', jsx('span', { className: `${ID}-sectioncount`, children: spot.length })] }) : null,
              spot.length ? jsx('div', { className: `${ID}-chips`, style: { padding: '0 1rem .5rem' }, children: spot.map(b => jsx('span', { className: `${ID}-chip`, children: `${b.coin} · ${b.total}${b.usd != null ? ` ($${b.usd})` : ''}` }, b.coin)) }) : null,
              fills.length ? jsxs('div', { className: `${ID}-section`, children: ['Recent fills', jsx('span', { className: `${ID}-sectioncount`, children: fills.length })] }) : null,
              fills.length ? jsx('div', { className: `${ID}-list`, children: fills.map((f, i) => jsxs('div', { className: `${ID}-row`, children: [
                jsx('div', { className: `${ID}-rowmain`, children: jsx('span', { className: 'text-sm text-(--ui-text-primary)', children: `${f.dir} ${f.sz} ${f.coin} @ ${f.px}` }) }),
                jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: f.time ? relTime(new Date(Number(f.time)).toISOString()) : '' })
              ] }, `fill${i}`)) }) : null
            ]})
      })
    })
  ]})
}

function AgentTab() {
  const q = useAgentHealth()
  const h = q.data || { signals: [], pins: [], kanban: {} }
  const refresh = () => queryClient.invalidateQueries({ queryKey: [ID, 'agent'] })
  const sigs = h.signals || []
  const fails = h.pins || []
  const kb = h.kanban || {}
  return jsxs('div', { className: `${ID}-page`, children: [
    jsxs('div', { className: `${ID}-tabs`, children: [
      jsx('span', { className: 'text-sm text-(--ui-text-primary)', children: 'Hermes agent health' }),
      h.as_of ? jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: `as of ${relTime(h.as_of)} ago` }) : null,
      jsx('span', { style: { flex: 1 } }),
      jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => refresh(), disabled: q.isFetching, children: q.isFetching ? 'Refreshing…' : 'Refresh' })
    ]}),
    jsx('div', { className: `${ID}-scrollwrap`, children:
      jsx(ScrollArea, { className: 'h-full', children:
        q.isLoading
          ? jsx('div', { className: 'grid h-full place-items-center p-4', children: jsx(GlyphSpinner, {}) })
          : jsxs('div', { children: [
              jsxs('div', { className: `${ID}-list`, children: sigs.map(s => jsxs('div', { className: `${ID}-row`, children: [
                jsx('span', { className: `${ID}-dotbtn`, title: s.label, style: { background: LEVEL_COLOR[s.level] || 'var(--ui-text-quaternary)' } }),
                jsx('div', { className: `${ID}-rowmain`, children: [
                  jsx('span', { className: 'text-sm text-(--ui-text-primary)', children: s.label }),
                  jsx('div', { className: `${ID}-meta`, children: jsx('span', { children: s.detail }) })
                ]})
              ] }, `sig${s.id}`)) }),
              fails.length ? jsxs('div', { className: `${ID}-section`, children: ['Open signals', jsx('span', { className: `${ID}-sectioncount`, children: fails.length })] }) : null,
              fails.length ? jsx('div', { className: `${ID}-list`, children: fails.map((f, i) => jsxs('div', { className: `${ID}-row`, children: [
                jsx('div', { className: `${ID}-rowmain`, children: [
                  jsx('span', { className: 'text-sm text-(--ui-text-primary)', children: f.title }),
                  jsx('div', { className: `${ID}-meta`, children: jsx('span', { children: f.ts ? relTime(f.ts) : '' }) })
                ]})
              ] }, `pin${i}`)) }) : null,
              kb.latest ? jsxs('div', { className: `${ID}-section`, children: ['Kanban board'] }) : null,
              kb.latest ? jsxs('div', { className: `${ID}-card`, children: [
                jsx('span', { className: 'text-sm text-(--ui-text-primary)', children: `Running: ${kb.latest.title}` }),
                jsx('div', { className: `${ID}-meta`, children: [
                  jsx('span', { children: `assignee ${kb.latest.assignee || '—'}` }),
                  jsx('span', { children: `status ${kb.latest.status}` })
                ]}),
                kb.counts ? jsx('div', { className: `${ID}-meta`, children: Object.entries(kb.counts).map(([k, v]) => jsx('span', { children: `${k}:${v}` }, k)) }) : null
              ]}) : null
            ]})
      })
    })
  ]})
}

// ─────────────────────────────────────────────────────────────────────────
// Page — Sources
// ─────────────────────────────────────────────────────────────────────────

function AddSourceCard({ onAdded, autofocus }) {
  const [url, setUrl] = useState('')
  const [candidates, setCandidates] = useState(null)
  const [picked, setPicked] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef(null)

  useEffect(() => {
    if (autofocus) {
      inputRef.current?.focus()
      addFocusArmed = false
    }
  }, [autofocus])

  // One input, two paths, routed by the SERVER (/search dual-routes:
  // URL-ish query -> classic discovery; topic -> Feedly's public index).
  const looksLikeUrl = /^https?:\/\//i.test(url.trim()) || /^[\w.-]+\.[a-z]{2,}(\/|$|:)/i.test(url.trim())
  const search = async () => {
    setError(''); setCandidates(null); setBusy(true)
    try {
      // Telegram channel: paste https://t.me/<user> or t.me/s/<user> → instant add.
      if (/^https?:\/\/(t\.me|telegram\.me)\//i.test(url.trim())) {
        await rest('/sources', { method: 'POST', body: { kind: 'telegram', feed_url: url.trim() } })
        setUrl(''); onAdded(); return
      }
      const out = await rest('/search', { method: 'POST', body: { query: url.trim() } })
      let cands = out.results || []
      if (out.via === 'discovery') cands = cands.filter(c => c.is_feed)
      if (out.via === 'feedly') cands = cands.filter(c => c.feed_url)
      setCandidates(cands)
      setPicked(0)
      if (!cands.length) setError(looksLikeUrl ? 'No feed found at that URL.' : `No feeds found for “${url.trim()}”.`)
    } catch (e) {
      setError(errText(e))
    } finally { setBusy(false) }
  }

  const add = async (feedUrl) => {
    setBusy(true); setError('')
    try {
      const isTg = /^https?:\/\/(t\.me|telegram\.me)\//i.test(feedUrl || '')
      await rest('/sources', {
        method: 'POST',
        body: isTg
          ? { kind: 'telegram', feed_url: feedUrl }
          : { url: looksLikeUrl ? url.trim() : '', feed_url: feedUrl }
      })
      setUrl(''); setCandidates(null)
      onAdded()
    } catch (e) {
      setError(errText(e))
    } finally { setBusy(false) }
  }

  return jsxs('div', { className: `${ID}-card`, children: [
    jsx('div', { className: `${ID}-setlabel`, style: { fontSize: '0.8125rem', color: 'var(--ui-text-primary)', fontWeight: 600 }, children: 'Add a source — search by topic or paste a URL' }),
    jsxs('div', { style: { display: 'flex', gap: '0.5rem' }, children: [
      jsx(Input, {
        value: url,
        ref: inputRef,
        onChange: e => setUrl(typeof e === 'string' ? e : e?.target?.value ?? ''),
        onKeyDown: e => { if (e.key === 'Enter' && url.trim() && !busy) void search() },
        placeholder: 'Search topics (“ai news”) or paste a site / feed URL',
        style: { flex: 1 }
      }),
      jsx(Button, { size: 'sm', onClick: () => void search(), disabled: busy || !url.trim(), children: busy ? 'Working…' : (looksLikeUrl ? 'Find feed' : 'Search') })
    ]}),
    error ? jsx('div', { className: `${ID}-err`, children: error }) : null,
    candidates && candidates.length > 0 ? jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: '0.375rem' }, children: [
      candidates.map((c, i) => {
        const feedUrl = c.feed_url || c.url
        return jsxs('button', {
          className: `${ID}-cand`,
          'data-picked': picked === i ? '1' : '0',
          onClick: () => setPicked(i),
          children: [
            jsx('span', { children: picked === i ? '◉' : '○' }),
            jsx('span', { children: c.title || feedUrl }),
            c.format ? jsx(Badge, { variant: 'outline', children: c.format }) : null,
            c.subscribers ? jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: `${Intl.NumberFormat().format(c.subscribers)} subs` }) : null,
            jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: feedUrl })
          ]
        }, feedUrl)
      }),
      jsx('div', { children: jsx(Button, {
        size: 'sm',
        onClick: () => void add(candidates[picked]?.feed_url || candidates[picked]?.url),
        disabled: busy,
        children: `Add “${candidates[picked]?.title || 'feed'}”`
      }) })
    ]}) : null
  ]})
}

function EditSourceDialog({ s, open, onOpenChange, onSaved }) {
  const [name, setName] = useState(s.name)
  const [category, setCategory] = useState(s.category || '')
  const [interval, setInterval_] = useState(String(s.refresh_interval ?? ''))
  const [error, setError] = useState('')
  const save = useMutation({
    mutationFn: () => rest(`/sources/${s.id}`, {
      method: 'PATCH',
      body: {
        name: name.trim(),
        category: category.trim(),
        ...(interval === '' ? {} : { refresh_interval: Number(interval) })
      }
    }),
    onSuccess: () => { onSaved(); onOpenChange(false) },
    onError: e => setError(errText(e))
  })
  return jsxs(Dialog, { open, onOpenChange, children: [
    jsx(DialogContent, { style: { maxWidth: '32rem' }, children: [
      jsxs(DialogHeader, { children: [
        jsx(DialogTitle, { children: 'Edit source' }),
        jsx(DialogDescription, { children: s.feed_url })
      ]}),
      jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: '0.75rem', paddingTop: '0.5rem' }, children: [
        jsxs('label', { className: `${ID}-setlabel`, style: { flexDirection: 'column', alignItems: 'stretch', gap: '0.25rem' }, children: [
          'Name',
          jsx(Input, { value: name, onChange: e => setName(typeof e === 'string' ? e : e?.target?.value ?? '') })
        ]}),
        jsxs('label', { className: `${ID}-setlabel`, style: { flexDirection: 'column', alignItems: 'stretch', gap: '0.25rem' }, children: [
          'Category',
          jsx(Input, { value: category, onChange: e => setCategory(typeof e === 'string' ? e : e?.target?.value ?? ''), placeholder: 'technology, science…' })
        ]}),
        jsxs('label', { className: `${ID}-setlabel`, style: { flexDirection: 'column', alignItems: 'stretch', gap: '0.25rem' }, children: [
          'Refresh interval (seconds, empty = default)',
          jsx(Input, { value: interval, onChange: e => setInterval_(typeof e === 'string' ? e : e?.target?.value ?? ''), placeholder: '300' })
        ]}),
        error ? jsx('div', { className: `${ID}-err`, children: error }) : null
      ]}),
      jsxs(DialogFooter, { children: [
        jsx(Button, { variant: 'ghost', size: 'sm', onClick: () => onOpenChange(false), children: 'Cancel' }),
        jsx(Button, { size: 'sm', disabled: save.isPending, onClick: () => save.mutate(), children: save.isPending ? 'Saving…' : 'Save' })
      ]})
    ]})
  ]})
}

function SourceRow({ s, onChanged }) {
  const [armed, setArmed] = useState(false)
  const [editing, setEditing] = useState(false)
  return jsxs('div', { className: `${ID}-srcrow`, children: [
    jsxs('div', { style: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '0.125rem' }, children: [
      jsxs('span', { className: 'text-sm text-(--ui-text-primary)', children: [
        s.name,
        s.kind === 'telegram' ? jsx(Badge, { variant: 'outline', children: 'telegram' }) : null,
        s.enabled ? null : jsx(Badge, { variant: 'outline', children: 'disabled' }),
        s.category ? jsx(Badge, { variant: 'outline', children: s.category }) : null
      ]}),
      jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: s.feed_url }),
      s.last_error
        ? jsxs('span', { className: `${ID}-err`, title: `last ok: ${s.last_success_at || 'never'}`, children: ['⚠ ', s.last_error, s.error_count > 1 ? ` (${s.error_count}×)` : ''] })
        : s.last_checked_at
          ? jsxs('span', { className: 'text-xs text-(--ui-text-quaternary)', children: [s.last_success_at ? 'ok' : 'never succeeded', ' · checked ', relTime(s.last_checked_at), ' ago'] })
          : jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: 'not checked yet' })
    ]}),
    jsx(Switch, {
      size: 'xs',
      checked: !!s.enabled,
      onCheckedChange: async v => {
        try {
          await rest(`/sources/${s.id}`, { method: 'PATCH', body: { enabled: v } })
          onChanged()
        } catch { /* refetch surfaces */ }
      }
    }),
    jsx(Button, {
      size: 'xs', variant: 'ghost',
      onClick: async () => {
        try { await rest(`/sources/${s.id}/refresh`, { method: 'POST', body: {} }); onChanged() } catch { /* surfaced */ }
      },
      children: 'Refresh'
    }),
    jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setEditing(true), children: 'Edit' }),
    jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setArmed(true), children: 'Delete' }),
    jsx(ConfirmDialog, {
      open: armed,
      onClose: () => setArmed(false),
      title: `Delete “${s.name}”?`,
      description: `Removes the source and its ${s.article_count ?? 0} cached article${s.article_count === 1 ? '' : 's'}.`,
      confirmLabel: 'Delete',
      destructive: true,
      onConfirm: async () => {
        try { await rest(`/sources/${s.id}`, { method: 'DELETE' }); onChanged() } catch { /* surfaced */ }
      }
    }),
    editing ? jsx(EditSourceDialog, { s, open: editing, onOpenChange: setEditing, onSaved: onChanged }) : null
  ]})
}

function SourcesTab({ sources, onChanged, autofocusAdd }) {
  const refreshAll = useMutation({
    mutationFn: () => rest('/refresh-all', { method: 'POST', body: {} }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [ID] })
  })
  const importRef = useRef(null)
  const [importBusy, setImportBusy] = useState(false)
  const [importMsg, setImportMsg] = useState('')

  const doImport = async file => {
    setImportBusy(true); setImportMsg('')
    try {
      const xml = await file.text()
      const out = await rest('/opml/import', { method: 'POST', body: { xml } })
      const { added = [], skipped = [], errors = [] } = out || {}
      setImportMsg(`Imported ${added.length}, skipped ${skipped.length}${errors.length ? `, failed ${errors.length}` : ''}`)
      onChanged()
    } catch (e) {
      setImportMsg(errText(e))
    } finally {
      setImportBusy(false)
      if (importRef.current) importRef.current.value = ''
    }
  }

  return jsxs('div', { className: `${ID}-page`, children: [
    jsxs('div', { className: `${ID}-tabs`, children: [
      jsx('span', { className: 'text-sm text-(--ui-text-primary)', children: `Sources (${sources.length})` }),
      jsx('span', { style: { flex: 1 } }),
      importMsg ? jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: importMsg }) : null,
      jsx(Button, { size: 'xs', variant: 'ghost', disabled: importBusy, onClick: () => importRef.current?.click(), children: importBusy ? 'Importing…' : 'Import OPML' }),
      jsx('input', { ref: importRef, type: 'file', accept: '.opml,.xml,text/xml,text/x-opml', style: { display: 'none' }, onChange: e => { const f = e.target.files?.[0]; if (f) void doImport(f) } }),
      jsx(Button, {
        size: 'xs', variant: 'ghost',
        onClick: async () => {
          try {
            // JSON twin route: the plugin REST bridge (Electron fetchJson)
            // only resolves JSON bodies — the raw text/xml route rejects.
            const out = await rest('/opml/export.json')
            const xml = typeof out === 'string' ? out : out?.xml || ''
            if (!xml) throw new Error('empty OPML document')
            const blob = new Blob([xml], { type: 'text/x-opml' })
            const a = document.createElement('a')
            a.href = URL.createObjectURL(blob)
            a.download = 'newswire-sources.opml'
            a.click()
            URL.revokeObjectURL(a.href)
            setImportMsg(`Exported ${sources.length} source${sources.length === 1 ? '' : 's'}`)
          } catch (e) {
            host.notifyError(e, 'OPML export failed')
          }
        },
        children: 'Export OPML'
      }),
      jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => void refreshAll.mutate(), disabled: refreshAll.isPending, children: refreshAll.isPending ? 'Refreshing…' : 'Refresh all' })
    ]}),
    jsx('div', { className: `${ID}-scrollwrap`, children:
      jsx(ScrollArea, { className: 'h-full', children:
        jsxs('div', { children: [
          jsx(AddSourceCard, { onAdded: onChanged, autofocus: autofocusAdd }),
          sources.length === 0
            ? jsxs('div', { className: `${ID}-card`, children: [
                jsx('span', { className: `${ID}-setlabel`, style: { fontSize: '0.8125rem', color: 'var(--ui-text-primary)', fontWeight: 600 }, children: 'Starter feeds (optional)' }),
                jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Pick any to subscribe — nothing is added without your click.' }),
                jsx('div', { className: `${ID}-chips`, children: STARTER_FEEDS.map(f => jsxs('button', {
                  className: `${ID}-chip`,
                  onClick: async () => {
                    try {
                      await rest('/sources', { method: 'POST', body: { feed_url: f.feed_url, name: f.name, category: f.category } })
                      onChanged()
                    } catch { /* surfaced on refetch */ }
                  },
                  children: [`+ ${f.name}`, jsx(Badge, { variant: 'outline', children: f.category })]
                }, f.feed_url)) })
              ]})
            : jsx('div', { className: `${ID}-list`, children: sources.map(s => jsx(SourceRow, { s, key: s.id, onChanged })) })
        ]})
      })
    })
  ]})
}

// ─────────────────────────────────────────────────────────────────────────
// Page — Settings
// ─────────────────────────────────────────────────────────────────────────

function SettingsTab() {
  const [settingsQ, s] = useSettings()
  const [hlAddr, setHlAddr] = useState(s?.hl_address || '')
  const [watchText, setWatchText] = useState((s?.watchlist || []).join(', '))
  openArticleMode = s?.open_article_behavior === 'external' ? 'external' : 'internal'
  const save = useMutation({
    mutationFn: patch => rest('/settings', { method: 'PATCH', body: patch }),
    onSuccess: (_data, patch) => {
      queryClient.invalidateQueries({ queryKey: [ID, 'settings'] })
      // Pane registration follows ticker_enabled / ticker_font_size live.
      if (patch && ('ticker_enabled' in patch || 'ticker_font_size' in patch) && applyTickerSettingsFn) {
        applyTickerSettingsFn({ ...s, ...patch })
      }
    }
  })
  if (!s) return jsx('div', { className: 'grid h-full place-items-center p-4', children: jsx(GlyphSpinner, {}) })

  const Toggle = ({ label, k }) => jsxs('div', { className: `${ID}-setrow`, children: [
    jsx('span', { className: `${ID}-setlabel`, children: label }),
    jsx(Switch, { size: 'xs', checked: s[k] !== false, onCheckedChange: v => save.mutate({ [k]: v }) })
  ]})
  const Choice = ({ label, k, options }) => jsxs('div', { className: `${ID}-setrow`, children: [
    jsx('span', { className: `${ID}-setlabel`, children: label }),
    jsx(SegmentedControl, {
      value: String(s[k]),
      onChange: v => save.mutate({ [k]: Number(v) }),
      options
    })
  ]})

  return jsx('div', { className: `${ID}-page`, children:
    jsx('div', { className: `${ID}-scrollwrap`, children:
      jsx(ScrollArea, { className: 'h-full', children:
        jsxs('div', { className: `${ID}-card`, style: { maxWidth: '38rem' }, children: [
          jsx('div', { className: `${ID}-setgroup`, children: 'Ticker' }),
          jsx(Toggle, { label: 'Ticker enabled', k: 'ticker_enabled' }),
          jsx(Toggle, { label: 'Pause on hover', k: 'pause_on_hover' }),
          jsxs('div', { className: `${ID}-setrow`, children: [
            jsx('span', { className: `${ID}-setlabel`, children: 'Scroll speed' }),
            jsx(SegmentedControl, {
              value: s.ticker_speed || 'normal',
              onChange: v => save.mutate({ ticker_speed: v }),
              options: SPEED_OPTIONS
            })
          ]}),
          jsxs('div', { className: `${ID}-setrow`, children: [
            jsx('span', { className: `${ID}-setlabel`, children: 'Group headlines' }),
            jsx(SegmentedControl, {
              value: s.ticker_grouping || 'newest',
              onChange: v => save.mutate({ ticker_grouping: v }),
              options: [
                { id: 'newest', label: 'Newest' },
                { id: 'source', label: 'By source' },
                { id: 'unread_first', label: 'Unread first' }
              ]
            })
          ]}),
          jsxs('div', { className: `${ID}-setrow`, children: [
            jsx('span', { className: `${ID}-setlabel`, children: 'Text size (px)' }),
            jsx(SegmentedControl, {
              value: String(s.ticker_font_size || 11),
              onChange: v => save.mutate({ ticker_font_size: Number(v) }),
              options: FONT_OPTIONS
            })
          ]}),
          jsxs('div', { className: `${ID}-setrow`, children: [
            jsx('span', { className: `${ID}-setlabel`, children: 'Open articles in' }),
            jsx(SegmentedControl, {
              value: s.open_article_behavior || 'internal',
              onChange: v => save.mutate({ open_article_behavior: v }),
              options: [
                { id: 'internal', label: 'Hermes browser' },
                { id: 'external', label: 'External' }
              ]
            })
          ]}),
          jsx(Toggle, { label: 'Show source name', k: 'show_source' }),
          jsx(Toggle, { label: 'Show relative time', k: 'relative_time' }),
          jsx(Toggle, { label: 'Only show unread', k: 'only_unread' }),
          jsx(Separator, {}),
          jsx('div', { className: `${ID}-setgroup`, children: 'Engine' }),
          jsx(Choice, { label: 'Default refresh interval', k: 'refresh_interval', options: INTERVAL_OPTIONS }),
          jsx(Choice, { label: 'Max article age', k: 'max_article_age_hours', options: AGE_OPTIONS }),
          jsx(Choice, { label: 'Max stored headlines', k: 'max_headlines', options: LIMIT_OPTIONS }),
          jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Feeds are polled by the backend with ETag/Last-Modified caching; unchanged feeds are not re-downloaded. Per-source refresh overrides: Sources → Edit.' }),
          jsx(Separator, {}),
          jsx('div', { className: `${ID}-setgroup`, children: 'Signal lanes' }),
          jsx('div', { className: `${ID}-setrow`, children: ['news', 'trades', 'agent'].map(k =>
            jsxs('span', { key: k, style: { display: 'inline-flex', gap: '0.375rem', alignItems: 'center' }, children: [
              jsx(Switch, { size: 'xs', checked: (s.ticker_lanes || {})[k] !== false, onCheckedChange: v => save.mutate({ ticker_lanes: { ...(s.ticker_lanes || {}), [k]: v } }) }),
              jsx('span', { className: `${ID}-setlabel`, children: `${k} lane` })
            ]})
          )}),
          jsx(Separator, {}),
          jsx('div', { className: `${ID}-setgroup`, children: 'Signals' }),
          jsx('div', { className: `${ID}-setrow`, children: [
            jsx('span', { className: `${ID}-setlabel`, children: 'Watchlist keywords (comma-separated)' }),
            jsx('span', { style: { display: 'flex', gap: '0.5rem', alignItems: 'center' }, children: [
              jsx(Input, { value: watchText, onChange: e => setWatchText(typeof e === 'string' ? e : e?.target?.value ?? ''), placeholder: 'HYPE, BTC, ETH, SOL', style: { width: '12rem' } }),
              jsx(Button, { size: 'xs', variant: 'outline', onClick: () => save.mutate({ watchlist: watchText.split(',').map(x => x.trim().toUpperCase()).filter(Boolean) }), children: 'Save' })
            ]})
          ]}),
          jsx(Toggle, { label: 'Notify on watchlist match', k: 'notify_on_watch' }),
          jsx('div', { className: `${ID}-setrow`, children: [
            jsx('span', { className: `${ID}-setlabel`, children: 'Hyperliquid address (public reads only)' }),
            jsx('span', { style: { display: 'flex', gap: '0.5rem', alignItems: 'center' }, children: [
              jsx(Input, { value: hlAddr, onChange: e => setHlAddr(typeof e === 'string' ? e : e?.target?.value ?? ''), placeholder: '0x…', style: { width: '15rem' } }),
              jsx(Button, { size: 'xs', variant: 'outline', onClick: () => save.mutate({ hl_address: hlAddr.trim() }), children: 'Save' })
            ]})
          ]}),
          jsx(Choice, { label: 'Trade poll interval', k: 'hl_poll_interval', options: [{ id: '15', label: '15s' }, { id: '30', label: '30s' }, { id: '60', label: '60s' }, { id: '300', label: '5m' }] }),
          jsx(Toggle, { label: 'Pin high-severity signals (pins cluster)', k: 'pins_enabled' })
        ]})
      })
    })
  })
}

// ─────────────────────────────────────────────────────────────────────────
// Page shell
// ─────────────────────────────────────────────────────────────────────────

function NewswirePage() {
  const [tab, setTab] = useState(() => storageGet?.('lastTab', 'latest') || 'latest')
  const [prefs, setPrefsRaw] = useState(() => storageGet?.('latestPrefs', {}) || {})
  const addSignal = useValue($addSourceSignal)
  const pinTab = useValue($pinTab)
  const addArmed = useRef(0)
  const [, sources] = useSources()
  const onChanged = () => queryClient.invalidateQueries({ queryKey: [ID] })

  const setPrefs = p => setPrefsRaw(p)
  useEffect(() => { storageSet?.('latestPrefs', prefs) }, [prefs]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { storageSet?.('lastTab', tab) }, [tab]) // eslint-disable-line react-hooks/exhaustive-deps

  // Palette "Add Source" → land on Sources tab with the add card focused.
  useEffect(() => {
    if (addSignal > addArmed.current) {
      addArmed.current = addSignal
      setTab('sources')
    }
  }, [addSignal])

  // Pin-chip click → open the tab that explains the pin.
  useEffect(() => {
    if (pinTab) {
      setTab(pinTab)
      $pinTab.set(null)
    }
  }, [pinTab])

  const tabBtn = (id, label) => jsx('button', {
    className: `${ID}-tab`, role: 'tab',
    'aria-selected': tab === id ? 'true' : 'false',
    'data-active': tab === id ? '1' : '0',
    onClick: () => setTab(id),
    children: label
  }, id)

  return jsxs('div', { className: `${ID}-page`, children: [
    jsxs('div', { className: `${ID}-tabs`, role: 'tablist', 'aria-label': 'Newswire sections', children: [
      jsx('span', { style: { fontWeight: 700, fontSize: '0.75rem', letterSpacing: '0.08em', color: 'var(--ui-accent)' }, children: 'NEWSWIRE' }),
      tabBtn('latest', 'Latest'),
      tabBtn('watchlist', 'Watchlist'),
      tabBtn('trades', 'Trades'),
      tabBtn('agent', 'Agent'),
      tabBtn('sources', `Sources${sources.length ? ` (${sources.length})` : ''}`),
      tabBtn('settings', 'Settings')
    ]}),
    tab === 'latest' ? jsx(LatestTab, { sources, prefs, setPrefs }) :
    tab === 'watchlist' ? jsx(WatchlistTab, { sources, prefs, setPrefs }) :
    tab === 'trades' ? jsx(TradesTab, {}) :
    tab === 'agent' ? jsx(AgentTab, {}) :
    tab === 'sources' ? jsx(SourcesTab, { sources, onChanged, autofocusAdd: addFocusArmed }) :
    jsx(SettingsTab, {})
  ]})
}

// ─────────────────────────────────────────────────────────────────────────
// Plugin registration
// ─────────────────────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'Hermes Newswire',
  description: 'Breaking-news ticker in the statusbar + full newswire page. RSS/Atom/JSON Feed, no API keys, no model usage.',
  defaultEnabled: true,

  register(ctx) {
    ensureStyles()
    rest = ctx.rest
    openExternalFn = ctx.os?.openExternal
      ? url => ctx.os.openExternal(url)
      : async () => false
    storageGet = (key, fallback) => ctx.storage.get(key, fallback)
    storageSet = (key, value) => ctx.storage.set(key, value)

    // Ticker pane registration is synced to the ticker_enabled setting:
    // registered = the strip exists in the layout (a thin row above the
    // statusbar); unregistered = the layout row is gone entirely. This
    // avoids a dead blank strip when the user disables the ticker, while
    // keeping the dock hint so re-enabling re-adopts the same spot.
    // Height follows the font-size setting (readability knob): re-register
    // on change — the pane system sizes a single-pane zone by its declared
    // height at adoption, and registerMany/register replaces same-id
    // contributions cleanly.
    const makeTickerPane = heightPx => ({
      id: 'ticker',
      // Thin persistent strip ABOVE the statusbar: a real layout split on
      // the workspace's bottom edge (pane system), NOT a statusbar row —
      // Tony: statusbar placement covered the app's own items. placement
      // 'main' + headerVeto: the strip is not a tab-able surface (like
      // full-page views), so the zone renders headerless and the whole
      // track is content. A single-pane zone declaring height is a fixed
      // track (same rule as the terminal deck).
      area: 'panes',
      title: 'Newswire',
      data: {
        placement: 'main',
        headerVeto: true,
        dock: { pane: 'workspace', pos: 'bottom' },
        height: `${heightPx}px`
      },
      render: () => jsx(NewswireTicker, {})
    })
    let tickerRegistered = false
    let disposeTicker = null
    let lastPaneKey = null
    const applyTickerSettings = settings => {
      const enabled = !settings || settings.ticker_enabled !== false
      const fontPx = Math.min(20, Math.max(9, Number(settings?.ticker_font_size) || 11))
      const heightPx = fontToHeight(fontPx)
      // Guard: only touch the registry when the pane's shape actually
      // changes. Re-registering unconditionally makes the pane remount,
      // which re-runs the caller's effect, which re-registers — the
      // React #185 loop. Same key = no-op.
      const key = `${enabled}|${heightPx}`
      if (key === lastPaneKey) return
      lastPaneKey = key
      if (!enabled) {
        if (tickerRegistered && disposeTicker) {
          disposeTicker()
          disposeTicker = null
          tickerRegistered = false
        }
        return
      }
      if (tickerRegistered && disposeTicker) {
        disposeTicker()
      }
      // (Re)register with the current height — same id, clean replace.
      disposeTicker = ctx.register(makeTickerPane(heightPx))
      tickerRegistered = true
    }

    // Initial state from the backend; keep in sync as settings change.
    applyTickerSettingsFn = applyTickerSettings
    void rest('/settings')
      .then(out => applyTickerSettings(out?.settings))
      .catch(() => applyTickerSettings(null))
    ctx.onDispose(() => { if (disposeTicker) disposeTicker() })

    const dispose = ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PAGE_PATH },
        render: () => jsx(NewswirePage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 60,
        data: { codicon: 'radio-tower', label: 'Newswire', path: PAGE_PATH }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: `${ID}.open`,
          label: 'Newswire: Open',
          keywords: ['newswire', 'news', 'ticker'],
          run: () => host.navigate(PAGE_PATH)
        }
      },
      {
        id: 'refresh',
        area: PALETTE_AREA,
        data: {
          id: `${ID}.refresh`,
          label: 'Newswire: Refresh Now',
          keywords: ['newswire', 'refresh', 'news'],
          run: () => void rest('/refresh-all', { method: 'POST', body: {} })
            .then(() => queryClient.invalidateQueries({ queryKey: [ID] }))
            .catch(() => {})
        }
      },
      {
        id: 'pause',
        area: PALETTE_AREA,
        data: {
          id: `${ID}.pause`,
          label: 'Newswire: Pause Ticker',
          keywords: ['newswire', 'pause', 'ticker'],
          run: () => $tickerPaused.set(true)
        }
      },
      {
        id: 'resume',
        area: PALETTE_AREA,
        data: {
          id: `${ID}.resume`,
          label: 'Newswire: Resume Ticker',
          keywords: ['newswire', 'resume', 'ticker'],
          run: () => $tickerPaused.set(false)
        }
      },
      {
        id: 'addSource',
        area: PALETTE_AREA,
        data: {
          id: `${ID}.addSource`,
          label: 'Newswire: Add Source',
          keywords: ['newswire', 'add', 'source', 'feed', 'rss'],
          run: () => {
            addFocusArmed = true
            $addSourceSignal.set($addSourceSignal.get() + 1)
            host.navigate(PAGE_PATH)
          }
        }
      },
      {
        id: 'askLast',
        area: PALETTE_AREA,
        data: {
          id: `${ID}.askLast`,
          label: 'Newswire: Ask Hermes About Last Item',
          keywords: ['newswire', 'ask', 'hermes', 'headline', 'position'],
          run: () => {
            const it = $lastItem.get()
            if (!it) {
              host.notify({ kind: 'info', message: 'Click a Newswire headline (or position) first, then run this again.' })
              return
            }
            void askHermes(it)
          }
        }
      }
    ])

    return dispose
  }
}

// v-shade: contrast fix marker
