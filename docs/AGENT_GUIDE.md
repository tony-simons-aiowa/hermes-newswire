# Hermes Newswire — Agent Guide

Deep architecture and safety reference for coding agents working on this
repo. Quick operating rules: root [`AGENTS.md`](../AGENTS.md). Everything
below is based on the actual code; where a name appears, it is the real
symbol in `dashboard/plugin_api.py` or `desktop/plugin.js`.

## System overview

```
feed URL / topic query
  → /search (Feedly public index, or discovery)   [backend]
  → _http_fetch (SSRF gate, redirects, caps)      [backend]
  → parse_feed (RSS 2.0 / RDF / Atom / JSON Feed) [backend]
  → strip_html sanitization                        [backend]
  → _insert_articles (dedup ladder)                [backend]
  → SQLite (<HERMES_HOME>/state/newswire/newswire.db)
  → FastAPI routes (@router.get/post...)           [backend, /api/plugins/hermes-newswire/]
  → ctx.rest JSON                                  [renderer]
  → ticker strip + /newswire page                  [renderer]
```

The renderer never talks to the network for feeds; it polls the backend
cache (React Query) and renders normalized plain text.

## Backend guide (`dashboard/plugin_api.py`)

- **Feed formats**: RSS 2.0, RSS 1.0/RDF, Atom, JSON Feed — `parse_feed`
  dispatches on content; `looks_like_feed` sniffs bodies; dates normalized
  to UTC ISO from both RFC 822 and ISO 8601.
- **Discovery** (`discover_in_html`): `<link rel="alternate">` with feed
  types first, then well-known paths (`/feed`, `/rss.xml`, …), each probed
  and verified before being returned as a candidate.
- **Search** (`/search`): topic queries hit Feedly's public
  `/v3/search/feeds` (no key); URL-ish queries route to discovery. Every
  candidate feed URL is re-validated through `_assert_public_http_url`
  before reaching the client.
- **Storage**: SQLite, WAL, `check_same_thread=False`, connection-per-use
  via `_db()`. Tables: `sources` (incl. `etag`, `last_modified`,
  `error_count`, `favicon_url`, `articles_ever`), `articles` (UNIQUE
  `(source_id, guid)` + UNIQUE `hash`), `settings` (key/JSON).
- **Dedup ladder** (`_insert_articles`): feed GUID → canonical URL →
  normalized URL → normalized title → global content hash.
- **Refresh**: background `_refresher_loop` (lifespan-managed task,
  per-source intervals, `due` computed from `last_checked_at`);
  `/refresh-all` forces. Conditional GETs via `_conditional_headers`; 304
  fast path with two guards: never-parsed self-heal (0 articles AND
  `articles_ever=0` → drop validators, refetch) vs retention-pruned
  (articles_ever>0, 0 current → keep validators + explanatory
  `last_error`; no re-fetch churn).
- **Retention** (`_apply_retention`): deletes articles older than
  `max_article_age_hours`; caps total rows at `max_headlines`.
- **Favicons** (`_favicon_for_source`): stored Feedly `icon_url` (validated
  public http(s)) else Google s2 over the SITE domain — `feeds.`/`rss.`/
  `www.` prefixes stripped so the brand shows, not the feed host.
- **Error isolation**: every fetch/parse failure is captured on the source
  row (`last_error`, `error_count`); sources are NEVER auto-disabled.
- **Routes**: sources CRUD (+ per-source refresh), `/articles` (filters:
  source, unread, `include_disabled_sources` opt-in; disabled sources'
  articles are hidden by default), `/settings` (validated by
  `_validate_setting`), `/refresh-all`, `/discover`, `/search`,
  `/opml/export(.json)`, `/opml/import`, `/preview` (emits the app's
  `preview.open` gateway event for in-app reading), `/health`, `/state`.

### Security controls — why they exist

Feed URLs, redirect targets, search results, icon URLs, and OPML entries
are **attacker-controlled network input**. A malicious feed can attempt
SSRF (point at localhost/cloud metadata), oversized bodies, redirect
chains, and HTML/script injection. Accordingly:

- `_assert_public_http_url` (+ `_resolve_host_sync`, `_ip_is_blocked`,
  legacy hex/octal IP forms) — the SSRF gate. Every URL and EVERY redirect
  hop passes it. Never bypass, never "just this once" fetch a raw URL.
- `_http_fetch` caps: `MAX_REDIRECTS=3`, `CONNECT_TIMEOUT=5s`,
  `TOTAL_TIMEOUT=15s`, `MAX_BODY_BYTES=5MB` (streaming, aborted mid-body).
- `strip_html` removes all markup before storage; the renderer renders
  plain text only. Do not add rich HTML rendering of feed content.
