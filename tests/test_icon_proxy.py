"""Icon proxy: fetch through the pinned backend, size cap, raster-only."""

from __future__ import annotations

import base64

import pytest

from conftest import RSS2, outcome


PREFIX = "/api/plugins/hermes-newswire"


@pytest.fixture
def anyio_backend():
    return "asyncio"

# 1x1 transparent PNG
PNG_1x1 = bytes((
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
    0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41,
    0x54, 0x78, 0x9C, 0x63, 0x00, 0x01, 0x00, 0x00,
    0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00,
    0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
    0x42, 0x60, 0x82,
))

STORED = "https://storage.googleapis.com/site-assets/abc_icon.png"


def _add_source(client, fake_fetch, plugin):
    fake_fetch({
        "https://example.com/feed.xml": lambda h: outcome(plugin, RSS2),
        STORED: lambda h: outcome(plugin, PNG_1x1, ctype="image/png"),
    })
    r = client.post(f"{PREFIX}/sources", json={
        "url": "https://example.com/feed.xml",
        "icon_url": STORED,
    })
    assert r.status_code == 201, r.text
    return r.json()["source"]["id"]


def test_icon_bytes_go_through_backend(plugin, client, fake_fetch):
    sid = _add_source(client, fake_fetch, plugin)
    r = client.get(f"{PREFIX}/sources/{sid}/icon")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == PNG_1x1
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_icon_json_twin_is_data_url(plugin, client, fake_fetch):
    sid = _add_source(client, fake_fetch, plugin)
    r = client.get(f"{PREFIX}/sources/{sid}/icon.json")
    assert r.status_code == 200
    body = r.json()
    assert body["content_type"] == "image/png"
    assert body["data_url"].startswith("data:image/png;base64,")
    raw = base64.b64decode(body["data_url"].split(",", 1)[1])
    assert raw == PNG_1x1


def test_icon_html_rejected(plugin, client, fake_fetch):
    fake_fetch({
        "https://example.com/feed.xml": lambda h: outcome(plugin, RSS2),
        STORED: lambda h: outcome(plugin, b"<!doctype html><html>nope</html>", ctype="text/html"),
    })
    r = client.post(f"{PREFIX}/sources", json={
        "url": "https://example.com/feed.xml", "icon_url": STORED})
    assert r.status_code == 201
    icon = client.get(f"{PREFIX}/sources/1/icon")
    assert icon.status_code == 502
    assert icon.json()["detail"]["code"] == "not_image"


def test_icon_svg_rejected_even_as_image(plugin, client, fake_fetch):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>'
    fake_fetch({
        "https://example.com/feed.xml": lambda h: outcome(plugin, RSS2),
        STORED: lambda h: outcome(plugin, svg, ctype="image/svg+xml"),
    })
    client.post(f"{PREFIX}/sources", json={
        "url": "https://example.com/feed.xml", "icon_url": STORED})
    icon = client.get(f"{PREFIX}/sources/1/icon")
    assert icon.status_code == 502
    assert icon.json()["detail"]["code"] == "not_image"


def test_icon_oversize_rejected(plugin, client, fake_fetch):
    huge = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)) + (b"x" * (plugin.MAX_ICON_BYTES + 8))
    fake_fetch({
        "https://example.com/feed.xml": lambda h: outcome(plugin, RSS2),
        STORED: lambda h: outcome(plugin, huge, ctype="image/png"),
    })
    client.post(f"{PREFIX}/sources", json={
        "url": "https://example.com/feed.xml", "icon_url": STORED})
    icon = client.get(f"{PREFIX}/sources/1/icon")
    assert icon.status_code == 400
    assert icon.json()["detail"]["code"] == "unsafe_url"


def test_icon_cached_second_hit_does_not_refetch(plugin, client, fake_fetch):
    sid = _add_source(client, fake_fetch, plugin)
    ff = fake_fetch({
        "https://example.com/feed.xml": lambda h: outcome(plugin, RSS2),
        STORED: lambda h: outcome(plugin, PNG_1x1, ctype="image/png"),
    })
    assert client.get(f"{PREFIX}/sources/{sid}/icon").status_code == 200
    n = len([u for u, _ in ff.calls if u == STORED])
    assert n == 1
    assert client.get(f"{PREFIX}/sources/{sid}/icon").status_code == 200
    n2 = len([u for u, _ in ff.calls if u == STORED])
    assert n2 == 1


@pytest.mark.anyio
async def test_icon_loopback_rejected_before_fetch(plugin):
    with pytest.raises(plugin.UnsafeURL, match="non-public"):
        await plugin._fetch_icon("http://127.0.0.1/x.png")
