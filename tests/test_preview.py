"""POST /preview: open an article in the app's internal preview pane.

Contract: emits the same preview.open gateway event the app's own
open_preview tool uses; SSRF-gates the URL first; rejects missing/unsafe
URLs with 400. Import of the gateway stays lazy so the plugin loads even
outside the desktop (route then reports unavailable).
"""

from __future__ import annotations


PREFIX = "/api/plugins/hermes-newswire"


def test_preview_requires_url(plugin, client):
    r = client.post(f"{PREFIX}/preview", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "missing_url"


def test_preview_rejects_unsafe_url(plugin, client):
    r = client.post(f"{PREFIX}/preview", json={"url": "http://127.0.0.1/x"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "unsafe_url"


def test_preview_emits_gateway_event(plugin, client, monkeypatch):
    import os
    from pathlib import Path

    # Stub the gateway module the route lazy-imports.
    import sys, types
    sent = []
    fake = types.ModuleType("tui_gateway.server")
    fake._broadcast_global_event = lambda event, payload: sent.append((event, payload))
    gw_pkg = types.ModuleType("tui_gateway")
    gw_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "tui_gateway", gw_pkg)
    monkeypatch.setitem(sys.modules, "tui_gateway.server", fake)

    # The SSRF gate resolves the hostname for real, which makes this test hostage to
    # the machine's DNS (a filtering resolver refuses example.com here). Stub the
    # plugin's own documented seam so the assertion is about the preview event only.
    monkeypatch.setattr(plugin, "_resolve_host_sync", lambda host: ["93.184.216.34"])

    r = client.post(f"{PREFIX}/preview", json={"url": "https://example.com/story", "label": "Story"})
    assert r.status_code == 200, r.text
    assert r.json()["opened"] == "https://example.com/story"
    assert sent == [("preview.open", {"url": "https://example.com/story", "label": "Story"})]
