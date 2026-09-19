"""Engine tests: refresh, conditional GETs, dedup, failure isolation, retention, routes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ATOM, HTML_PAGE, JSONFEED, RSS2, outcome

PREFIX = "/api/plugins/hermes-newswire"


def ok(plugin, body=RSS2, **kw):
    return outcome(plugin, body, **kw)


# --- Source CRUD + discovery --------------------------------------------------

def test_add_direct_feed(plugin, client, fake_fetch):
    fake_fetch({
        "https://example.com/feed.xml": lambda h: ok(plugin, headers={"etag": '"v1"', "last-modified": "Sat, 12 Sep 2026 00:00:00 GMT"}),
    })
    r = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml", "category": "tech"})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["articles_added"] == 2
    src = data["source"]
    assert src["name"] == "Example RSS2"          # feed title used as name
    assert src["etag"] == '"v1"'
    assert src["last_modified"] == "Sat, 12 Sep 2026 00:00:00 GMT"
    assert src["last_error"] is None
    assert src["enabled"] is True
    assert src["category"] == "tech"

    arts = client.get(f"{PREFIX}/articles").json()
    assert arts["total"] == 2
    first = max(arts["items"], key=lambda a: a["published_at"])
    assert first["title"] == "Second post"         # newest first
    assert first["source_name"] == "Example RSS2"
    assert first["read"] is False


def test_add_site_url_discovers_feed(plugin, client, fake_fetch):
    fake_fetch({
        "https://example.com/": lambda h: ok(plugin, HTML_PAGE, ctype="text/html"),
        "https://example.com/feed.xml": lambda h: ok(plugin),
    })
    r = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/"})
    assert r.status_code == 201, r.text
    src = r.json()["source"]
    assert src["feed_url"] == "https://example.com/feed.xml"
    assert src["url"] == "https://example.com/"    # site URL preserved
    assert src["etag"] is None                      # HTML page had no validators... feed response had none
    assert r.json()["articles_added"] == 2


def test_add_discovers_and_keeps_feed_validators(plugin, client, fake_fetch):
    fake_fetch({
        "https://example.com/": lambda h: ok(plugin, HTML_PAGE, ctype="text/html"),
        "https://example.com/feed.xml": lambda h: ok(plugin, headers={"etag": '"feed-etag"'}),
    })
    r = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/"})
    assert r.status_code == 201
    assert r.json()["source"]["etag"] == '"feed-etag"'   # from the FEED response, not the page


def test_add_duplicate_rejected(plugin, client, fake_fetch):
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin)})
    assert client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"}).status_code == 201
    r = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "duplicate"


def test_add_no_feed_found(plugin, client, fake_fetch):
    responses = {"https://example.com/": lambda h: ok(plugin, b"<html><body>x</body></html>", ctype="text/html")}
    for path in plugin.COMMON_FEED_PATHS:
        responses[f"https://example.com{path}"] = lambda h: ok(plugin, b"not found", status=404, ctype="text/plain")
    fake_fetch(responses)
    r = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "no_feed_found"


def test_add_fetch_failure(plugin, client, fake_fetch):
    fake_fetch({"https://down.example/feed": RuntimeError("boom")})
    r = client.post(f"{PREFIX}/sources", json={"url": "https://down.example/feed"})
    assert r.status_code == 502
    assert "boom" in r.json()["detail"]["message"]


def test_add_bad_scheme(plugin, client):
    r = client.post(f"{PREFIX}/sources", json={"url": "file:///etc/passwd"})
    assert r.status_code == 400


def test_patch_and_delete_source(plugin, client, fake_fetch):
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin)})
    sid = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"}).json()["source"]["id"]

    r = client.patch(f"{PREFIX}/sources/{sid}", json={"name": "Renamed", "category": "news", "enabled": False, "refresh_interval": 600})
    assert r.status_code == 200
    src = r.json()["source"]
    assert src["name"] == "Renamed"
    assert src["category"] == "news"
    assert src["enabled"] is False
    assert src["refresh_interval"] == 600

    # Unknown field rejected
    assert client.patch(f"{PREFIX}/sources/{sid}", json={"bogus": 1}).status_code == 400
    # Bad refresh_interval rejected
    assert client.patch(f"{PREFIX}/sources/{sid}", json={"refresh_interval": 5}).status_code == 400

    r = client.delete(f"{PREFIX}/sources/{sid}")
    assert r.status_code == 200
    assert client.get(f"{PREFIX}/sources").json()["sources"] == []
    assert client.get(f"{PREFIX}/articles").json()["total"] == 0   # articles cascade
    assert client.delete(f"{PREFIX}/sources/{sid}").status_code == 404


def test_patch_feed_url_resets_conditional_state(plugin, client, fake_fetch):
    fake_fetch({
        "https://example.com/feed.xml": lambda h: ok(plugin, headers={"etag": '"v1"'}),
        "https://new.example/feed.xml": lambda h: ok(plugin, ATOM, ctype="application/atom+xml"),
    })
    sid = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"}).json()["source"]["id"]
    assert client.get(f"{PREFIX}/sources").json()["sources"][0]["etag"] == '"v1"'
    r = client.patch(f"{PREFIX}/sources/{sid}", json={"feed_url": "https://new.example/feed.xml"})
    assert r.status_code == 200
    src = r.json()["source"]
    assert src["etag"] is None and src["last_modified"] is None and src["last_checked_at"] is None


# --- Refresh: conditional GETs + 304 ------------------------------------------

def test_refresh_sends_conditional_headers(plugin, client, fake_fetch):
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin, headers={"etag": '"v1"'})})
    sid = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"}).json()["source"]["id"]

    calls: list[dict] = []

    def second(headers):
        calls.append(headers)
        return ok(plugin, RSS2.replace(b"First", b"First-UPDATED"), headers={"etag": '"v2"'})

    ff = fake_fetch({"https://example.com/feed.xml": second})
    r = client.post(f"{PREFIX}/sources/{sid}/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["ok"] is True
    assert body["result"]["added"] == 0              # same guids -> no new rows
    assert calls and calls[0].get("If-None-Match") == '"v1"'
    assert body["source"]["etag"] == '"v2"'


def test_refresh_304_is_success_no_reparse(plugin, client, fake_fetch):
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin, headers={"etag": '"v1"'})})
    sid = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"}).json()["source"]["id"]

    def not_modified(headers):
        assert headers.get("If-None-Match") == '"v1"'
        return ok(plugin, b"", status=304)

    fake_fetch({"https://example.com/feed.xml": not_modified})
    r = client.post(f"{PREFIX}/sources/{sid}/refresh").json()
    assert r["result"]["ok"] is True
    assert r["result"]["not_modified"] is True
    assert r["source"]["last_success_at"] is not None
    assert r["source"]["last_error"] is None
    assert r["source"]["error_count"] == 0
    assert client.get(f"{PREFIX}/articles").json()["total"] == 2  # unchanged


def test_refresh_http_error_recorded(plugin, client, fake_fetch):
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin)})
    sid = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"}).json()["source"]["id"]
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin, b"err", status=500, ctype="text/plain")})
    r = client.post(f"{PREFIX}/sources/{sid}/refresh").json()
    assert r["result"]["ok"] is False
    assert "HTTP 500" in r["result"]["error"]
    assert r["source"]["error_count"] == 1
    assert "500" in r["source"]["last_error"]


def test_refresh_html_feed_rejected(plugin, client, fake_fetch):
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin)})
    sid = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"}).json()["source"]["id"]
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin, b"<html><body>page</body></html>", ctype="text/html")})
    r = client.post(f"{PREFIX}/sources/{sid}/refresh").json()
    assert r["result"]["ok"] is False
    assert "not a feed" in r["result"]["error"]


def test_refresh_parse_error_recorded(plugin, client, fake_fetch):
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin)})
    sid = client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"}).json()["source"]["id"]
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin, b"<rss><channel><broken", ctype="application/rss+xml")})
    r = client.post(f"{PREFIX}/sources/{sid}/refresh").json()
    assert r["result"]["ok"] is False
    assert "parse error" in r["result"]["error"].lower()
    assert r["source"]["error_count"] == 1


def test_refresh_all_failure_isolation(plugin, client, fake_fetch):
    fake_fetch({
        "https://good.example/feed.xml": lambda h: ok(plugin),
        "https://bad.example/feed.xml": lambda h: ok(plugin),
    })
    client.post(f"{PREFIX}/sources", json={"url": "https://good.example/feed.xml"})
    client.post(f"{PREFIX}/sources", json={"url": "https://bad.example/feed.xml"})
    fake_fetch({
        "https://good.example/feed.xml": lambda h: ok(plugin, RSS2.replace(b"Second", b"Second-v2")),
        "https://bad.example/feed.xml": lambda h: ok(plugin, b"gone", status=503, ctype="text/plain"),
    })
    r = client.post(f"{PREFIX}/refresh-all").json()
    by = {x["source_id"]: x for x in r["results"]}
    assert by[1]["ok"] and by[1]["added"] == 0        # guid dedup across refresh
    assert by[2]["ok"] is False and "503" in by[2]["error"]


# --- Dedup ladder ---------------------------------------------------------------

def _src_with_entries(plugin, client, fake_fetch, entries_xml: bytes, feed_url="https://example.com/feed.xml"):
    body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>D</title>""" + entries_xml + b"""</channel></rss>"""
    fake_fetch({feed_url: lambda h: ok(plugin, body)})
    sid = client.post(f"{PREFIX}/sources", json={"url": feed_url}).json()["source"]["id"]
    return sid, body


