# AGENTS.md — Hermes Newswire

Operating instructions for coding agents (Hermes, Codex, Claude Code, Cursor, …).
Deep reference: [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md).

## Project purpose

A breaking-news ticker plugin for Hermes Desktop: a thin, continuously scrolling
RSS/Atom/JSON-Feed strip docked just above the app's statusbar, plus a `/newswire`
management page. Zero API keys, zero LLM/model tokens for routine operation.

## Architecture map

```
plugin.yaml              native manifest — makes the plugin CLI-visible + catalog-installable
__init__.py              no-op register() (capability probe only; no core tools/hooks)
dashboard/manifest.json  dashboard-plugin manifest (api: plugin_api.py)
dashboard/plugin_api.py  BACKEND — FastAPI router: feed engine, SQLite, SSRF-hardened fetch, routes
desktop/plugin.js        RENDERER — plain ESM, loaded uncompiled by Hermes Desktop
tests/                   pytest (fixtures only — no live network); tests/ui/ ESM render smoke
assets/                  hero artwork
```

- **Backend owns**: all network I/O, parsing, dedup, storage, settings, search.
  Mounted at `/api/plugins/hermes-newswire/` by the desktop's `hermes serve`
  child (requires the plugin in `plugins.enabled` in the Hermes profile's
  `config.yaml`). State lives in `<HERMES_HOME>/state/newswire/newswire.db`
  (SQLite, WAL) — never in the renderer.
- **Renderer owns**: presentation only. Reads normalized JSON from the backend
  via `ctx.rest`; never fetches feeds directly, never renders feed HTML.

## Critical invariants (verified against code)

1. **Zero model calls / zero API keys** in routine operation. No LLM anywhere
   in the feed path.
2. **Feed URLs are attacker-controlled network input.** Every fetch goes
   through `_http_fetch`: scheme allowlist (http/https), SSRF gate blocking
   loopback/private/link-local/CGNAT/metadata addresses (literal IPs incl.
   hex/octal legacy forms, AND post-DNS), per-hop redirect re-validation
   (max 3), 5s connect / 15s total timeouts, 5 MB body cap. Do not weaken.
3. **All feed text is HTML-stripped server-side** (`strip_html`) before
   storage; the renderer only ever receives plain text.
4. **Renderer stays plain ESM** importing only `@hermes/plugin-sdk`,
   `react`, `react/jsx-runtime`, written as `jsx()` calls (not JSX syntax —
   the file loads uncompiled).
5. **The ticker must not cover the Hermes statusbar** — it is a `panes`
   contribution (`placement: 'main'`, `headerVeto: true`,
   `dock: { pane: 'workspace', pos: 'bottom' }`), a layout split, never an
   overlay.
6. **Pane re-registration is idempotence-guarded** (`lastPaneKey`) — an
   unguarded register-on-settings-change causes a React #185 infinite loop.
7. **SQLite migrations must be additive and light** (`PRAGMA table_info` +
   `ALTER TABLE ADD COLUMN` in `_db()`); existing users' data survives.
8. **Conditional GETs stay intact**: ETag/Last-Modified persisted per source,
   304 fast path, plus the never-parsed self-heal (drops validators when a
   source has zero articles AND `articles_ever = 0`) and the
   retention-pruned fast path (no churn loop).
9. **Per-source failure isolation**: a broken source records
   `last_error`/`error_count` and never breaks other sources or the ticker.
10. **Accessibility**: reduced-motion rotation fallback, keyboard-focusable
    headline buttons, aria labels; native `<select>` options must set
    explicit theme-solid backgrounds (OS popup is otherwise unreadable).

## Verification commands

```bash
# Python tests (offline fixtures; expect all passing)
env -u PYTHONPATH ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q

# Renderer syntax (plain ESM, uncompiled)
node --check desktop/plugin.js

# Official plugin validation (manifest, capability probe, collisions)
~/.hermes/hermes-agent/venv/bin/hermes plugins validate . --json

# ESM render smoke (loads the real plugin.js with SDK stubs)
node tests/ui/esm-render.mjs
```

`PYTHONPATH` must be unset for the venv interpreter (a global
`hermes-agent` path breaks venv imports).

## Editing guidance

| Change | Where |
|---|---|
| Feed engine / parsing | `dashboard/plugin_api.py` — `parse_feed`, `discover_in_html`, `_insert_articles` |
| Storage / schema | `dashboard/plugin_api.py` — `_SCHEMA`, `_db()` migrations, `_apply_retention` |
| API routes | `dashboard/plugin_api.py` — `@router.*` handlers (settings via `_validate_setting`) |
| Ticker UI | `desktop/plugin.js` — `NewswireTicker`, `TickerItem`, `SPEED_DURATIONS`, `groupTickerArticles` |
| Newswire page | `desktop/plugin.js` — `LatestTab`, `SourcesTab`, `SettingsTab`, `AddSourceCard` |
| Settings | backend `DEFAULT_SETTINGS` + `_validate_setting` + renderer `SettingsTab` |
| Tests | mirror the behavior in `tests/` (fixture-based, monkeypatch `_http_fetch`) |
| Version/manifests | `plugin.yaml`, `dashboard/manifest.json` (keep in sync) |

## Definition of done

- [ ] `pytest tests/ -q` passes (with `env -u PYTHONPATH`)
- [ ] `node --check desktop/plugin.js` passes
- [ ] Security-sensitive changes ship with regression tests (SSRF, sanitization, redirects)
- [ ] No secrets, private paths, or machine-specific values introduced
- [ ] Docs updated when behavior changes; README claims (incl. test counts) match reality
- [ ] Renderer changes verified in a live Hermes Desktop session when possible
