"""Trusted-endpoint allowlist tests: opt-in exact host:port exemptions from the
public-IP SSRF policy, for self-hosted feed generators (e.g. RSSHub on localhost).

Scoping contract:
- exact host:port match only (no wildcards, no default-port surprises),
- missing/empty file = upstream behavior unchanged (blocked),
- scheme allowlist still enforced for trusted endpoints,
- literal-IP hosts work the same as hostname hosts.
"""

from __future__ import annotations

import pytest

from conftest import load_plugin


@pytest.fixture
def np(plugin):
    return plugin


def _allow(np, monkeypatch, tmp_path, lines):
    f = tmp_path / "trusted_endpoints.txt"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(np, "_TRUSTED_ENDPOINTS_FILE", f)


def _resolve_localhost(np, monkeypatch):
    monkeypatch.setattr(
        np, "_resolve_host_sync", lambda host: ["127.0.0.1", "::1"]
    )


def test_trusted_exact_host_port_passes(np, monkeypatch, tmp_path):
    _allow(np, monkeypatch, tmp_path, ["localhost:1200"])
    _resolve_localhost(np, monkeypatch)
    ips = np._resolve_validated_ips("http://localhost:1200/twitter/user/x")
    assert set(ips) == {"127.0.0.1", "::1"}


def test_other_port_on_same_host_still_blocked(np, monkeypatch, tmp_path):
    _allow(np, monkeypatch, tmp_path, ["localhost:1200"])
    _resolve_localhost(np, monkeypatch)
    with pytest.raises(np.UnsafeURL):
        np._resolve_validated_ips("http://localhost:9200/probe")


def test_other_host_with_same_port_still_blocked(np, monkeypatch, tmp_path):
    _allow(np, monkeypatch, tmp_path, ["localhost:1200"])
    with pytest.raises(np.UnsafeURL):
        np._resolve_validated_ips("http://127.0.0.1:1200/probe")


def test_missing_file_keeps_upstream_behavior(np, monkeypatch, tmp_path):
    monkeypatch.setattr(
        np, "_TRUSTED_ENDPOINTS_FILE", tmp_path / "does-not-exist.txt"
    )
    _resolve_localhost(np, monkeypatch)
    with pytest.raises(np.UnsafeURL):
        np._resolve_validated_ips("http://localhost:1200/feed")


def test_empty_or_comment_only_file_keeps_upstream_behavior(np, monkeypatch, tmp_path):
    _allow(np, monkeypatch, tmp_path, ["# just a comment", ""])
    _resolve_localhost(np, monkeypatch)
    with pytest.raises(np.UnsafeURL):
        np._resolve_validated_ips("http://localhost:1200/feed")


def test_scheme_allowlist_still_enforced_for_trusted(np, monkeypatch, tmp_path):
    _allow(np, monkeypatch, tmp_path, ["localhost:1200"])
    with pytest.raises(np.UnsafeURL):
        np._resolve_validated_ips("ftp://localhost:1200/feed")


def test_literal_ip_trusted_host(np, monkeypatch, tmp_path):
    _allow(np, monkeypatch, tmp_path, ["127.0.0.1:1200"])
    ips = np._resolve_validated_ips("http://127.0.0.1:1200/feed")
    assert ips == ["127.0.0.1"]


def test_public_host_behavior_unchanged(np, monkeypatch, tmp_path):
    _allow(np, monkeypatch, tmp_path, ["localhost:1200"])
    monkeypatch.setattr(
        np, "_resolve_host_sync", lambda host: ["93.184.216.34"]
    )
    ips = np._resolve_validated_ips("http://example.com/rss.xml")
    assert ips == ["93.184.216.34"]


def test_default_https_port_matching(np, monkeypatch, tmp_path):
    # "host" without :port in the file means default port for the scheme (443/https).
    _allow(np, monkeypatch, tmp_path, ["feedhost.local"])
    monkeypatch.setattr(np, "_resolve_host_sync", lambda host: ["192.0.2.10"])
    ips = np._resolve_validated_ips("https://feedhost.local/rss.xml")
    assert ips == ["192.0.2.10"]