- Policy rejections surface as `400 unsafe_url` (client error), distinct
  from upstream `502 fetch_failed`.
- Any change touching fetch/redirect/validation/sanitization logic
  **requires new adversarial tests** (see `tests/test_http.py`,
  `tests/test_search.py` for the pattern: monkeypatch `_resolve_host_sync`
  / `_http_fetch`, assert the gate).

## Desktop guide (`desktop/plugin.js`)

Plain ESM, loaded uncompiled by the Hermes Desktop runtime loader. Only
`@hermes/plugin-sdk`, `react`, `react/jsx-runtime` resolve; UI is `jsx()`
calls, never JSX syntax. Hot-reloads on save; `node --check` is the syntax
gate.

- **Ticker strip**: `panes` contribution — `placement: 'main'`,
  `headerVeto: true`, `dock: { pane: 'workspace', pos: 'bottom' }`,
  height follows the font-size setting (a single-pane zone declaring
  height is a fixed track). Marquee (duplicated track, `-50%` keyframes),
  hover-pause, reduced-motion static rotation, focusable headline buttons.
  **It is a layout split, never an overlay — it must not cover the
  statusbar.**
- **Registration sync**: `applyTickerSettings` registers/unregisters the
  pane for `ticker_enabled`/font changes, guarded by `lastPaneKey` —
  re-registering unconditionally makes the pane remount → its effect
  re-registers → React #185 infinite loop. Keep the guard.
- **Page** (`/newswire`, ROUTES_AREA + sidebar nav + 5 palette commands):
  `LatestTab` (search, source filter, grouping picker, read state,
  pagination), `SourcesTab` (add via topic search or URL discovery,
  enable/disable, edit, delete, OPML), `SettingsTab`.
- **Grouping** (`groupTickerArticles` + page sections): newest | by source
  (contiguous blocks, dividers) | unread first. Pure client transform.
- **Article opening** (`openArticle`): default internal — POST `/preview`
  (backend emits `preview.open`) with external-browser fallback;
  `open_article_behavior` setting flips it.
- **Styling**: scoped `<style>` injected with content comparison
  (`ensureStyles`), theme vars only (`--ui-*`) — never hardcoded colors.
  Native `<select>` options must set explicit theme-solid backgrounds
  (dark popup; `--ui-bg-elevated` for the closed control —
  `--ui-bg-input` is a LIGHT field color in this theme system).
- **Favicons**: eager `<img loading>` removed — lazy images never load
  inside a moving marquee.

## Testing

- `tests/` — pytest, **fixtures only, no live network**. The seam is
  `_http_fetch` (scripted `FakeFetch`) and `_resolve_host_sync`
  (monkeypatched DNS). `plugin`/`client` fixtures build a FastAPI
  TestClient against a temp `HERMES_HOME`.
- `tests/ui/esm-render.mjs` — loads the real `plugin.js` as ESM with SDK
  stubs (`.stubs/`), asserts registration + ticker structure (26 checks).
- Never add live-site tests; simulate HTTP outcomes instead. New behavior
  gets a red-proven regression test; security changes get adversarial
  cases (private IPs, hex-octal forms, redirect-to-internal, oversized
  bodies).
- macOS note: if the venv lacks pytest, use an ephemeral overlay
  (`uv run --python <venv>/bin/python --with pytest -m pytest ...`) —
  never install into the Hermes venv.

## Safe change recipes

**Add a setting** — `DEFAULT_SETTINGS` + `_validate_setting` (backend),
`SettingsTab` row + any consumer (renderer); test: defaults + validation
bounds + round-trip. If it affects pane shape (enabled/font), route through
`applyTickerSettings` and keep the `lastPaneKey` guard.

**Add a backend route** — new `@router` handler; validate all URL inputs
through `_assert_public_http_url`; test with `FakeFetch` fixtures.

**Change ticker behavior** — `NewswireTicker`/`TickerItem`/styles; run
`node --check` + `node tests/ui/esm-render.mjs`; verify in a live session
(CDP probe or eyes) that the statusbar stays uncovered and hover-pause
works.

**Add a feed format** — extend `parse_feed` dispatch + a parser fn; add
fixture XML/JSON in `tests/`; assert normalization (dates → UTC ISO,
summaries stripped).

**Modify database schema** — additive `ALTER TABLE` in `_db()` guarded by
`PRAGMA table_info`; backfill in the same migration block (see
`articles_ever`); test migration from a pre-column database snapshot.

## Publishing notes

- Commits: author email must be registered on the maintainer's GitHub
  account or contributions won't link.
- The Hermes plugin catalog pins exact SHAs; after merges that should ship
  to the catalog, the pin entry (upstream `plugin-catalog/*.yaml`) needs a
  bump PR.
- `hermes plugins validate .` must stay green — it gates catalog admission.
