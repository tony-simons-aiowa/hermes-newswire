"""Favicon resolution: stored icon wins, else Google s2 over the site domain.

Contract: /sources and /articles rows carry a same-origin proxy path as
favicon_url (never a remote URL the renderer could <img>); feed-host prefixes
(feeds./rss.) are stripped so the SITE brand is fetched; icon_url from search
is validated public-http(s) before storing and a bad one never blocks add.
"""

from __future__ import annotations

from conftest import RSS2, outcome


PREFIX = "/api/plugins/hermes-newswire"


def test_articles_carry_favicon(plugin, client, fake_fetch):
    fake_fetch({"https://www.theverge.com/rss/index.xml": lambda h: outcome(plugin, RSS2)})
    r = client.post(f"{PREFIX}/sources", json={"url": "https://www.theverge.com/rss/index.xml"})
    assert r.status_code == 201
    items = client.get(f"{PREFIX}/articles").json()["items"]
    assert items, "articles must exist"
    fv = items[0]["favicon_url"]
    assert fv == f"{PREFIX}/sources/1/icon", fv
    assert not fv.startswith("http")
    with plugin._db() as conn:
        row = conn.execute("SELECT * FROM sources").fetchone()
    assert plugin._favicon_remote_url(row) == "https://www.google.com/s2/favicons?domain=theverge.com&sz=32"


def test_feed_host_prefix_stripped(plugin, client, fake_fetch):
    fake_fetch({"https://feeds.arstechnica.com/arstechnica/index": lambda h: outcome(plugin, RSS2)})
    r = client.post(f"{PREFIX}/sources", json={"url": "https://feeds.arstechnica.com/arstechnica/index"})
    assert r.status_code == 201
    items = client.get(f"{PREFIX}/articles").json()["items"]
    assert items[0]["favicon_url"] == f"{PREFIX}/sources/1/icon"
    with plugin._db() as conn:
        row = conn.execute("SELECT * FROM sources").fetchone()
    assert "domain=arstechnica.com" in plugin._favicon_remote_url(row)


def test_stored_icon_url_wins(plugin, client, fake_fetch):
    fake_fetch({"https://example.com/feed.xml": lambda h: outcome(plugin, RSS2)})
    r = client.post(f"{PREFIX}/sources", json={
        "url": "https://example.com/feed.xml",
        "icon_url": "https://storage.googleapis.com/site-assets/abc_icon.png"})
    assert r.status_code == 201
    assert r.json()["source"]["favicon_url"] == f"{PREFIX}/sources/1/icon"
    items = client.get(f"{PREFIX}/articles").json()["items"]
    assert items[0]["favicon_url"] == f"{PREFIX}/sources/1/icon"
    with plugin._db() as conn:
        row = conn.execute("SELECT * FROM sources").fetchone()
    assert plugin._favicon_remote_url(row).startswith("https://storage.googleapis.com/")


def test_unsafe_icon_url_dropped_not_fatal(plugin, client, fake_fetch):
    fake_fetch({"https://example.org/feed.xml": lambda h: outcome(plugin, RSS2)})
    r = client.post(f"{PREFIX}/sources", json={
        "url": "https://example.org/feed.xml",
        "icon_url": "http://127.0.0.1/evil.png"})
    assert r.status_code == 201  # source still added
    fv = r.json()["source"]["favicon_url"]
    assert "127.0.0.1" not in fv  # bad icon discarded
    assert fv.startswith(PREFIX)
    with plugin._db() as conn:
        row = conn.execute("SELECT * FROM sources").fetchone()
    assert "127.0.0.1" not in plugin._favicon_remote_url(row)
