# ruff: noqa: BLE001, PLW1510, S110
"""Hermes Newswire plugin backend — RSS/Atom/JSON Feed engine.

Mounted at ``/api/plugins/hermes-newswire/`` by the Hermes dashboard plugin
system (``dashboard/manifest.json`` declares ``api=plugin_api.py``; the plugin
must be listed in ``plugins.enabled`` in config.yaml before the serve process
imports it — GHSA-mcfc-hp25-cjv7).

Design (M2 backend, Kanban t_e0c80073):

* Feed parsing ported from the proven stdlib parser in the ``rss-feeds`` skill
  (``research/rss-feeds/scripts/feed.py``): RSS 2.0 / RSS 1.0 RDF / Atom /
  JSON Feed, plus HTML discovery via ``<link rel=alternate>`` and common paths.
* SQLite storage under ``<HERMES_HOME>/state/newswire/newswire.db`` (WAL),
  home resolved via ``hermes_constants.get_hermes_home()`` at call time.
* All fetches go through one seam — ``_http_fetch`` — which enforces the SSRF
  policy (http/https only, DNS-resolved IP allow-listing per hop, redirect
  target validation BEFORE following, ≤3 redirects, 5s connect / 15s total,
  5 MB body cap). Connections are PINNED to the validated addresses
  (``_PinnedIPBackend``): the resolution the gate approved is the one dialed,
  with no second DNS lookup between validation and the socket — DNS
  rebinding/TOCTOU has no window. TLS SNI + certificate verification and the
  HTTP Host header keep the original hostname.
* Conditional GETs: per-source ``etag`` / ``last_modified`` persisted and sent
  as ``If-None-Match`` / ``If-Modified-Since``; 304 is a success (timestamps
  updated, no reparse).
* Dedup order per article: feed GUID → canonical URL → normalized URL →
  normalized title (within source) → content hash (global unique index).
* Background refresher rides a router-level lifespan (FastAPI merges router
  lifespans into the app's custom lifespan; router ``on_startup`` would never
  fire because the dashboard app defines its own lifespan).
* HTML is stripped from every stored title/summary/author — the renderer never
  renders feed HTML.
* No secrets, no telemetry, no LLM calls.

Routes (all under /api/plugins/hermes-newswire/):
    GET    /health
    GET    /state                      settings + counts
    GET    /sources
    POST   /sources                    add (with feed discovery), fetch first page
    PATCH  /sources/{id}
    DELETE /sources/{id}
    POST   /sources/{id}/refresh
    GET    /sources/{id}/icon          raster bytes (pinned fetch, cached)
    GET    /sources/{id}/icon.json     JSON twin {data_url, content_type} for ctx.rest
    GET    /articles                   ?limit&offset&source_id&unread&include_summary
    POST   /articles/{id}/read         body {"read": true|false}
    POST   /articles/read-all          body {"source_id":?}
    GET    /settings
    PATCH  /settings
    POST   /refresh-all
    GET    /opml/export
    GET    /opml/export.json             JSON twin {"xml": "..."} for ctx.rest
    POST   /opml/import                body {"xml": "<opml…>"} or raw OPML text
    POST   /discover                   body {"url": "https://site.example/"}
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html as html_mod
import ipaddress
import json
import re
import socket
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import Response

try:  # available in-gateway; tests get it via the hermes-agent venv
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - defensive fallback
    import os

    def get_hermes_home() -> Path:  # type: ignore[misc]
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")

PLUGIN_VERSION = "0.1.1"
USER_AGENT = f"hermes-newswire/{PLUGIN_VERSION} (+https://github.com/NousResearch/hermes-agent)"

# ---------------------------------------------------------------------------
# Tunables / policy constants
# ---------------------------------------------------------------------------
DEFAULT_REFRESH_INTERVAL = 300          # seconds; per-source override supported
REFRESHER_GRANULARITY = 15              # background loop tick
MAX_REDIRECTS = 3
CONNECT_TIMEOUT = 5.0
TOTAL_TIMEOUT = 15.0
MAX_BODY_BYTES = 5 * 1024 * 1024        # 5 MB
MAX_ICON_BYTES = 256 * 1024             # favicons only; smaller than the feed cap
ICON_CACHE_TTL_SEC = 24 * 3600
SUMMARY_MAX_CHARS = 2000
MAX_DISCOVERY_PROBES = 10
# Hard ceiling on new article rows a single refresh may insert, however large
# the feed document is: one accidental giant backfill (or a hostile feed)
# cannot burst-write the DB or flood the ticker beyond retention's reach.
# Entries past the cap are skipped THIS refresh (feeds list newest-first as a
# rule, so the cap drops the oldest tail); the next refresh picks up more.
MAX_ARTICLES_PER_REFRESH = 100

ALLOWED_SCHEMES = {"http", "https"}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

DEFAULT_SETTINGS: dict[str, Any] = {
    "refresh_interval": DEFAULT_REFRESH_INTERVAL,
    "max_article_age_hours": 168,       # 0 = keep forever
    "max_headlines": 500,               # 0 = keep everything
    "ticker_enabled": True,
    "ticker_speed": "normal",           # slow | normal | fast
    "ticker_font_size": 11,             # px, 9-20 — readability knob (Tony)
    "ticker_grouping": "newest",        # newest | source | unread_first
    "pause_on_hover": True,
    "show_source": True,
    "relative_time": True,
    "open_article_behavior": "internal",   # internal (preview pane) | external (OS browser)
    "only_unread": False,
}
_TICKER_SPEEDS = {"slow", "normal", "fast"}

# ---------------------------------------------------------------------------
# Feed parsing (ported from research/rss-feeds skill feed.py)
# ---------------------------------------------------------------------------
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
}
RSS1_NS = "http://purl.org/rss/1.0/"
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/json")
COMMON_FEED_PATHS = (
    "/feed", "/feed.xml", "/rss", "/rss.xml", "/atom.xml",
    "/index.xml", "/feed.json", "/blog/feed", "/blog/rss.xml",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
_ATTR_RES = {
    "type": re.compile(r"""type\s*=\s*["']([^"']+)["']""", re.I),
    "href": re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I),
    "rel": re.compile(r"""rel\s*=\s*["']([^"']+)["']""", re.I),
}


def strip_html(text: str | None) -> str:
    """Unescape entities FIRST, then strip all tags, then collapse whitespace.

    Order matters: unescaping after stripping lets hex/named-escaped tags
    (``&#x3C;script&#x3E;``) survive as literal ``<script>`` text. Unescape-then-
    strip guarantees no tag-like sequence remains — the renderer only ever sees
    plain text.
    """
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html_mod.unescape(text))).strip()


def parse_date(value: str | None) -> str | None:
    """Normalise RFC 822 (RSS) and ISO 8601 (Atom/JSON Feed) dates to UTC ISO."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _text(el: ET.Element, *paths: str) -> str | None:
    for p in paths:
        found = el.find(p, NS)
        if found is not None and (found.text or "").strip():
            return found.text
    return None


def _atom_link(entry: ET.Element) -> str | None:
    alternate = None
    for link in entry.findall("atom:link", NS) + entry.findall("link"):
        href = link.get("href")
        if not href:
            continue
        rel = link.get("rel", "alternate")
        if rel == "alternate":
            return href
        alternate = alternate or href
    return alternate


def parse_xml_feed(data: bytes) -> dict[str, Any]:
    root = ET.fromstring(data)
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag == "feed":  # Atom
        entries = []
        for e in root.findall("atom:entry", NS):
            entries.append({
                "title": strip_html(_text(e, "atom:title")),
                "link": _atom_link(e),
                "guid": _text(e, "atom:id") or None,
                "published": parse_date(_text(e, "atom:published", "atom:updated")),
                "author": strip_html(_text(e, "atom:author/atom:name", "dc:creator")),
                "summary": strip_html(_text(e, "atom:summary", "atom:content"))[:SUMMARY_MAX_CHARS],
            })
        return {"format": "atom", "title": strip_html(_text(root, "atom:title")), "entries": entries}
    channel = root.find("channel") if tag == "rss" else root.find(f"{{{RSS1_NS}}}channel")
    if channel is None:
        channel = root  # RDF without a channel element: fall back to the root
    entries = []
    items = channel.iter("item") if tag == "rss" else root.iter(f"{{{RSS1_NS}}}item")
    for item in items:
        entries.append({
            "title": strip_html(_text(item, "title", f"{{{RSS1_NS}}}title")),
            "link": (_text(item, "link", f"{{{RSS1_NS}}}link") or "").strip() or None,
            "guid": (_text(item, "guid") or None),
            "published": parse_date(_text(item, "pubDate", "dc:date")),
            "author": strip_html(_text(item, "dc:creator", "author")),
            "summary": strip_html(
                _text(item, "content:encoded", "description", f"{{{RSS1_NS}}}description")
            )[:SUMMARY_MAX_CHARS],
        })
    return {
        "format": "rss",
        "title": strip_html(_text(channel, "title", f"{{{RSS1_NS}}}title")),
        "entries": entries,
    }


def parse_json_feed(data: bytes) -> dict[str, Any]:
    doc = json.loads(data)
    entries = []
    for item in doc.get("items", []):
        authors = item.get("authors") or ([item["author"]] if item.get("author") else [])
        entries.append({
            "title": strip_html(item.get("title")),
            "link": item.get("url") or item.get("external_url"),
            "guid": item.get("id") or None,
            "published": parse_date(item.get("date_published") or item.get("date_modified")),
            "author": ", ".join(a.get("name", "") for a in authors if isinstance(a, dict)) or None,
            "summary": strip_html(
                item.get("summary") or item.get("content_text") or item.get("content_html")
            )[:SUMMARY_MAX_CHARS],
        })
    return {"format": "jsonfeed", "title": strip_html(doc.get("title")), "entries": entries}


def parse_feed(data: bytes, content_type: str = "") -> dict[str, Any]:
    head = data.lstrip()[:1]
    if head == b"{" or "json" in content_type.lower():
        return parse_json_feed(data)
    return parse_xml_feed(data)


def looks_like_feed(data: bytes, content_type: str = "") -> bool:
    head = data.lstrip()[:300].lower()
    if head.startswith(b"{") and b"items" in data[:2000]:
        return True
    return b"<rss" in head or b"<feed" in head or b"<rdf" in head


def discover_in_html(page_url: str, page_html: bytes | str) -> list[str]:
    """Candidate feed URLs for a page: <link rel=alternate> first, then common paths."""
    text = page_html.decode("utf-8", "replace") if isinstance(page_html, bytes) else page_html
    found: list[str] = []
    for m in _LINK_TAG_RE.finditer(text):
        tag = m.group(0)
        type_m = _ATTR_RES["type"].search(tag)
        href_m = _ATTR_RES["href"].search(tag)
        rel_m = _ATTR_RES["rel"].search(tag)
        if not href_m or not type_m or type_m.group(1).lower() not in FEED_TYPES:
            continue
        if rel_m and "alternate" not in rel_m.group(1).lower():
            continue
        url = urljoin(page_url, html_mod.unescape(href_m.group(1)))
        if url not in found:
            found.append(url)
    if found:
        return found
    parsed = urlsplit(page_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return [base + p for p in COMMON_FEED_PATHS]


# ---------------------------------------------------------------------------
# URL normalisation + dedup keys
# ---------------------------------------------------------------------------
_UTM_RE = re.compile(r"^utm_")


def canonical_url(feed_url: str, link: str | None) -> str | None:
    """Absolute URL for an item link, fragment stripped. None if no link."""
    if not link or not link.strip():
        return None
    return urljoin(feed_url, link.strip()).split("#", 1)[0] or None


def normalize_url(url: str | None) -> str | None:
    """Lowercase scheme/host, drop default ports, drop utm_* + empty query."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port and not ((parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    if parts.username:
        netloc = f"{parts.username}@{netloc}"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not _UTM_RE.match(k.lower())])
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", query, ""))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def content_hash(title: str, summary: str, fallback: str) -> str:
    basis = f"{(title or '').strip().lower()}\x00{(summary or '')[:500].strip().lower()}"
    if not basis.strip("\x00"):
        basis = fallback
    return _sha256(basis)


# ---------------------------------------------------------------------------
# SSRF policy
# ---------------------------------------------------------------------------
class UnsafeURL(Exception):
    """Raised when a URL (or redirect target) violates the SSRF policy."""


class NotImage(Exception):
    """Raised when an icon fetch did not return a raster image."""


def _resolve_host_sync(host: str) -> list[str]:
    """Resolve a hostname to IP strings. Module seam: tests monkeypatch this."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


def _ip_is_blocked(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        return True
    if ip.version == 4 and ip in _CGNAT:  # 100.64/10 CGNAT (explicit; is_private coverage varies)
        return True
    return False


def _resolve_validated_ips(url: str) -> list[str]:
    """SSRF gate: return the addresses a URL may be contacted on, or raise.

    Raise UnsafeURL unless url is http(s) and resolves to public IPs only.
    The returned list is the *validated set*: every member passed the
    public-IP policy, and — critically — nothing outside this list may be
    dialed for this URL (see ``_PinnedIPBackend``).
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURL(f"unparseable URL: {url!r}") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURL(f"scheme not allowed: {parts.scheme!r} (http/https only)")
    host = parts.hostname
    if not host:
        raise UnsafeURL(f"no hostname in URL: {url!r}")
    # Literal IP: check directly. Hostname: every resolved address must be public.
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        candidates = _resolve_host_sync(host)
        if not candidates:
            raise UnsafeURL(f"cannot resolve host: {host!r}")  # noqa: B904
    for ip_text in candidates:
        if _ip_is_blocked(ip_text):
            raise UnsafeURL(f"host resolves to non-public address: {host} -> {ip_text}")
    return candidates


def _assert_public_http_url(url: str) -> None:
    """Raise UnsafeURL unless url is http(s) and resolves to public IPs only."""
    _resolve_validated_ips(url)


class _PinnedIPBackend:
    """httpcore network backend that dials only SSRF-validated addresses.

    Why this exists: validating a hostname (resolve → policy-check the IPs)
    and then handing the *hostname* to httpx leaves a TOCTOU window — httpx
    resolves the name a second time when opening the socket, and DNS may
    answer differently on the second lookup (DNS rebinding): the gate
    approves 93.184.216.34 while the connection lands on 127.0.0.1,
    169.254.169.254, or RFC1918 space.

    This backend closes the window structurally. ``connect_tcp`` is the only
    place a socket gets opened on this client, and it dials exclusively
    addresses from the pin table that ``_validate_and_pin`` filled — from the
    very resolution the gate approved, for this exact hop, immediately before
    the request. Hostnames are never re-resolved, so a rebinding answer has
    nowhere to land. Anything unpinned fails closed with ConnectError;
    nothing is ever dialed on trust.

    TLS semantics are preserved by construction: only the dial address is
    substituted. httpcore still calls ``start_tls(server_hostname=<original
    host>)`` on the socket we return, so SNI and certificate verification
    keep using the original hostname, and the HTTP Host header is untouched.
    """

    def __init__(self, delegate):
        # ``delegate`` is the pool's original dialer (httpcore AutoBackend);
        # tests substitute a recording double here.
        import httpcore

        self._httpcore = httpcore
        self._delegate = delegate
        self._pins: dict[str, tuple[str, ...]] = {}

    def pin(self, host: str, ips: "list[str] | tuple[str, ...]") -> None:
        """Bind a hostname to its validated address set for this fetch."""
        if host and ips:
            self._pins[host] = tuple(ips)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ):
        pins = self._pins.get(host)
        if not pins:
            raise self._httpcore.ConnectError(
                f"refusing to connect to {host!r}: no SSRF-validated address pinned for this request"
            )
        last_exc: Exception | None = None
        for ip in pins:  # fallback is allowed only *within* the validated set
            try:
                return await self._delegate.connect_tcp(
                    ip, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (self._httpcore.ConnectError, self._httpcore.ConnectTimeout) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None):
        raise self._httpcore.ConnectError("unix sockets are not permitted by the SSRF policy")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


# ---------------------------------------------------------------------------
# HTTP seam (single choke point for every outbound fetch)
# ---------------------------------------------------------------------------
@dataclass
class FetchOutcome:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lowercase keys
    body: bytes = b""
    url: str = ""  # final URL after redirects


async def _validate_and_pin(url: str, backend: "_PinnedIPBackend | None") -> None:
    """Resolve + SSRF-validate one hop URL, then pin the approved addresses.

    This is the anti-DNS-rebinding core: the same resolution whose results
    the gate approved is the one and only source the transport may dial for
    this hop. There is deliberately no second DNS lookup between validation
    and the network connection — ``_PinnedIPBackend.connect_tcp`` reads the
    pin table filled here and dials straight to an approved IP.

    ``backend=None`` means "validate only, never connect" and is reachable
    ONLY through the explicit test seam (a client flagged
    ``_newswire_mock_transport``): production callers must hold a pinned
    backend — ``_http_fetch`` enforces that before any resolution happens.
    """
    candidates = await asyncio.to_thread(_resolve_validated_ips, url)
    if backend is None:  # test-only MockTransport client: gate only
        return
    keys = {urlsplit(url).hostname or ""}
    try:  # authoritative key: exactly what httpcore passes to connect_tcp
        import httpx

        keys.add(str(httpx.URL(url).host))
    except Exception:  # pragma: no cover - URL rejected above already
        pass
    for key in keys:
        if key:
            backend.pin(key, candidates)


async def _http_fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
) -> FetchOutcome:
    """GET with SSRF-checked, IP-pinned manual redirects, timeouts, 5 MB cap.

    Per hop: resolve → validate every address against the public-IP policy →
    pin the validated set for this exact connection attempt → connect. The
    pinned network backend (see ``_PinnedIPBackend``) makes it structurally
    impossible for a fetch to land anywhere the gate did not approve — a URL
    cannot be fetched until its destination IP has been resolved, validated,
    and bound to that connection attempt. TLS SNI, certificate verification,
    and the Host header all keep using the original hostname.

    Streaming: the body is consumed chunk-by-chunk and the cap aborts the
    transfer as soon as it is exceeded, so an oversized response is never
    buffered whole. Module seam: engine tests monkeypatch this whole function;
    the transport-level behaviour is exercised in test_http.py and
    test_dns_pinning.py through the ``_build_async_client`` +
    ``_resolve_host_sync`` seams.
    """
    import httpx

    current = url
    hop_headers = dict(headers or {})
    cap = MAX_BODY_BYTES if max_bytes is None else max_bytes
    client = _build_async_client()
    pin_backend = getattr(client, "_newswire_pin_backend", None)
    if pin_backend is None and not getattr(client, "_newswire_mock_transport", False):
        # Fail closed: without the pinned backend, httpx would re-resolve the
        # hostname itself when dialing — silently restoring the DNS-rebinding
        # window this module exists to close. Only clients explicitly flagged
        # as test mocks (httpx.MockTransport; they never open sockets) may
        # proceed past the URL gate alone. Everything else refuses to fetch.
        await client.aclose()
        raise RuntimeError(
            f"refusing to fetch {url!r}: SSRF IP-pinning transport unavailable "
            "(httpx/httpcore internals changed?)"
        )
    try:
        async with client:
            for _hop in range(MAX_REDIRECTS + 1):
                await _validate_and_pin(current, pin_backend)
                async with client.stream("GET", current, headers=hop_headers) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise RuntimeError(f"redirect without Location from {current}")
                        current = urljoin(current, location)
                        continue
                    body = b""
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > cap:
                            raise UnsafeURL(f"response body exceeds {cap} bytes")
                    return FetchOutcome(
                        status=resp.status_code,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                        body=body,
                        url=current,
                    )
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"timeout fetching {url}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"network error fetching {current}: {exc}") from exc
    raise RuntimeError(f"too many redirects (>{MAX_REDIRECTS}) fetching {url}")


async def _assert_public_http_url_sync(url: str) -> None:
    """Async wrapper: DNS resolution runs in a worker thread."""
    await asyncio.to_thread(_assert_public_http_url, url)


def _install_pin_backend(client) -> "_PinnedIPBackend | None":
    """Install ``_PinnedIPBackend`` on the client's httpcore connection pool.

    Returns the backend, or ``None`` if this httpx/httpcore build does not
    expose the expected transport seam (``_transport._pool._network_backend``).
    Callers MUST fail closed on ``None`` — see ``_build_async_client``.
    """
    pool = getattr(getattr(client, "_transport", None), "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        return None
    backend = _PinnedIPBackend(pool._network_backend)
    pool._network_backend = backend
    client._newswire_pin_backend = backend
    return backend


def _build_async_client():
    """httpx client factory seam (tests inject MockTransport here).

    Installs ``_PinnedIPBackend`` (see ``_install_pin_backend``) so the only
    dialer this client ever uses is the one that refuses unpinned hosts.
    On AsyncClient the backend is reachable as ``client._newswire_pin_backend``
    (the attribute ``_http_fetch`` reads to fill the per-hop pin table).

    FAIL CLOSED: this function relies on httpx/httpcore internals (the
    connection pool's ``_network_backend`` attribute) because httpx 0.28
    exposes no public transport-level resolver hook. If a future httpx or
    httpcore release changes that internal shape, installation returns None —
    and we REFUSE to return an ordinary client, because a plain httpx client
    re-resolves hostnames itself when dialing, which would silently restore
    the DNS-rebinding window this module exists to close. Newswire either
    connects through the validated-IP-pinned transport or it does not
    connect at all; the plugin surfaces a RuntimeError instead.
    """
    import httpx

    client = httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT),
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    if _install_pin_backend(client) is None:
        raise RuntimeError(
            "SSRF IP-pinning transport could not be installed: httpx/httpcore "
            "internals changed (expected _transport._pool._network_backend). "
            "Refusing outbound networking without validated-IP pinning."
        )
    return client


# ---------------------------------------------------------------------------
# SQLite storage
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  feed_url TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  category TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  refresh_interval INTEGER,
  last_checked_at TEXT,
  last_success_at TEXT,
  last_error TEXT,
  error_count INTEGER NOT NULL DEFAULT 0,
  etag TEXT,
  last_modified TEXT
);
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  guid TEXT,
  canonical_url TEXT,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  author TEXT,
  published_at TEXT,
  discovered_at TEXT NOT NULL,
  read INTEGER NOT NULL DEFAULT 0,
  hash TEXT NOT NULL,
  UNIQUE(source_id, guid)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_hash ON articles(hash);
CREATE INDEX IF NOT EXISTS idx_articles_source_pub ON articles(source_id, published_at DESC);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> Path:
    return get_hermes_home() / "state" / "newswire" / "newswire.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        # Light migration: articles_ever distinguishes "never parsed" (heal a
        # stale 304 validator) from "parsed but retention-pruned" (fast-path,
        # no churn loop — O1).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "favicon_url" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN favicon_url TEXT NOT NULL DEFAULT ''")
        if "articles_ever" not in cols:
            conn.execute(
                "ALTER TABLE sources ADD COLUMN articles_ever INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                """UPDATE sources SET articles_ever = (
                       SELECT COUNT(*) FROM articles WHERE articles.source_id = sources.id)"""
            )
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS[key]
    return json.loads(row["value"])


def all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        try:
            out[row["key"]] = json.loads(row["value"])
        except (ValueError, TypeError):
            continue
    return out


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )



def _favicon_remote_url(row: sqlite3.Row) -> str:
    """Remote icon URL the backend will fetch: stored Feedly/website icon, else
    the Google s2 favicon service over the site domain (never the feed host —
    feedburner et al would show the wrong brand). Empty when nothing derivable.

    This URL is never handed to the renderer as an ``<img src>`` — it is only
    fetched through ``_fetch_icon`` / the pinned transport.
    """
    def col(*names: str) -> str:
        for n in names:
            if n in row.keys():
                v = (row[n] or "").strip()
                if v:
                    return v
        return ""
    stored = col("favicon_url", "stored_favicon")
    if stored:
        return stored
    for cand in (col("url", "src_url"), col("feed_url", "src_feed_url")):
        try:
            host = urlsplit(cand).hostname
        except ValueError:
            host = None
        if host and "." in host:
            # strip common feed/www prefixes to the site brand domain
            parts = host.split(".")
            while len(parts) > 2 and parts[0] in ("feeds", "rss", "feed", "news", "www"):
                parts = parts[1:]
            domain = ".".join(parts)
            return f"https://www.google.com/s2/favicons?domain={domain}&sz=32"
    return ""


def _favicon_proxy_url(source_id: int) -> str:
    return f"/api/plugins/hermes-newswire/sources/{int(source_id)}/icon"


def _favicon_for_source(row: sqlite3.Row) -> str:
    """Same-origin proxy path the renderer may use as ``<img src>``.

    The remote URL stays server-side. Empty when nothing derivable.
    """
    if not _favicon_remote_url(row):
        return ""
    keys = row.keys()
    if "source_id" in keys and row["source_id"]:
        sid = row["source_id"]
    else:
        sid = row["id"]
    return _favicon_proxy_url(sid)


# url -> (expires_at, body, content_type)
_icon_mem: dict[str, tuple[float, bytes, str]] = {}


def _sniff_raster_image(body: bytes) -> str | None:
    """Return a safe raster image/* type from magic bytes, or None.

    SVG/HTML/XML are rejected even if a Content-Type claims image/*.
    """
    if len(body) < 12:
        return None
    png_magic = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A))
    ico_magic = bytes((0x00, 0x00, 0x01, 0x00))
    cur_magic = bytes((0x00, 0x00, 0x02, 0x00))
    if body.startswith(png_magic):
        return "image/png"
    if body.startswith(bytes((0xFF, 0xD8, 0xFF))):
        return "image/jpeg"
    if body.startswith(b"GIF87a") or body.startswith(b"GIF89a"):
        return "image/gif"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    if body.startswith(ico_magic) or body.startswith(cur_magic):
        return "image/x-icon"
    if body.startswith(b"BM"):
        return "image/bmp"
    if body[4:8] == b"ftyp" and body[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


async def _fetch_icon(url: str) -> tuple[bytes, str]:
    """Fetch an icon through ``_http_fetch`` (pinned), with a tight size cap.

    Returns ``(body, content_type)``. Raises ``UnsafeURL``, ``NotImage``, or
    a generic ``RuntimeError`` on upstream failure. Cached in-process by URL.
    """
    now = datetime.now(timezone.utc).timestamp()
    hit = _icon_mem.get(url)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    outcome = await _http_fetch(
        url,
        headers={"Accept": "image/*,*/*;q=0.1"},
        max_bytes=MAX_ICON_BYTES,
    )
    if outcome.status != 200:
        raise RuntimeError(f"HTTP {outcome.status} fetching icon")
    if len(outcome.body) > MAX_ICON_BYTES:
        raise UnsafeURL(f"response body exceeds {MAX_ICON_BYTES} bytes")
    sniffed = _sniff_raster_image(outcome.body)
    if not sniffed:
        raise NotImage("response is not a raster image")
    declared = (outcome.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if declared in {"image/svg+xml", "text/html", "application/xml", "text/xml"}:
        raise NotImage(f"refusing content-type {declared}")
    _icon_mem[url] = (now + ICON_CACHE_TTL_SEC, outcome.body, sniffed)
    return outcome.body, sniffed


def _icon_payload(body: bytes, content_type: str) -> dict[str, str]:
    b64 = base64.b64encode(body).decode("ascii")
    return {
        "content_type": content_type,
        "data_url": f"data:{content_type};base64,{b64}",
    }


def _source_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "feed_url": row["feed_url"],
        "enabled": bool(row["enabled"]),
        "category": row["category"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "refresh_interval": row["refresh_interval"],
        "last_checked_at": row["last_checked_at"],
        "last_success_at": row["last_success_at"],
        "last_error": row["last_error"],
        "error_count": row["error_count"],
        "etag": row["etag"],
        "last_modified": row["last_modified"],
        "favicon_url": _favicon_for_source(row),
    }


def _article_row(row: sqlite3.Row, *, include_summary: bool = True, source_name: str | None = None) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "source_id": row["source_id"],
        "guid": row["guid"],
        "canonical_url": row["canonical_url"],
        "title": row["title"],
        "author": row["author"],
        "published_at": row["published_at"],
        "discovered_at": row["discovered_at"],
        "read": bool(row["read"]),
    }
    if source_name is not None:
        out["source_name"] = source_name
    try:
        out["favicon_url"] = row["favicon_url"] or ""
    except (IndexError, KeyError):
        out["favicon_url"] = ""
    if include_summary:
        out["summary"] = row["summary"]
    return out


# ---------------------------------------------------------------------------
# Refresh engine
# ---------------------------------------------------------------------------
_refresh_locks: dict[int, asyncio.Lock] = {}


def _get_refresh_lock() -> asyncio.Lock:
    """Per-event-loop refresh lock.

    One loop in production; tests drive the router through per-request portals,
    so keying by the running loop keeps the lock valid in both worlds.
    """
    loop_id = id(asyncio.get_running_loop())
    lock = _refresh_locks.get(loop_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[loop_id] = lock
    return lock


def _conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


async def refresh_source(source_id: int) -> dict[str, Any]:
    """Fetch + parse one source, record errors on the source row.

    Never raises for fetch/parse failures — returns a result dict so callers
    (background loop, refresh-all) get per-source isolation for free.

    Failure policy (deliberate, M5): a failing source is NEVER auto-disabled.
    ``error_count`` counts CONSECUTIVE failures (reset to 0 on any success —
    200 or 304) and ``last_error`` carries the latest message; both surface in
    the Sources UI. Rationale: transient DNS/offline blips and temporary
    Cloudflare block pages are the common failure modes, and silently stopping
    refreshes would read as "my feed went stale" with no visible cause. The
    user disables a source explicitly; the engine only reports.
    """
    with _db() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            return {"source_id": source_id, "ok": False, "error": "source not found"}
        feed_url, etag, lm, name = row["feed_url"], row["etag"], row["last_modified"], row["name"]

    now = _now_iso()
    try:
        outcome = await _http_fetch(feed_url, headers=_conditional_headers(etag, lm))
    except Exception as exc:  # fetch failure: record + isolate
        with _db() as conn:
            conn.execute(
                "UPDATE sources SET last_checked_at=?, last_error=?, error_count=error_count+1, updated_at=? WHERE id=?",
                (now, str(exc)[:500], now, source_id),
            )
        return {"source_id": source_id, "ok": False, "error": str(exc)[:500]}

    if outcome.status == 304:
        # Self-heal guard: a 304 means "the document we already parsed is
        # unchanged" — but if this source has NEVER stored articles, that
        # document was never parsed (validator captured from an aborted/raced
        # first fetch — observed live: serve restart between the etag write
        # and the article insert). A 304-on-never-parsed is nonsense; drop
        # the validators and force a full re-fetch next tick. A source whose
        # articles were parsed but pruned by retention (articles_ever > 0,
        # count 0) keeps the fast path — no churn loop (O1).
        with _db() as conn:
            src = conn.execute(
                "SELECT (SELECT COUNT(*) FROM articles WHERE source_id=?) AS n, articles_ever FROM sources WHERE id=?",
                (source_id, source_id),
            ).fetchone()
            if src and src["n"] == 0 and not src["articles_ever"]:
                conn.execute(
                    "UPDATE sources SET etag=NULL, last_modified=NULL WHERE id=?",
                    (source_id,),
                )
                return {"source_id": source_id, "ok": True, "not_modified": True, "added": 0,
                        "self_heal": "dropped stale validators (source had no articles)"}
        with _db() as conn:
            src2 = conn.execute(
                "SELECT (SELECT COUNT(*) FROM articles WHERE source_id=?) AS n2, articles_ever FROM sources WHERE id=?",
                (source_id, source_id),
            ).fetchone()
            if src2 and src2["n2"] == 0 and src2["articles_ever"]:
                # Parsed at least once, everything since pruned by retention —
                # a healthy-but-quiet source (all-stale feed). Explain it in
                # last_error so the Sources UI can say WHY it shows nothing
                # (O1: surfacing, not churning). Cleared on the next insert.
                conn.execute(
                    "UPDATE sources SET last_error=?, updated_at=? WHERE id=?",
                    ("all articles older than retention window (max_article_age_hours)", _now_iso(), source_id),
                )
                conn.execute(
                    """UPDATE sources SET last_checked_at=?, last_success_at=?, error_count=0 WHERE id=?""",
                    (now, now, source_id),
                )
                _apply_retention(conn)
                return {"source_id": source_id, "ok": True, "not_modified": True, "added": 0,
                        "note": "all articles older than the retention window — raise max_article_age_hours to keep stale history"}
            conn.execute(
                """UPDATE sources SET last_checked_at=?, last_success_at=?, last_error=NULL,
                   error_count=0, updated_at=? WHERE id=?""",
                (now, now, now, source_id),
            )
            _apply_retention(conn)  # settings changes apply even when feeds are unchanged
        return {"source_id": source_id, "ok": True, "not_modified": True, "added": 0}

    if outcome.status != 200:
        msg = f"HTTP {outcome.status} fetching {feed_url}"
        with _db() as conn:
            conn.execute(
                "UPDATE sources SET last_checked_at=?, last_error=?, error_count=error_count+1, updated_at=? WHERE id=?",
                (now, msg[:500], now, source_id),
            )
        return {"source_id": source_id, "ok": False, "error": msg}

    ctype = outcome.headers.get("content-type", "")
    if not looks_like_feed(outcome.body, ctype):
        if "html" in ctype.lower():
            msg = f"not a feed: {feed_url} returned text/html"
            with _db() as conn:
                conn.execute(
                    "UPDATE sources SET last_checked_at=?, last_error=?, error_count=error_count+1, updated_at=? WHERE id=?",
                    (now, msg[:500], now, source_id),
                )
            return {"source_id": source_id, "ok": False, "error": msg}
    try:
        feed = parse_feed(outcome.body, ctype)
    except (ET.ParseError, ValueError, json.JSONDecodeError) as exc:
        msg = f"parse error: {exc}"
        with _db() as conn:
            conn.execute(
                "UPDATE sources SET last_checked_at=?, last_error=?, error_count=error_count+1, updated_at=? WHERE id=?",
                (now, msg[:500], now, source_id),
            )
        return {"source_id": source_id, "ok": False, "error": msg}

    added = _insert_articles(source_id, feed_url, feed)
    new_name = name if (name and name != feed_url) else (feed.get("title") or name)

    with _db() as conn:
        if added:
            conn.execute(
                "UPDATE sources SET articles_ever = articles_ever + ? WHERE id=?",
                (added, source_id),
            )
        conn.execute(
            """UPDATE sources SET last_checked_at=?, last_success_at=?, last_error=NULL,
               error_count=0, etag=?, last_modified=?, name=?, updated_at=? WHERE id=?""",
            (
                now, now,
                outcome.headers.get("etag") or None,
                outcome.headers.get("last-modified") or None,
                new_name, now, source_id,
            ),
        )
        _apply_retention(conn)
    return {"source_id": source_id, "ok": True, "added": added, "format": feed.get("format")}


def _insert_articles(source_id: int, feed_url: str, feed: dict[str, Any]) -> int:
    """Insert feed entries with the dedup ladder. Returns rows actually added.

    Ladder per item: feed GUID → canonical URL → normalized URL → normalized
    title (all within-source) → global content hash (unique index). The maps are
    preloaded once per source and updated as rows insert, so normalized URLs are
    always compared against normalized stored URLs (never raw vs normalized).

    New-row inserts are capped at MAX_ARTICLES_PER_REFRESH: whatever the feed
    document contains, one refresh never burst-writes more rows than that.
    """
    added = 0
    now = _now_iso()
    with _db() as conn:
        guids: dict[str, int] = {}
        canon: dict[str, int] = {}
        norms: dict[str, int] = {}
        titles: dict[str, int] = {}
        for row in conn.execute(
            "SELECT id, guid, canonical_url, title FROM articles WHERE source_id=?", (source_id,)
        ).fetchall():
            if row["guid"]:
                guids.setdefault(row["guid"], row["id"])
            if row["canonical_url"]:
                canon.setdefault(row["canonical_url"], row["id"])
                n = normalize_url(row["canonical_url"])
                if n:
                    norms.setdefault(n, row["id"])
            if row["title"]:
                titles.setdefault(_WS_RE.sub(" ", row["title"]).strip().lower(), row["id"])

        for item in feed.get("entries", []):
            guid = (item.get("guid") or "").strip() or None
            can = canonical_url(feed_url, item.get("link"))
            norm = normalize_url(can)
            title = item.get("title") or ""
            summary = item.get("summary") or ""
            author = item.get("author") or None
            published = item.get("published")

            existing_id: int | None = None
            if guid and guid in guids:
                existing_id = guids[guid]
            elif can and can in canon:
                existing_id = canon[can]
            elif norm and norm in norms:
                existing_id = norms[norm]
            elif title.strip():
                existing_id = titles.get(_WS_RE.sub(" ", title).strip().lower())

            if existing_id is not None:
                # Refresh the canonical URL if a later feed variant improved it.
                if can:
                    cur = conn.execute(
                        "UPDATE articles SET canonical_url=? WHERE id=? AND canonical_url IS NULL",
                        (can, existing_id),
                    )
                    if cur.rowcount:
                        canon.setdefault(can, existing_id)
                        n2 = normalize_url(can)
                        if n2:
                            norms.setdefault(n2, existing_id)
                continue

            if added >= MAX_ARTICLES_PER_REFRESH:
                continue  # per-refresh insert cap reached; next refresh continues

            chash = content_hash(title, summary, guid or can or norm or f"{source_id}:{now}:{added}")
            try:
                conn.execute(
                    """INSERT INTO articles
                       (source_id, guid, canonical_url, title, summary, author, published_at, discovered_at, read, hash)
                       VALUES (?,?,?,?,?,?,?,?,0,?)""",
                    (source_id, guid, can, title, summary, author, published, now, chash),
                )
            except sqlite3.IntegrityError:
                continue  # global hash unique — identical content already stored (dedup fallback)
            new_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            if guid:
                guids.setdefault(guid, new_id)
            if can:
                canon.setdefault(can, new_id)
                if norm:
                    norms.setdefault(norm, new_id)
            if title.strip():
                titles.setdefault(_WS_RE.sub(" ", title).strip().lower(), new_id)
            added += 1
    return added


def _apply_retention(conn: sqlite3.Connection) -> None:
    settings = all_settings(conn)
    age_hours = settings.get("max_article_age_hours") or 0
    if age_hours > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM articles WHERE COALESCE(published_at, discovered_at) < ?", (cutoff,))
    max_headlines = settings.get("max_headlines") or 0
    if max_headlines > 0:
        conn.execute(
            """DELETE FROM articles WHERE id NOT IN (
                 SELECT id FROM articles ORDER BY COALESCE(published_at, discovered_at) DESC LIMIT ?
               )""",
            (max_headlines,),
        )


# ---------------------------------------------------------------------------
# Background refresher (router lifespan; merged into the app's lifespan)
# ---------------------------------------------------------------------------
_refresher_task: asyncio.Task | None = None


def _due_source_ids(interval_default: int) -> list[int]:
    now = datetime.now(timezone.utc)
    with _db() as conn:
        rows = conn.execute("SELECT id, refresh_interval, last_checked_at FROM sources WHERE enabled=1").fetchall()
    due: list[int] = []
    for row in rows:
        interval = row["refresh_interval"] or interval_default or DEFAULT_REFRESH_INTERVAL
        if not row["last_checked_at"]:
            due.append(row["id"])
            continue
        try:
            checked = datetime.fromisoformat(row["last_checked_at"])
        except ValueError:
            due.append(row["id"])
            continue
        if (now - checked).total_seconds() >= interval:
            due.append(row["id"])
    return due


async def _refresher_loop() -> None:
    while True:
        try:
            with _db() as conn:
                interval_default = int(get_setting(conn, "refresh_interval") or DEFAULT_REFRESH_INTERVAL)
            for source_id in await asyncio.to_thread(_due_source_ids, interval_default):
                async with _get_refresh_lock():
                    await refresh_source(source_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # never let the loop die; per-source errors are already recorded
        await asyncio.sleep(REFRESHER_GRANULARITY)


@asynccontextmanager
async def _router_lifespan(app: Any):
    global _refresher_task
    with suppress(Exception):
        with _db():
            pass  # initialise schema + WAL before the first request
    _refresher_task = asyncio.create_task(_refresher_loop(), name="hermes-newswire-refresher")
    try:
        yield
    finally:
        task, _refresher_task = _refresher_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


router = APIRouter(lifespan=_router_lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "plugin": "hermes-newswire", "version": PLUGIN_VERSION}


@router.get("/state")
def state() -> dict[str, Any]:
    with _db() as conn:
        settings = all_settings(conn)
        counts = {
            "sources": conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"],
            "enabled_sources": conn.execute("SELECT COUNT(*) c FROM sources WHERE enabled=1").fetchone()["c"],
            "articles": conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"],
            "unread": conn.execute("SELECT COUNT(*) c FROM articles WHERE read=0").fetchone()["c"],
        }
    return {"settings": settings, "counts": counts}


@router.get("/sources")
def list_sources() -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM sources ORDER BY created_at, id").fetchall()
        counts: dict[int, int] = {
            r["source_id"]: r["c"] for r in conn.execute(
                "SELECT source_id, COUNT(*) c FROM articles GROUP BY source_id")
        }
    items = []
    for row in rows:
        src = _source_row(row)
        src["article_count"] = counts.get(row["id"], 0)
        items.append(src)
    return {"sources": items}


async def _probe_feed(feed_url: str) -> tuple[bool, dict[str, Any] | None, str, "FetchOutcome | None"]:
    """Fetch a URL and report (is_feed, parsed_feed, content_type, outcome).

    The outcome is returned so callers can persist the etag/last-modified that
    actually belong to the feed response (never a preceding HTML page).
    """
    outcome = await _http_fetch(feed_url)
    ctype = outcome.headers.get("content-type", "")
    if outcome.status != 200:
        return False, None, ctype, outcome
    if not looks_like_feed(outcome.body, ctype):
        return False, None, ctype, outcome
    try:
        feed = parse_feed(outcome.body, ctype)
    except (ET.ParseError, ValueError, json.JSONDecodeError):
        return False, None, ctype, outcome
    return True, feed, ctype, outcome


@router.post("/sources", status_code=201)
async def add_source(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    url = str(payload.get("url") or payload.get("feed_url") or "").strip()
    if not url:
        raise _err(400, "missing_url", "body must include 'url' (feed or site URL)")
    if urlsplit(url).scheme not in ALLOWED_SCHEMES:
        raise _err(400, "bad_scheme", "url must be http(s)")
    name = str(payload.get("name") or "").strip() or None
    category = str(payload.get("category") or "").strip()
    enabled = payload.get("enabled", True)
    enabled = bool(enabled) if isinstance(enabled, bool) else True
    refresh_interval = payload.get("refresh_interval")
    if refresh_interval is not None:
        try:
            refresh_interval = int(refresh_interval)
            if not 30 <= refresh_interval <= 86400:
                raise ValueError
        except (TypeError, ValueError):
            raise _err(400, "bad_refresh_interval", "refresh_interval must be int in [30, 86400]") from None

    # Feed or site? Fetch once; if it's not a feed, discover candidates on the page.
    try:
        outcome = await _http_fetch(url)
    except UnsafeURL as exc:
        # F5: a policy rejection is a client error, not an upstream failure.
        raise _err(400, "unsafe_url", str(exc)) from exc
    except Exception as exc:
        raise _err(502, "fetch_failed", f"could not fetch {url}: {exc}") from exc
    if outcome.status != 200:
        raise _err(502, "fetch_failed", f"HTTP {outcome.status} fetching {url}")

    ctype = outcome.headers.get("content-type", "")
    feed_url = url
    feed: dict[str, Any] | None = None
    if looks_like_feed(outcome.body, ctype):
        try:
            feed = parse_feed(outcome.body, ctype)
        except (ET.ParseError, ValueError, json.JSONDecodeError) as exc:
            raise _err(400, "parse_failed", f"content at {url} looks like a feed but failed to parse: {exc}") from exc
    else:
        candidates = discover_in_html(url, outcome.body)
        errors: list[str] = []
        for cand in candidates[:MAX_DISCOVERY_PROBES]:
            try:
                is_feed, cand_feed, _, cand_outcome = await _probe_feed(cand)
            except Exception as exc:
                errors.append(f"{cand}: {exc}")
                continue
            if is_feed:
                feed_url = cand
                feed = cand_feed
                outcome = cand_outcome
                break
            errors.append(f"{cand}: not a feed")
        if feed is None:
            raise _err(
                400, "no_feed_found",
                f"no feed found at {url} (tried {min(len(candidates), MAX_DISCOVERY_PROBES)} candidates)",
            )

    final_name = name or feed.get("title") or feed_url
    now = _now_iso()
    with _db() as conn:
        dup = conn.execute("SELECT id FROM sources WHERE feed_url=?", (feed_url,)).fetchone()
        if dup:
            raise _err(409, "duplicate", f"source already exists (id {dup['id']}) for {feed_url}")
        icon_url = str(payload.get("icon_url") or "").strip()
        if icon_url:
            try:
                _assert_public_http_url(icon_url)
            except UnsafeURL:
                icon_url = ""  # a bad icon never blocks adding the source
        cur = conn.execute(
            """INSERT INTO sources (name, url, feed_url, enabled, category, created_at, updated_at, refresh_interval, favicon_url)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (final_name, url if url != feed_url else "", feed_url, int(enabled), category, now, now, refresh_interval, icon_url),
        )
        source_id = cur.lastrowid
    added = _insert_articles(source_id, feed_url, feed)
    with _db() as conn:
        if added:
            conn.execute(
                "UPDATE sources SET articles_ever = articles_ever + ? WHERE id=?",
                (added, source_id),
            )
        conn.execute(
            "UPDATE sources SET etag=?, last_modified=?, last_checked_at=?, last_success_at=?, updated_at=? WHERE id=?",
            (outcome.headers.get("etag") or None, outcome.headers.get("last-modified") or None,
             now, now, now, source_id),
        )
        _apply_retention(conn)
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    return {"source": _source_row(row), "articles_added": added}


@router.patch("/sources/{source_id}")
def patch_source(source_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    allowed = {"name", "url", "feed_url", "enabled", "category", "refresh_interval"}
    unknown = set(payload) - allowed
    if unknown:
        raise _err(400, "unknown_fields", f"unknown source fields: {sorted(unknown)}")
    now = _now_iso()
    with _db() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise _err(404, "not_found", f"source {source_id} not found")
        sets, vals = [], []
        if "name" in payload:
            sets.append("name=?"); vals.append(str(payload["name"]).strip() or row["name"])
        if "category" in payload:
            sets.append("category=?"); vals.append(str(payload["category"]).strip())
        if "enabled" in payload:
            sets.append("enabled=?"); vals.append(1 if payload["enabled"] else 0)
        if "refresh_interval" in payload:
            ri = payload["refresh_interval"]
            if ri is not None:
                try:
                    ri = int(ri)
                    if not 30 <= ri <= 86400:
                        raise ValueError
                except (TypeError, ValueError):
                    raise _err(400, "bad_refresh_interval", "refresh_interval must be int in [30, 86400]") from None
            sets.append("refresh_interval=?"); vals.append(ri)
        new_feed = payload.get("feed_url")
        if new_feed:
            new_feed = str(new_feed).strip()
            if urlsplit(new_feed).scheme not in ALLOWED_SCHEMES:
                raise _err(400, "bad_scheme", "feed_url must be http(s)")
            dup = conn.execute("SELECT id FROM sources WHERE feed_url=? AND id<>?", (new_feed, source_id)).fetchone()
            if dup:
                raise _err(409, "duplicate", f"another source already uses {new_feed}")
            sets.append("feed_url=?"); vals.append(new_feed)
            # Changing the feed URL invalidates conditional-GET state.
            sets += ["etag=NULL", "last_modified=NULL", "last_checked_at=NULL"]
        if "url" in payload:
            new_site = str(payload["url"]).strip()
            if new_site and urlsplit(new_site).scheme not in ALLOWED_SCHEMES:
                raise _err(400, "bad_scheme", "url must be http(s)")
            sets.append("url=?"); vals.append(new_site)
        if not sets:
            raise _err(400, "empty_patch", "no updatable fields in payload")
        sets.append("updated_at=?"); vals.append(now)
        vals.append(source_id)
        conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", vals)
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    return {"source": _source_row(row)}


@router.delete("/sources/{source_id}")
def delete_source(source_id: int) -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise _err(404, "not_found", f"source {source_id} not found")
        deleted = conn.execute("DELETE FROM articles WHERE source_id=?", (source_id,)).rowcount
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    return {"deleted": source_id, "articles_removed": deleted}


async def _source_icon_bytes(source_id: int) -> tuple[bytes, str]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise _err(404, "not_found", f"source {source_id} not found")
    remote = _favicon_remote_url(row)
    if not remote:
        raise _err(404, "no_icon", "source has no icon URL")
    try:
        return await _fetch_icon(remote)
    except UnsafeURL as exc:
        raise _err(400, "unsafe_url", str(exc)) from exc
    except NotImage as exc:
        raise _err(502, "not_image", str(exc)) from exc
    except Exception as exc:
        raise _err(502, "fetch_failed", f"could not fetch icon: {exc}") from exc


@router.get("/sources/{source_id}/icon.json")
async def source_icon_json(source_id: int) -> dict[str, str]:
    """JSON twin of /icon for ctx.rest (which JSON-decodes responses)."""
    body, ctype = await _source_icon_bytes(source_id)
    return _icon_payload(body, ctype)


@router.get("/sources/{source_id}/icon")
async def source_icon(source_id: int) -> Response:
    """Raster bytes fetched through the pinned transport, never a remote URL."""
    body, ctype = await _source_icon_bytes(source_id)
    return Response(
        content=body,
        media_type=ctype,
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/sources/{source_id}/refresh")
async def refresh_one(source_id: int) -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute("SELECT id, enabled FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise _err(404, "not_found", f"source {source_id} not found")
    async with _get_refresh_lock():
        result = await refresh_source(source_id)
    with _db() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    return {"result": result, "source": _source_row(row)}


@router.get("/articles")
def list_articles(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    source_id: int | None = Query(None),
    unread: bool = Query(False),
    include_summary: bool = Query(True),
    include_disabled_sources: bool = Query(False),
) -> dict[str, Any]:
    where, vals = [], []
    if source_id is not None:
        where.append("a.source_id=?"); vals.append(source_id)
    if unread:
        where.append("a.read=0")
    # F1 (QA HOLD): a disabled source must vanish from article lists — its
    # cached articles stay in the DB (offline value) but are excluded until
    # the source is re-enabled. Sources tab opts in explicitly.
    if not include_disabled_sources:
        where.append("s.enabled=1")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _db() as conn:
        total = conn.execute(
            f"""SELECT COUNT(*) c FROM articles a
                JOIN sources s ON s.id = a.source_id {clause}""",
            vals
        ).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT a.*, s.name AS source_name, s.url AS src_url, s.feed_url AS src_feed_url,
                       s.favicon_url AS stored_favicon
                FROM articles a
                JOIN sources s ON s.id = a.source_id
                {clause}
                ORDER BY COALESCE(a.published_at, a.discovered_at) DESC, a.id DESC
                LIMIT ? OFFSET ?""",
            vals + [limit, offset],
        ).fetchall()
    items = []
    for r in rows:
        item = _article_row(r, include_summary=include_summary, source_name=r["source_name"])
        item["favicon_url"] = _favicon_for_source(r)
        items.append(item)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/articles/{article_id}/read")
def mark_read(article_id: int, payload: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
    read = True
    if isinstance(payload, dict) and "read" in payload:
        read = bool(payload["read"])
    with _db() as conn:
        cur = conn.execute("UPDATE articles SET read=? WHERE id=?", (1 if read else 0, article_id))
        if cur.rowcount == 0:
            raise _err(404, "not_found", f"article {article_id} not found")
    return {"id": article_id, "read": read}


@router.post("/articles/read-all")
def mark_all_read(payload: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
    source_id = payload.get("source_id") if isinstance(payload, dict) else None
    with _db() as conn:
        if source_id is not None:
            cur = conn.execute("UPDATE articles SET read=1 WHERE read=0 AND source_id=?", (source_id,))
        else:
            cur = conn.execute("UPDATE articles SET read=1 WHERE read=0")
    return {"updated": cur.rowcount}


@router.get("/settings")
def get_settings_route() -> dict[str, Any]:
    with _db() as conn:
        return {"settings": all_settings(conn)}


def _validate_setting(key: str, value: Any) -> Any:
    if key not in DEFAULT_SETTINGS:
        raise _err(400, "unknown_setting", f"unknown setting: {key}")
    if key in {"ticker_enabled", "pause_on_hover", "show_source", "relative_time", "only_unread"}:
        if not isinstance(value, bool):
            raise _err(400, "bad_type", f"{key} must be a boolean")
        return value
    if key == "ticker_grouping":
        if value not in {"newest", "source", "unread_first"}:
            raise _err(400, "bad_grouping", "ticker_grouping must be one of newest|source|unread_first")
        return value
    if key == "open_article_behavior":
        if value not in {"internal", "external"}:
            raise _err(400, "bad_open_behavior", "open_article_behavior must be 'internal' or 'external'")
        return value
    if key == "ticker_speed":
        if value not in _TICKER_SPEEDS:
            raise _err(400, "bad_speed", f"ticker_speed must be one of {sorted(_TICKER_SPEEDS)}")
        return value
    if key == "ticker_font_size":
        if isinstance(value, bool) or not isinstance(value, int):
            # int(11.5) would silently truncate — require a true int.
            raise _err(400, "bad_type", "ticker_font_size must be an integer (px)")
        if not 9 <= value <= 20:
            raise _err(400, "bad_range", "ticker_font_size must be in [9, 20]")
        return value
    if key in {"refresh_interval", "max_article_age_hours", "max_headlines"}:
        try:
            iv = int(value)
        except (TypeError, ValueError):
            raise _err(400, "bad_type", f"{key} must be an integer") from None
        if iv < 0:
            raise _err(400, "bad_range", f"{key} must be >= 0")
        if key == "refresh_interval" and not 30 <= iv <= 86400:
            raise _err(400, "bad_range", "refresh_interval must be in [30, 86400]")
        return iv
    raise _err(400, "unknown_setting", f"unknown setting: {key}")  # unreachable


@router.patch("/settings")
def patch_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise _err(400, "empty_patch", "settings payload must be a non-empty object")
    validated = {k: _validate_setting(k, v) for k, v in payload.items()}
    with _db() as conn:
        for k, v in validated.items():
            set_setting(conn, k, v)
        settings = all_settings(conn)
    return {"settings": settings}


@router.post("/refresh-all")
async def refresh_all() -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute("SELECT id FROM sources WHERE enabled=1 ORDER BY id").fetchall()
    results = []
    async with _get_refresh_lock():
        for row in rows:
            results.append(await refresh_source(row["id"]))
    return {"results": results}


@router.get("/opml/export")
def opml_export() -> Response:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM sources ORDER BY created_at, id").fetchall()
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<opml version="2.0">',
             "  <head><title>Hermes Newswire feeds</title></head>",
             "  <body>"]
    for row in rows:
        attrs = [
            'type="rss"',
            f'text="{_xml_escape(row["name"])}"',
            f'xmlUrl="{_xml_escape(row["feed_url"])}"',
        ]
        if row["url"]:
            attrs.append(f'htmlUrl="{_xml_escape(row["url"])}"')
        if row["category"]:
            attrs.append(f'category="{_xml_escape(row["category"])}"')
        lines.append("    <outline " + " ".join(attrs) + "/>")
    lines += ["  </body>", "</opml>", ""]
    return Response("\n".join(lines), media_type="text/xml",
                    headers={"Content-Disposition": 'attachment; filename="hermes-newswire.opml"'})


@router.get("/opml/export.json")
def opml_export_json() -> dict[str, Any]:
    """JSON twin of /opml/export.

    The desktop plugin REST bridge (Electron fetchJson) only resolves JSON
    bodies — a text/xml response rejects with "Invalid JSON", so the
    renderer's Export button can't consume the raw route above. This route
    wraps the exact same document for ctx.rest callers.
    """
    return {"xml": opml_export().body.decode("utf-8")}


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


@router.post("/opml/import")
async def opml_import(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    xml_text = payload.get("xml")
    if not xml_text and isinstance(payload.get("opml"), str):
        xml_text = payload["opml"]
    if not xml_text or not str(xml_text).strip():
        raise _err(400, "missing_xml", "body must include 'xml' (OPML document text)")
    try:
        root = ET.fromstring(str(xml_text))
    except ET.ParseError as exc:
        raise _err(400, "bad_opml", f"invalid OPML: {exc}") from exc
    entries: list[tuple[str, str, str, str]] = []  # (xmlUrl, title, category, htmlUrl)
    seen_urls: set[str] = set()
    for outline in root.iter("outline"):
        feed_url = (outline.get("xmlUrl") or outline.get("xmlurl") or "").strip()
        if not feed_url or feed_url in seen_urls:
            continue
        if urlsplit(feed_url).scheme not in ALLOWED_SCHEMES:
            continue
        seen_urls.add(feed_url)
        title = (outline.get("title") or outline.get("text") or "").strip()
        category = (outline.get("category") or "").strip()
        html_url = (outline.get("htmlUrl") or outline.get("htmlurl") or "").strip()
        entries.append((feed_url, title, category, html_url))

    added, skipped, errors = [], [], []
    now = _now_iso()
    for feed_url, title, category, html_url in entries:
        with _db() as conn:
            dup = conn.execute("SELECT id FROM sources WHERE feed_url=?", (feed_url,)).fetchone()
            if dup:
                skipped.append(feed_url)
                continue
        # Verify the feed is real and grab its title + first articles.
        try:
            is_feed, feed, _, feed_outcome = await _probe_feed(feed_url)
        except Exception as exc:
            errors.append({"feed_url": feed_url, "error": str(exc)[:300]})
            continue
        if not is_feed:
            errors.append({"feed_url": feed_url, "error": "not a feed"})
            continue
        final_title = title or (feed or {}).get("title") or feed_url
        with _db() as conn:
            cur = conn.execute(
                """INSERT INTO sources (name, url, feed_url, enabled, category, created_at, updated_at)
                   VALUES (?,?,?,1,?,?,?)""",
                (final_title, html_url, feed_url, category, now, now),
            )
            source_id = cur.lastrowid
        n = _insert_articles(source_id, feed_url, feed or {"entries": []})
        if feed_outcome is not None:
            with _db() as conn:
                conn.execute(
                    "UPDATE sources SET etag=?, last_modified=?, last_checked_at=?, last_success_at=?, updated_at=? WHERE id=?",
                    (feed_outcome.headers.get("etag") or None, feed_outcome.headers.get("last-modified") or None,
                     now, now, now, source_id),
                )
                _apply_retention(conn)
        added.append({"feed_url": feed_url, "id": source_id, "name": final_title, "articles_added": n})
    return {"added": added, "skipped": skipped, "errors": errors}


@router.post("/discover")
async def discover(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    url = str(payload.get("url") or "").strip()
    if not url:
        raise _err(400, "missing_url", "body must include 'url'")
    try:
        outcome = await _http_fetch(url)
    except UnsafeURL as exc:
        # F5: a policy rejection is a client error, not an upstream failure.
        raise _err(400, "unsafe_url", str(exc)) from exc
    except Exception as exc:
        raise _err(502, "fetch_failed", f"could not fetch {url}: {exc}") from exc
    if outcome.status != 200:
        raise _err(502, "fetch_failed", f"HTTP {outcome.status} fetching {url}")

    candidates = discover_in_html(url, outcome.body)
    results = []
    for cand in candidates[:MAX_DISCOVERY_PROBES]:
        entry: dict[str, Any] = {"url": cand}
        try:
            outcome = await _http_fetch(cand)
        except Exception as exc:
            entry["is_feed"] = False
            entry["error"] = str(exc)[:300]
            results.append(entry)
            continue
        ctype = outcome.headers.get("content-type", "")
        if outcome.status == 200 and looks_like_feed(outcome.body, ctype):
            entry["is_feed"] = True
            try:
                feed = parse_feed(outcome.body, ctype)
                entry["title"] = feed.get("title")
                entry["format"] = feed.get("format")
            except (ET.ParseError, ValueError, json.JSONDecodeError):
                entry["title"] = None
        else:
            entry["is_feed"] = False
        results.append(entry)
    return {"url": url, "candidates": results}


# ---------------------------------------------------------------------------
# Feed search (Feedly public index — no API key; fallback to discovery)
# ---------------------------------------------------------------------------
FEEDLY_SEARCH = "https://cloud.feedly.com/v3/search/feeds"
MAX_SEARCH_RESULTS = 12


@router.post("/search")
async def search_feeds(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Search feeds by topic or site name.

    Primary: Feedly's public feed search (no key). Fallback: treat the query
    as a bare hostname (e.g. "arstechnica.com") and run classic discovery on
    https://<query>/. Every candidate feed URL is re-validated through the
    SSRF gate before being returned.
    """
    q = str(payload.get("query") or payload.get("q") or "").strip()
    if not q:
        raise _err(400, "missing_query", "body must include 'query'")

    results: list[dict[str, Any]] = []

    # A URL-ish query skips the index and goes straight to discovery.
    if "://" in q or q.startswith("www.") or "." in q.split()[0] and " " not in q:
        try:
            url = q if "://" in q else f"https://{q}"
            await _assert_public_http_url_sync(url)
            disc = await _discover_url(url)
            return {"query": q, "results": disc, "via": "discovery"}
        except UnsafeURL as exc:
            raise _err(400, "unsafe_url", str(exc)) from exc

    # Topic search via Feedly's public index.
    try:
        outcome = await _http_fetch(
            f"{FEEDLY_SEARCH}?{urlencode({'query': q, 'count': MAX_SEARCH_RESULTS, 'locale': 'en'})}"
        )
        if outcome.status == 200:
            doc = json.loads(outcome.body.decode("utf-8", "replace"))
            for r in doc.get("results", [])[:MAX_SEARCH_RESULTS]:
                feed_id = str(r.get("feedId") or "")
                if not feed_id.startswith("feed/"):
                    continue
                feed_url = feed_id[len("feed/"):]
                # Same trust boundary as every other URL we hand the client.
                try:
                    await _assert_public_http_url_sync(feed_url)
                except UnsafeURL:
                    continue
                results.append({
                    "title": strip_html(r.get("title")) or feed_url,
                    "feed_url": feed_url,
                    "website": (r.get("website") or "")[:300],
                    "description": strip_html(r.get("description") or "")[:300],
                    "subscribers": r.get("subscribers") or 0,
                    "language": r.get("language") or "",
                    "icon_url": ((r.get("iconUrl") or "") if await _is_safe_image_url(r.get("iconUrl")) else ""),
                })
    except Exception:
        pass  # index unreachable → fall through to discovery fallback

    if results:
        return {"query": q, "results": results, "via": "feedly"}

    # Fallback: query as bare hostname (e.g. "nasa.gov").
    if "." in q and " " not in q:
        try:
            url = f"https://{q}"
            await _assert_public_http_url_sync(url)
            disc = await _discover_url(q if "://" in q else url)
            return {"query": q, "results": disc, "via": "discovery"}
        except UnsafeURL as exc:
            raise _err(400, "unsafe_url", str(exc)) from exc

    return {"query": q, "results": [], "via": "none"}


async def _is_safe_image_url(u: str | None) -> bool:
    if not u:
        return False
    try:
        await _assert_public_http_url_sync(u)
        return True
    except UnsafeURL:
        return False


async def _discover_url(url: str) -> list[dict[str, Any]]:
    """Shared discovery core for /discover and /search (hostname form)."""
    try:
        outcome = await _http_fetch(url)
    except Exception as exc:
        raise _err(502, "fetch_failed", f"could not fetch {url}: {exc}") from exc
    if outcome.status != 200:
        raise _err(502, "fetch_failed", f"HTTP {outcome.status} fetching {url}")
    candidates = discover_in_html(url, outcome.body)
    results: list[dict[str, Any]] = []
    for cand in candidates[:MAX_DISCOVERY_PROBES]:
        entry: dict[str, Any] = {"url": cand, "feed_url": cand}
        try:
            outcome = await _http_fetch(cand)
        except Exception as exc:
            entry["is_feed"] = False
            entry["error"] = str(exc)[:300]
            results.append(entry)
            continue
        ctype = outcome.headers.get("content-type", "")
        if outcome.status == 200 and looks_like_feed(outcome.body, ctype):
            entry["is_feed"] = True
            try:
                feed = parse_feed(outcome.body, ctype)
                entry["title"] = feed.get("title")
                entry["format"] = feed.get("format")
            except (ET.ParseError, ValueError, json.JSONDecodeError):
                entry["title"] = None
        else:
            entry["is_feed"] = False
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Open in Hermes preview pane (internal browser)
# ---------------------------------------------------------------------------
@router.post("/preview")
async def open_in_preview(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Open an article in the desktop's in-app preview pane.

    Emits the same ``preview.open`` gateway event the app's own open_preview
    tool uses (tools/desktop_ui.py emitter), broadcast to live transports so
    it works from a REST context with no turn-bound session. The renderer's
    gate opens the pane for the visible window; never steals focus for
    background content.
    """
    url = str(payload.get("url") or "").strip()
    label = str(payload.get("label") or "").strip()
    if not url:
        raise _err(400, "missing_url", "body must include 'url'")
    try:
        _assert_public_http_url(url)
    except UnsafeURL as exc:
        raise _err(400, "unsafe_url", str(exc)) from exc

    from tui_gateway.server import _broadcast_global_event

    _broadcast_global_event(
        "preview.open", {"url": url, "label": label or url}
    )
    return {"opened": url}
