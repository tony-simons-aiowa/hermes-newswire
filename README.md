# Hermes Newswire

![Hermes Newswire](assets/hermes-newswire-hero.webp)

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

## Test & verify

```bash
env -u PYTHONPATH ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q
node --check desktop/plugin.js
```

## Agent-friendly development

Coding agents (Hermes, Codex, Claude Code, Cursor, …) start with the root
[`AGENTS.md`](AGENTS.md) — architecture map, critical invariants, exact
verification commands, and a definition of done. The deep reference is
[`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md) (data flow, security model,
safe-change recipes).

Quick gates before any PR: `env -u PYTHONPATH <hermes-venv>/bin/python -m pytest tests/ -q` ·
`node --check desktop/plugin.js` · `hermes plugins validate . --json`.

## Status

v0.1.0 — feature-complete (backend, page, ticker, hardening, search, favicons, grouping) with an independent QA-gated history. 102-test suite + ESM render smoke. Built as a standalone unified plugin against the Hermes Desktop plugin SDK (catalog submission pending).
