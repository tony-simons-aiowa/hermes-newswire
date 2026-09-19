# Hermes Newswire

[![](https://img.shields.io/badge/X-%40tonysimons_-1DA1F2?style=for-the-badge&logo=x&logoColor=white)](https://x.com/tonysimons_)
[![Support the Project](https://img.shields.io/badge/Support_the_Project-X%20Money-black?style=for-the-badge&logo=x&logoColor=white)](https://x.com/tonysimons_)
[![](https://img.shields.io/badge/tonysimons.dev-111827?style=for-the-badge&logo=googlechrome&logoColor=white)](https://tonysimons.dev)
[![License: MIT](https://img.shields.io/badge/license-MIT-6c63ff?style=for-the-badge)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-102%20passed-brightgreen?style=for-the-badge)](#test--verify)
[![Zero API keys](https://img.shields.io/badge/zero-API%20keys-00d26a?style=for-the-badge)](#security)
[![No LLM tokens](https://img.shields.io/badge/routine%20ops-no%20model%20tokens-00d26a?style=for-the-badge)](#security)

A breaking-news ticker plugin for [Hermes Desktop](https://hermes-agent.nousresearch.com) — a thin, continuously scrolling newswire strip docked just above the statusbar, backed by a model-free RSS/Atom/JSON-Feed engine.

```
NEWSWIRE ◆ Hacker News: Why is Google still serving dodgy ads? · 56m ◆ The Verge: Apple is reportedly working on… · 14m ◆ …
```

## What it is

- **Bottom ticker strip** — a 28–40px pane docked to the workspace's bottom edge (above the statusbar, never covering it). Continuous marquee, hover-pause, clickable headlines that open in your default browser, source + relative age per story, reduced-motion fallback (static rotating headline), keyboard accessible.
- **Newswire page** (`/newswire`, sidebar row, ⌘K commands) — Latest list with search/filter/unread/mark-read, Sources management with feed discovery (paste `https://www.theverge.com`, get its feed), Settings.
- **Zero LLM usage** — feed retrieval, parsing, dedup, storage, and rendering consume no model tokens and need no API keys.
- **Offline-friendly** — SQLite cache keeps the ticker alive when the network is down; refresh resumes silently on reconnect.

## Install (unified plugin)

```bash
git clone https://github.com/tony-simons-aiowa/hermes-newswire ~/.hermes/plugins/hermes-newswire/
# enable the backend (plugins.enabled must be a real YAML list):
#   edit ~/.hermes/config.yaml → plugins: enabled: [- hermes-newswire]
# restart the desktop's serve child (or restart Hermes Desktop) so the
# backend mounts; the renderer half is lifted automatically:
#   ~/.hermes/desktop-plugins/hermes-newswire/plugin.js
```

Verify: `Mounted plugin API routes: /api/plugins/hermes-newswire/` in `~/.hermes/logs/agent.log`.

## Layout

```
dashboard/          backend  (FastAPI router → /api/plugins/hermes-newswire/)
  manifest.json     name/label/version/api pointer
  plugin_api.py     feed engine, SQLite store, refresh loop, routes
desktop/            renderer (plain ESM plugin.js, loaded uncompiled)
tests/              pytest suite (fixtures only — no live sites)
```

## Supported formats & discovery

RSS 2.0, RSS 1.0/RDF, Atom, JSON Feed. Discovery: `<link rel="alternate">` tags first, then well-known paths (`/feed`, `/rss.xml`, `/atom.xml`, …). Direct feed URLs skip discovery.

## Security

Source URLs are untrusted: http/https only (no file/ftp), loopback/private/link-local/CGNAT/metadata addresses blocked (literal, hex/octal legacy forms, AND post-DNS resolution), redirects validated per hop (max 3), 5s connect / 15s total timeouts, 5 MB body cap, all feed HTML stripped before storage. OPML import validates every URL through the same gates. No telemetry, no remote service, no secrets.

## Settings

Ticker enabled · scroll speed · **text size 9–20px** (strip height follows) · pause on hover · show source · relative time · only-unread · max article age · max headlines · refresh interval (30s–24h, default 5min). Conditional GETs (ETag/Last-Modified → 304) keep polling cheap.

## Signal lanes (2026-09-17 desktop build)

The ticker is now a **three-lane strip**: news · trades · agent. Lane toggles, watchlist, HL address, notification and pin settings live in Settings → Signal lanes / Signals.

- **Trades lane** — live Hyperliquid snapshot via the public info API (zero keys): open positions with uPnL/lev/liq-distance/conviction, or *Flat · acct $X · last fill* when flat. Handles the HIP-3 builder clearinghouses (`perpDexs` merge) so `xyz:`/`flx:`/etc. positions never vanish; conviction overlay from `~/.hermes/hl_state/entry_signals.json`. Poll interval default 60s.
- **Agent lane** — Hermes health: cron failures (24h, from every profile's `executions.db` + `jobs.json` failure streaks), gateway heartbeat age, scheduler ticker age, kanban board churn.
- **Health rail** — four non-scrolling dots (cron/gateway/ticker/board) pinned left of the marquee; click → Agent tab.
- **Alert pins** — high-severity items (watchlist matches + cron failures) pin as a `⚠ n` chip instead of scrolling past.
- **Watchlist lens** — keyword watchlist (default `HYPE BTC ETH SOL`): matched headlines are accent-highlighted in the strip, listed in the Watchlist tab, and can fire a desktop notification.
- **Ask Hermes (bonus)** — ⌘K *Newswire: Ask Hermes About Last Item*, plus right-click / long-press on any lane item: sends a context-aware prompt to the focused chat (`prompt.submit`).

### New backend routes

| Route | Purpose |
|---|---|
| `GET /trades` | Cached HL snapshot (positions across dexes, spot, fills, conviction) |
| `GET /agent/health` | Signals + high-severity pins from cron/heartbeat/kanban |
| `GET /articles?watch=1` / `?severity=high` | Watchlist / severity filters; every article gains `watch` + `severity` |

### New settings

`ticker_lanes {news,trades,agent}` · `hl_address` (0x + 40 hex) · `hl_poll_interval` (15–3600s) · `watchlist` (string list) · `notify_on_watch` (bool) · `pins_enabled` (bool)

## Telegram channel sources (v0.3.0-dev, 2026-09-17)

Free X alternative: public Telegram channels via their **`t.me/s/<username>` preview page** — no credentials, no API cost (X's API went pay-per-use, $0.005/post read). Channels render recent posts as HTML with `data-post` IDs + ISO timestamps; the backend scrapes + parses them into normal articles, so everything the ticker supports (read state, watchlist highlight, pins, Ask-Hermes) works on TG posts.

- **Add one in the UI**: Settings → Latest/Sources → paste `https://t.me/<user>` or `https://t.me/s/<user>` — instant add (kind auto-detected). Or `POST /sources {kind: "telegram", feed_url: "https://t.me/s/<user>"}`.
- Works for channels that expose the public preview widget. If a channel has preview disabled the page returns no posts and the source stays quiet — pick a different channel.
- Per-source `refresh_interval` default 900s to be polite to t.me; first refresh ingests ~10–20 recent posts, dedup holds thereafter (stale posts older than `max_article_age_hours` are pruned by retention as usual).
- Starter set wired on Guy's box: `WatcherGuru`, `CoinTelegraph`, `@utoday_en`, `@glassnode` (category `telegram-crypto`).

## Test & verify

```bash
env -u PYTHONPATH ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q
node --check desktop/plugin.js
```

## Status

v0.1.0 — feature-complete through M5 (backend, page, ticker, hardening) with an independent QA gate (M6). v0.2.0-dev — signal lanes build (trades · agent · alerts · watchlist · ask-Hermes). Built as a standalone unified plugin against Hermes Desktop v0.21.x plugin SDK.
