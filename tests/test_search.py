"""Feed search (/search): topic search via Feedly public index, hostname
fallback to discovery. Contract: keyless search returns validated public
feed URLs with metadata; URL-ish queries route to discovery; unsafe URLs
never appear in results.
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

from conftest import RSS2, outcome


PREFIX = "/api/plugins/hermes-newswire"


def _feedly_doc(query="space news"):
    return {
        "results": [
            {"feedId": "feed/https://space.example/today.xml", "title": "Space Today",
             "website": "https://space.example", "description": "Daily <b>space</b> news",
             "subscribers": 1200, "language": "en", "iconUrl": "https://img.example/s.png"},
            {"feedId": "feed/http://127.0.0.1/evil.xml", "title": "Evil"},
            {"feedId": "feed/https://private.example/x.xml", "title": "Unresolvable"},
            {"notAFeed": True},
        ]
    }


def _feedly_url(query):
    return f"https://cloud.feedly.com/v3/search/feeds?{urlencode({'query': query, 'count': 12, 'locale': 'en'})}"


def test_search_topic_via_feedly(plugin, client, fake_fetch, monkeypatch):
    # Offline: the SSRF gate resolves candidates — stub DNS to a public IP.
    monkeypatch.setattr(plugin, "_resolve_host_sync", lambda host: ["93.184.216.34"])
    body = json.dumps(_feedly_doc()).encode()
    fake_fetch({_feedly_url("space news"): lambda h: outcome(plugin, body, ctype="application/json")})
    r = client.post(f"{PREFIX}/search", json={"query": "space news"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["via"] == "feedly"
    urls = [x["feed_url"] for x in d["results"]]
    assert "https://space.example/today.xml" in urls
    # SSRF gate: the loopback candidate is filtered out
    assert "http://127.0.0.1/evil.xml" not in urls
    first = d["results"][0]
    assert first["title"] == "Space Today"
    assert first["subscribers"] == 1200
    assert "<b>" not in first["description"]  # HTML stripped


def test_search_empty_query_rejected(plugin, client):
    r = client.post(f"{PREFIX}/search", json={"query": "  "})
    assert r.status_code == 400


def test_search_hostname_falls_back_to_discovery(plugin, client, fake_fetch):
    page = b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed"></head></html>'
    fake_fetch({
        "https://nasa.gov": lambda h: outcome(plugin, page, ctype="text/html"),
        "https://nasa.gov/feed": lambda h: outcome(plugin, RSS2),
    })
    r = client.post(f"{PREFIX}/search", json={"query": "nasa.gov"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["via"] == "discovery"
    assert any(x.get("is_feed") for x in d["results"])


def test_search_unsafe_hostname_rejected(plugin, client):
    # dotted private literal → URL-ish branch → SSRF gate → 400
    r2 = client.post(f"{PREFIX}/search", json={"query": "127.0.0.1"})
    assert r2.status_code == 400
    assert r2.json()["detail"]["code"] == "unsafe_url"


def test_search_direct_feed_url_returns_self(plugin, client, fake_fetch, monkeypatch):
    # Pasted feed URL: the URL itself IS the feed — it must come back as a
    # selectable candidate, not just guesses at sibling paths (RNZ regression).
    from conftest import RSS2, outcome
    monkeypatch.setattr(plugin, "_resolve_host_sync", lambda host: ["93.184.216.34"])
    fake_fetch({
        "https://feed.example/today.xml": lambda h: outcome(plugin, RSS2),
    })
    r = client.post(f"{PREFIX}/search", json={"query": "https://feed.example/today.xml"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["via"] == "discovery"
    assert d["results"], "direct feed URL must yield at least itself"
    first = d["results"][0]
    assert first.get("is_feed") is True
    assert first.get("feed_url") == "https://feed.example/today.xml"
    assert first.get("title") == "Example RSS2"