def test_dedup_guid_same_source(plugin, client, fake_fetch):
    entries = b"""<item><title>A</title><guid>g1</guid><link>https://example.com/a</link></item>"""
    sid, body = _src_with_entries(plugin, client, fake_fetch, entries)
    before = client.get(f"{PREFIX}/articles").json()["total"]
    client.post(f"{PREFIX}/sources/{sid}/refresh")
    assert client.get(f"{PREFIX}/articles").json()["total"] == before


def test_dedup_canonical_url_without_guid(plugin, client, fake_fetch):
    entries = b"""<item><title>A</title><link>https://example.com/a</link></item>
                  <item><title>A2</title><link>https://example.com/a#frag</link></item>"""
    _src_with_entries(plugin, client, fake_fetch, entries)
    # Same link modulo fragment -> canonical URLs equal -> 1 article
    assert client.get(f"{PREFIX}/articles").json()["total"] == 1


def test_dedup_normalized_url(plugin, client, fake_fetch):
    entries = b"""<item><title>A</title><link>https://EXAMPLE.com/a?utm_source=rss&amp;b=2</link></item>
                  <item><title>B</title><link>https://example.com/a?b=2</link></item>"""
    _src_with_entries(plugin, client, fake_fetch, entries)
    assert client.get(f"{PREFIX}/articles").json()["total"] == 1


