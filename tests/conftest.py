"""Shared fixtures: load plugin_api.py like the dashboard loader does, tmp HERMES_HOME."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_LOAD_COUNTER = [0]


def load_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Import plugin_api.py fresh, pointed at a per-test HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _LOAD_COUNTER[0] += 1
    name = f"test_newswire_plugin_api_{_LOAD_COUNTER[0]}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # FastAPI resolves string annotations by module name
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    return load_plugin(monkeypatch, tmp_path)


@pytest.fixture
def client(plugin) -> TestClient:
    app = FastAPI()
    app.include_router(plugin.router, prefix="/api/plugins/hermes-newswire")
    # No context manager: engine tests don't need the background refresher.
    return TestClient(app)


class FakeFetch:
    """Replace mod._http_fetch with a scripted responder.

    responses: url -> FetchOutcome | Exception | callable(headers) -> FetchOutcome | Exception
    Records every (url, conditional headers) pair for assertions.
    """

    def __init__(self, plugin, responses: dict[str, Any]):
        self.plugin = plugin
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, *, headers: dict[str, str] | None = None,
                       max_bytes: int | None = None):
        import inspect

        self.calls.append((url, dict(headers or {})))
        target = self.responses.get(url)
        if callable(target):
            result = target(dict(headers or {}))
            if inspect.isawaitable(result):
                result = await result
            target = result
        if isinstance(target, Exception):
            raise target
        assert target is not None, f"unexpected fetch: {url}"
        return target


@pytest.fixture
def fake_fetch(plugin, monkeypatch):
    def install(responses: dict[str, Any]) -> FakeFetch:
        ff = FakeFetch(plugin, responses)
        monkeypatch.setattr(plugin, "_http_fetch", ff)
        return ff

    return install


# ---------------------------------------------------------------------------
# Feed fixtures
# ---------------------------------------------------------------------------
RSS2 = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example RSS2</title>
    <link>https://example.com/</link>
    <description>Example feed</description>
    <item>
      <title>First &lt;b&gt;post&lt;/b&gt;</title>
      <link>https://example.com/first?utm_source=rss</link>
      <guid isPermaLink="false">guid-1</guid>
      <pubDate>Sun, 13 Sep 2026 10:00:00 GMT</pubDate>
      <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Alice</dc:creator>
      <description>Hello &lt;i&gt;world&lt;/i&gt;  with   spaces</description>
    </item>
    <item>
      <title>Second post</title>
      <link>https://example.com/second</link>
      <guid isPermaLink="false">guid-2</guid>
      <pubDate>Sun, 13 Sep 2026 11:30:00 GMT</pubDate>
      <description>Another one</description>
    </item>
  </channel>
</rss>
"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://example.com/">
    <title>Example RDF</title>
    <link>https://example.com/</link>
    <description>RSS 1.0 feed</description>
  </channel>
  <item rdf:about="https://example.com/one">
    <title>RDF item one</title>
    <link>https://example.com/one</link>
    <dc:date>2026-09-12T08:00:00Z</dc:date>
    <dc:creator>Bob</dc:creator>
    <description>rdf body</description>
  </item>
</rdf:RDF>
"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <id>urn:uuid:1234</id>
  <updated>2026-09-13T09:00:00Z</updated>
  <entry>
    <title>Atom &amp;entry</title>
    <id>urn:uuid:abcd-1</id>
    <link rel="alternate" href="https://example.com/atom-1"/>
    <link rel="self" href="https://example.com/feed.atom"/>
    <published>2026-09-13T08:00:00Z</published>
    <updated>2026-09-13T08:05:00Z</updated>
    <author><name>Carol</name></author>
    <summary>Atom summary &lt;em&gt;markup&lt;/em&gt;</summary>
  </entry>
</feed>
"""

JSONFEED = b"""{
  "version": "https://jsonfeed.org/version/1.1",
  "title": "Example JSON Feed",
  "home_page_url": "https://example.com/",
  "items": [
    {
      "id": "jf-1",
      "url": "https://example.com/jf-1",
      "title": "JSON feed item",
      "date_published": "2026-09-13T07:00:00Z",
      "authors": [{"name": "Dan"}],
      "content_text": "plain text body"
    }
  ]
}
"""

MALFORMED_XML = b"<?xml version='1.0'?><rss><channel><title>broken"

HTML_PAGE = b"""<!doctype html>
<html><head>
<link rel="alternate" type="application/rss+xml" title="Example RSS" href="/feed.xml">
<link rel="icon" href="/favicon.ico">
</head><body><p>hi</p></body></html>
"""


def outcome(plugin, body: bytes, status: int = 200, ctype: str = "application/rss+xml",
            headers: dict[str, str] | None = None):
    hdrs = {"content-type": ctype}
    hdrs.update(headers or {})
    return plugin.FetchOutcome(status=status, headers=hdrs, body=body, url="x")