def test_dedup_normalized_title_within_source(plugin, client, fake_fetch):
    entries = b"""<item><title>  Same   Headline </title><link>https://example.com/1</link></item>
                  <item><title>same headline</title><link>https://example.com/2</link></item>"""
    _src_with_entries(plugin, client, fake_fetch, entries)
    assert client.get(f"{PREFIX}/articles").json()["total"] == 1


def test_dedup_content_hash_across_sources(plugin, client, fake_fetch):
    e1 = b"""<item><title>T</title><description>same body</description><link>https://a.example/1</link></item>"""
    e2 = b"""<item><title>T</title><description>same body</description><link>https://b.example/1</link></item>"""
    _src_with_entries(plugin, client, fake_fetch, e1, "https://a.example/feed.xml")
    _src_with_entries(plugin, client, fake_fetch, e2, "https://b.example/feed.xml")
    # Identical title+summary from different sources -> global content-hash dedup keeps 1
    assert client.get(f"{PREFIX}/articles").json()["total"] == 1


def test_same_guid_different_sources_both_kept(plugin, client, fake_fetch):
    e1 = b"""<item><title>A1</title><guid>shared-guid</guid><link>https://a.example/1</link></item>"""
    e2 = b"""<item><title>B1</title><guid>shared-guid</guid><link>https://b.example/1</link></item>"""
    _src_with_entries(plugin, client, fake_fetch, e1, "https://a.example/feed.xml")
    _src_with_entries(plugin, client, fake_fetch, e2, "https://b.example/feed.xml")
    assert client.get(f"{PREFIX}/articles").json()["total"] == 2   # UNIQUE(source_id, guid)


# --- Articles read state + filters ------------------------------------------------

def test_article_read_roundtrip_and_filters(plugin, client, fake_fetch):
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin)})
    client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"})
    arts = client.get(f"{PREFIX}/articles").json()["items"]
    a1, a2 = arts[0], arts[1]

    assert client.post(f"{PREFIX}/articles/{a1['id']}/read", json={}).status_code == 200
    unread = client.get(f"{PREFIX}/articles?unread=true").json()
    assert unread["total"] == 1 and unread["items"][0]["id"] == a2["id"]

    # unread filter with source_id
    assert client.get(f"{PREFIX}/articles?unread=true&source_id=999").json()["total"] == 0

    # unread -> read toggle back
    assert client.post(f"{PREFIX}/articles/{a1['id']}/read", json={"read": False}).status_code == 200
    assert client.get(f"{PREFIX}/articles?unread=true").json()["total"] == 2

    # include_summary=false drops summary
    nosum = client.get(f"{PREFIX}/articles?include_summary=false").json()["items"][0]
    assert "summary" not in nosum

    # pagination
    page = client.get(f"{PREFIX}/articles?limit=1&offset=1").json()
    assert page["total"] == 2 and len(page["items"]) == 1

    assert client.post(f"{PREFIX}/articles/424242/read").status_code == 404


def test_read_all_scope(plugin, client, fake_fetch):
    fake_fetch({
        "https://a.example/feed.xml": lambda h: ok(plugin),
        "https://b.example/feed.xml": lambda h: ok(plugin, ATOM, ctype="application/atom+xml"),
    })
    client.post(f"{PREFIX}/sources", json={"url": "https://a.example/feed.xml"})
    client.post(f"{PREFIX}/sources", json={"url": "https://b.example/feed.xml"})
    r = client.post(f"{PREFIX}/articles/read-all", json={"source_id": 1})
    assert r.status_code == 200
    assert r.json()["updated"] == 2
    assert client.get(f"{PREFIX}/articles?unread=true").json()["total"] == 1   # source 2 untouched
    assert client.post(f"{PREFIX}/articles/read-all", json={}).json()["updated"] == 1


# --- Settings -------------------------------------------------------------------

def test_settings_defaults_and_patch(plugin, client):
    s = client.get(f"{PREFIX}/settings").json()["settings"]
    assert s["refresh_interval"] == 300
    assert s["ticker_speed"] == "normal"

    r = client.patch(f"{PREFIX}/settings", json={"ticker_speed": "fast", "max_headlines": 50, "only_unread": True})
    assert r.status_code == 200
    s = r.json()["settings"]
    assert s["ticker_speed"] == "fast" and s["max_headlines"] == 50 and s["only_unread"] is True

    for bad in ({"ticker_speed": "ludicrous"}, {"refresh_interval": 1}, {"refresh_interval": "x"},
                {"unknown_key": 1}, {"ticker_enabled": "yes"}):
        assert client.patch(f"{PREFIX}/settings", json=bad).status_code == 400
    assert client.patch(f"{PREFIX}/settings", json={}).status_code == 400


def test_state_route(plugin, client):
    st = client.get(f"{PREFIX}/state").json()
    assert st["counts"] == {"sources": 0, "enabled_sources": 0, "articles": 0, "unread": 0}
    assert st["settings"]["refresh_interval"] == 300


# --- Retention --------------------------------------------------------------------

def test_retention_age(plugin, client, fake_fetch):
    # pubDates are derived from "now" on purpose: a pinned date rots into a fixture the
    # retention pass legitimately prunes (on 2026-09-19 the old literal `13 Sep 2026`
    # item had aged past the 24h window and this assertion flipped to []).
    now = datetime.now(timezone.utc)
    old_pub = (now - timedelta(days=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    fresh_pub = (now - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    old = f"""<item><title>Old</title><link>https://example.com/old</link><pubDate>{old_pub}</pubDate></item>""".encode()
    fresh = f"""<item><title>Fresh</title><link>https://example.com/new</link><pubDate>{fresh_pub}</pubDate></item>""".encode()
    client.patch(f"{PREFIX}/settings", json={"max_article_age_hours": 0})  # keep everything for now
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin, b"<rss version='2.0'><channel>" + old + fresh + b"</channel></rss>")})
    client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"})
    assert client.get(f"{PREFIX}/articles").json()["total"] == 2
    client.patch(f"{PREFIX}/settings", json={"max_article_age_hours": 24})
    # trigger retention via a refresh (304 keeps it cheap)
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin, b"", status=304)})
    client.post(f"{PREFIX}/refresh-all")
    titles = [a["title"] for a in client.get(f"{PREFIX}/articles").json()["items"]]
    assert titles == ["Fresh"]


def test_retention_max_headlines(plugin, client, fake_fetch):
    items = b"".join(
        b"<item><title>N%d</title><link>https://example.com/n%d</link></item>" % (i, i) for i in range(10)
    )
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin, b"<rss version='2.0'><channel>" + items + b"</channel></rss>")})
    client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"})
    assert client.get(f"{PREFIX}/articles").json()["total"] == 10
    client.patch(f"{PREFIX}/settings", json={"max_headlines": 3})
    fake_fetch({"https://example.com/feed.xml": lambda h: ok(plugin, b"", status=304)})
    client.post(f"{PREFIX}/refresh-all")
    assert client.get(f"{PREFIX}/articles").json()["total"] == 3


# --- Disabled sources ---------------------------------------------------------------

def test_disabled_source_skipped_by_refresh_all(plugin, client, fake_fetch):
    calls: list[str] = []

    def handler_for(url):
        async def _f(*a, headers=None, **kw):
            calls.append(url)
            return ok(plugin)
        return _f

    fake_fetch({"https://a.example/feed.xml": lambda h: ok(plugin)})
    sid = client.post(f"{PREFIX}/sources", json={"url": "https://a.example/feed.xml"}).json()["source"]["id"]
    client.patch(f"{PREFIX}/sources/{sid}", json={"enabled": False})
    calls.clear()
    fake_fetch({"https://a.example/feed.xml": handler_for("a")})
    r = client.post(f"{PREFIX}/refresh-all").json()
    assert r["results"] == []                       # disabled source not refreshed
    assert calls == []
    # Manual refresh of a disabled source still works (explicit user intent)
    rr = client.post(f"{PREFIX}/sources/{sid}/refresh").json()
    assert rr["result"]["ok"] is True


# --- Background refresher (real lifespan) ---------------------------------------------

def test_background_refresher_refreshes_due_sources(plugin, tmp_path, monkeypatch):
    import asyncio
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def scenario():
        app = FastAPI()
        app.include_router(plugin.router, prefix=PREFIX)
        # Speed up the loop for the test
        monkeypatch.setattr(plugin, "REFRESHER_GRANULARITY", 0.05)

        fetched_urls: list[str] = []

        async def fake_http(url, *, headers=None):
            fetched_urls.append(url)
            return ok(plugin)

        monkeypatch.setattr(plugin, "_http_fetch", fake_http)

        with TestClient(app) as client:               # context manager => lifespan runs
            client.post(f"{PREFIX}/sources", json={"url": "https://example.com/feed.xml"})
            fetched_urls.clear()
            client.patch(f"{PREFIX}/settings", json={"refresh_interval": 30})
            # source last_checked=now; force it due by backdating
            import sqlite3
            db = tmp_path / "state" / "newswire" / "newswire.db"
            conn = sqlite3.connect(db)
            conn.execute("UPDATE sources SET last_checked_at='2020-01-01T00:00:00+00:00'")
            conn.commit(); conn.close()
            for _ in range(50):
                if fetched_urls:
                    break
                await asyncio.sleep(0.05)
            assert fetched_urls, "background refresher never fetched the due source"
            # loop keeps running without dying
            await asyncio.sleep(0.1)
        # after context exit the task is cancelled
        assert plugin._refresher_task is None or plugin._refresher_task.done()

    asyncio.run(scenario())


def test_refresher_loop_survives_errors(plugin, monkeypatch):
    import asyncio

    monkeypatch.setattr(plugin, "REFRESHER_GRANULARITY", 0.01)
    boom = {"n": 0}

    def due(interval):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("db exploded")
        return [999]  # nonexistent source id -> refresh_source returns not-found result

    monkeypatch.setattr(plugin, "_due_source_ids", due)

    async def fake_refresh(sid):
        return {"source_id": sid, "ok": False, "error": "nope"}

    monkeypatch.setattr(plugin, "refresh_source", fake_refresh)

    async def run():
        task = asyncio.create_task(plugin._refresher_loop())
        await asyncio.sleep(0.1)
        assert boom["n"] >= 2                        # loop survived the raised error
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


# --- Health ---------------------------------------------------------------------------

def test_health(plugin, client):
    r = client.get(f"{PREFIX}/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "plugin": "hermes-newswire", "version": plugin.PLUGIN_VERSION}


# --- DB location ----------------------------------------------------------------------

def test_db_under_hermes_home(plugin, tmp_path):
    with plugin._db() as conn:
        conn.execute("INSERT INTO sources (name, feed_url, created_at, updated_at) VALUES ('x','https://x.example/f','2026','2026')")
    db = tmp_path / "state" / "newswire" / "newswire.db"
    assert db.exists()
    assert str(tmp_path) in str(plugin._db_path())
    # WAL mode active
    import sqlite3
    c = sqlite3.connect(db)
    mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    c.close()
    assert mode == "wal"
