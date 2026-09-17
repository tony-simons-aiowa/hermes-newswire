"""Adversarial DNS-rebinding regression coverage (issue #2).

These tests attack the validation→connection boundary itself, not just the
URL gate. Every test installs a fake network backend BELOW
``_PinnedIPBackend`` — i.e. exactly where httpcore would otherwise open a
real socket and do its own (uncontrolled) DNS resolution — so we observe
precisely which IP each connection attempt dials.

Attack model simulated: the validation resolution returns a public IP, and
any *later* resolution returns 127.0.0.1 (DNS rebinding). Contract under
test: the socket may only ever connect to an address from the validated set
that was pinned for that exact hop; unpinned hosts fail closed.
"""

from __future__ import annotations

import asyncio
import socket as socket_mod

import httpx
import pytest

from conftest import RSS2


# ---------------------------------------------------------------------------
# Test doubles for the layer beneath the pin gate
# ---------------------------------------------------------------------------

class RefusingDialer:
    """Records connect_tcp targets and refuses them all (no sockets)."""

    def __init__(self):
        self.ips: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        import httpcore

        self.ips.append(host)
        raise httpcore.ConnectError(f"refusing {host}:{port}")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("unix sockets must never be dialed")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class SockPairStream:
    """httpcore AsyncNetworkStream over one end of a non-blocking socketpair."""

    def __init__(self, sock):
        self._sock = sock
        self._loop = asyncio.get_running_loop()

    async def read(self, max_bytes, timeout=None):
        while True:
            try:
                data = self._sock.recv(max_bytes)
                return data  # b"" on EOF — httpcore treats as connection close
            except BlockingIOError:
                await self._wait_readable()

    async def _wait_readable(self):
        fut = self._loop.create_future()

        def _on_readable():
            if not fut.done():
                fut.set_result(None)

        loop, sock = self._loop, self._sock
        loop.add_reader(sock, _on_readable)
        try:
            await fut
        finally:
            loop.remove_reader(sock)

    async def write(self, buffer, timeout=None):
        await self._loop.sock_sendall(self._sock, buffer)

    async def aclose(self):
        try:
            self._sock.close()
        except OSError:
            pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        raise AssertionError("plain-HTTP test dialer asked to start_tls")

    def get_extra_info(self, info):
        return None


class ServingDialer:
    """Serves scripted plain-HTTP responses over real socketpairs.

    ``handler(host_ip, method, path, host_header) -> (status, headers, body)``
    decides the response per request. Records every dialed IP, request
    target, and observed Host header so tests can assert both pinning and
    hostname semantics.
    """

    _REASONS = {200: "OK", 301: "Moved Permanently", 302: "Found",
                303: "See Other", 307: "Temporary Redirect", 308: "Permanent Redirect"}

    def __init__(self, handler):
        self.handler = handler
        self.dials: list[tuple[str, int]] = []
        self.requests: list[tuple[str, str, str]] = []  # (dialed_ip, target, Host header)

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        self.dials.append((host, int(port)))
        loop = asyncio.get_running_loop()
        client_sock, server_sock = socket_mod.socketpair()
        client_sock.setblocking(False)
        server_sock.setblocking(False)
        loop.create_task(self._serve(server_sock, host))
        return SockPairStream(client_sock)

    async def _serve(self, sock, dialed_ip):
        loop = asyncio.get_running_loop()
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = await loop.sock_recv(sock, 65536)
                if not chunk:
                    return
                data += chunk
                if len(data) > 1 << 20:
                    return
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            lines = head.split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            host_header = next((l.split(":", 1)[1].strip() for l in lines[1:]
                                if l.lower().startswith("host:")), "")
            self.requests.append((dialed_ip, target, host_header))
            status, headers, body = self.handler(dialed_ip, method, target, host_header)
            reason = self._REASONS.get(status, "OK")
            if isinstance(body, str):
                body = body.encode()
            resp_lines = [f"HTTP/1.1 {status} {reason}"]
            resp_lines += [f"{k}: {v}" for k, v in headers.items()]
            resp_lines.append(f"Content-Length: {len(body)}")
            resp_lines.append("Connection: close")
            blob = ("\r\n".join(resp_lines) + "\r\n\r\n").encode("latin-1") + body
            await loop.sock_sendall(sock, blob)
        except Exception:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("unix sockets must never be dialed")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def install(plugin, monkeypatch, dialer):
    """Point the pool's network backend at ``dialer`` under the pin gate."""

    def build():
        client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(plugin.TOTAL_TIMEOUT, connect=plugin.CONNECT_TIMEOUT),
            headers={"User-Agent": plugin.USER_AGENT, "Accept": "*/*"},
        )
        backend = plugin._PinnedIPBackend(dialer)
        client._transport._pool._network_backend = backend
        client._newswire_pin_backend = backend
        return client

    monkeypatch.setattr(plugin, "_build_async_client", build)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def rss_ok(_ip, _method, _target, _host):
    return 200, {"Content-Type": "application/rss+xml"}, RSS2


# ---------------------------------------------------------------------------
# The adversarial core: DNS changes its answer between validation and connect
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_rebind_second_answer_never_dialed(plugin, monkeypatch):
    """Validation sees 93.184.216.34; every LATER resolution returns 127.0.0.1.

    If any code path re-resolved the hostname when opening the socket (the
    pre-fix behavior), that lookup happens after validation and yields the
    loopback — the connection would land on localhost. With pinning there is
    no second lookup: the socket dials the validated IP only, and the fetch
    completes against it.
    """
    calls = {"n": 0}

    def rebind_dns(host):
        calls["n"] += 1
        return ["93.184.216.34"] if calls["n"] == 1 else ["127.0.0.1"]

    monkeypatch.setattr(plugin, "_resolve_host_sync", rebind_dns)
    dialer = ServingDialer(rss_ok)
    install(plugin, monkeypatch, dialer)

    out = await plugin._http_fetch("http://rebind.example/feed.xml")
    assert out.status == 200
    assert out.body == RSS2
    assert calls["n"] == 1                       # exactly one resolution: validation's
    assert dialer.dials == [("93.184.216.34", 80)]  # and the socket used it
    assert all("127.0.0.1" != ip for ip, _ in dialer.dials)


@pytest.mark.anyio
async def test_rebind_on_redirect_hop_never_dialed(plugin, monkeypatch):
    """Same attack on hop 2: the redirect target's validated IP is dialed,
    even though a post-validation re-resolution would now answer loopback."""
    dns_state = {"example.com": 0, "other.example": 0}

    def dns(host):
        dns_state[host] += 1
        first = dns_state[host] == 1
        return [{"example.com": ["93.184.216.34"],
                 "other.example": ["8.8.8.8"]}[host][0] if first else "127.0.0.1"]

    monkeypatch.setattr(plugin, "_resolve_host_sync", dns)

    def handler(ip, method, target, host):
        if target == "/old":
            return 302, {"Location": "http://other.example/feed.xml"}, b""
        return 200, {"Content-Type": "application/rss+xml"}, RSS2

    dialer = ServingDialer(handler)
    install(plugin, monkeypatch, dialer)

    out = await plugin._http_fetch("http://example.com/old")
    assert out.status == 200
    assert out.body == RSS2
    assert out.url == "http://other.example/feed.xml"
    assert [ip for ip, _ in dialer.dials] == ["93.184.216.34", "8.8.8.8"]


@pytest.mark.anyio
async def test_unpinned_host_fails_closed(plugin, monkeypatch):
    """No validation → no connection. A host with no pin entry is refused
    before any socket or DNS access (proves the structural invariant)."""
    dialer = RefusingDialer()
    install(plugin, monkeypatch, dialer)

    client = plugin._build_async_client()
    assert client._newswire_pin_backend is not None
    client._newswire_pin_backend.pin("trusted.example", ["93.184.216.35"])
    try:
        async with client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://untrusted.example/feed")
    finally:
        await client.aclose()
    assert dialer.ips == []


# ---------------------------------------------------------------------------
# Fail-closed when the pinned transport cannot be installed (review follow-up)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_client_construction_fails_closed_on_missing_seam(plugin, monkeypatch):
    """_build_async_client must refuse to hand out an unpinnable client.

    Simulates an incompatible httpx/httpcore build where the pool seam
    (``_transport._pool._network_backend``) is gone: installation returns
    None and client construction must raise rather than return an ordinary
    httpx client that would re-resolve hostnames itself when dialing.
    """
    monkeypatch.setattr(plugin, "_install_pin_backend", lambda client: None)
    with pytest.raises(RuntimeError, match="IP-pinning transport could not be installed"):
        plugin._build_async_client()


@pytest.mark.anyio
async def test_http_fetch_fails_closed_without_pin_backend(plugin, monkeypatch):
    """A client lacking the pin backend cannot fetch — no fallback dial.

    The pre-fix fail-open path: a client whose transport could not be pinned
    sailed through validation and then connected with ordinary httpx
    behavior (validate -> re-resolve -> connect), restoring the rebind
    window. The fetch must now refuse BEFORE resolving DNS or opening any
    socket, and must never fall back to the pre-fix behavior.
    """
    dialer = RefusingDialer()
    resolved: list[str] = []

    def recording_dns(host):
        resolved.append(host)
        return ["93.184.216.34"]

    monkeypatch.setattr(plugin, "_resolve_host_sync", recording_dns)

    def build():
        client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(plugin.TOTAL_TIMEOUT, connect=plugin.CONNECT_TIMEOUT),
            headers={"User-Agent": plugin.USER_AGENT, "Accept": "*/*"},
        )
        # no _install_pin_backend call, no _newswire_mock_transport flag:
        # exactly what an httpx internals change would leave us with.
        return client

    monkeypatch.setattr(plugin, "_build_async_client", build)

    with pytest.raises(RuntimeError, match="IP-pinning transport unavailable"):
        await plugin._http_fetch("https://unpinnable.example/feed")
    assert dialer.ips == []   # no outbound socket was opened
    assert resolved == []     # no DNS resolution occurred — not even the gate's


@pytest.mark.anyio
async def test_production_client_installs_backend_on_this_env(plugin):
    """Sanity: the real production path still installs the backend here.

    Guards against the fail-closed raising accidentally on the current
    httpx/httpcore (i.e. the guard is reachable only when the seam is
    genuinely absent, not always).
    """
    client = plugin._build_async_client()
    try:
        assert isinstance(client._newswire_pin_backend, plugin._PinnedIPBackend)
        assert client._transport._pool._network_backend is client._newswire_pin_backend
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_mock_transport_seam_still_allowed(plugin, monkeypatch):
    """The explicit test seam (flagged MockTransport) keeps working.

    MockTransport never opens sockets, so a mock-flagged client may pass the
    URL gate without a pin backend — but it must still fail if some future
    change made the mock flag meaningless (no transport at all).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss version='2.0'><channel/></rss>")

    def build():
        client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(plugin.TOTAL_TIMEOUT, connect=plugin.CONNECT_TIMEOUT),
            headers={"User-Agent": plugin.USER_AGENT, "Accept": "*/*"},
            transport=httpx.MockTransport(handler),
        )
        client._newswire_mock_transport = True
        return client

    monkeypatch.setattr(plugin, "_build_async_client", build)
    monkeypatch.setattr(plugin, "_resolve_host_sync", lambda host: ["93.184.216.34"])
    out = await plugin._http_fetch("https://mockflag.example/feed")
    assert out.status == 200




# ---------------------------------------------------------------------------
# Redirect hops: independent resolution + validation + pinning
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_redirect_public_to_private_blocked_before_dial(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_resolve_host_sync",
                        lambda host: ["10.0.0.5"] if host == "evil.example" else ["93.184.216.34"])

    def handler(ip, method, target, host):
        if host == "example.com":
            return 302, {"Location": "http://evil.example/steal"}, b""
        return 200, {}, b"should never be served"

    dialer = ServingDialer(handler)
    install(plugin, monkeypatch, dialer)

    with pytest.raises(plugin.UnsafeURL, match="non-public"):
        await plugin._http_fetch("http://example.com/old")
    assert [ip for ip, _ in dialer.dials] == ["93.184.216.34"]  # hop 1 only
    assert all("10.0.0.5" != ip for ip, _ in dialer.dials)


@pytest.mark.anyio
async def test_redirect_hop_pins_are_per_hostname(plugin, monkeypatch):
    """Hop 1's approval is never reused for a different hostname: the pin
    table holds distinct validated sets per host, filled per hop."""
    dns_map = {"example.com": ["93.184.216.34"], "other.example": ["8.8.8.8"]}
    monkeypatch.setattr(plugin, "_resolve_host_sync", lambda h: dns_map.get(h, ["104.16.132.229"]))
    dialer = RefusingDialer()
    install(plugin, monkeypatch, dialer)

    client = plugin._build_async_client()
    pin = client._newswire_pin_backend
    try:
        await plugin._validate_and_pin("http://example.com/old", pin)
        await plugin._validate_and_pin("http://other.example/feed.xml", pin)
        # a literal-IP hop pins to itself
        await plugin._validate_and_pin("http://9.9.9.9/x", pin)
    finally:
        await client.aclose()
    assert pin._pins["example.com"] == ("93.184.216.34",)
    assert pin._pins["other.example"] == ("8.8.8.8",)
    assert pin._pins["9.9.9.9"] == ("9.9.9.9",)


# ---------------------------------------------------------------------------
# Multiple DNS answers: fallback only within the validated set
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_multi_answer_fallback_within_validated_set(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_resolve_host_sync",
                        lambda host: ["93.184.216.34", "8.8.8.8"])

    class FirstRefusesThenServes(ServingDialer):
        def __init__(self):
            super().__init__(rss_ok)
            self.refused: list[str] = []

        async def connect_tcp(self, host, port, **kw):
            if not self.refused:
                self.refused.append(host)
                import httpcore

                raise httpcore.ConnectError(f"first candidate down: {host}")
            return await super().connect_tcp(host, port, **kw)

    dialer = FirstRefusesThenServes()
    install(plugin, monkeypatch, dialer)

    out = await plugin._http_fetch("http://multi.example/feed.xml")
    assert out.status == 200
    assert dialer.refused == ["93.184.216.34"]
    assert [ip for ip, _ in dialer.dials] == ["8.8.8.8"]  # 2nd validated answer


@pytest.mark.anyio
async def test_multi_answer_with_blocked_member_rejected(plugin, monkeypatch):
    """A mixed public+link-local answer set is rejected outright (no dial)."""
    monkeypatch.setattr(plugin, "_resolve_host_sync",
                        lambda host: ["93.184.216.34", "169.254.169.254"])
    dialer = RefusingDialer()
    install(plugin, monkeypatch, dialer)
    with pytest.raises(plugin.UnsafeURL, match="non-public"):
        await plugin._http_fetch("http://mixed.example/feed.xml")
    assert dialer.ips == []


# ---------------------------------------------------------------------------
# Literal IPs and IPv6
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_literal_public_ip_pins_to_itself(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_resolve_host_sync", lambda host: ["0.0.0.0"])  # must not be consulted
    dialer = RefusingDialer()
    install(plugin, monkeypatch, dialer)
    with pytest.raises(RuntimeError, match="network error"):
        await plugin._http_fetch("http://93.184.216.34/feed")
    assert dialer.ips == ["93.184.216.34"]


@pytest.mark.anyio
async def test_literal_private_ips_rejected_before_dial(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_resolve_host_sync", lambda host: ["93.184.216.34"])
    dialer = RefusingDialer()
    install(plugin, monkeypatch, dialer)
    for url in (
        "http://10.1.2.3/feed",
        "http://192.168.1.1/feed",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/feed",
        "http://[::1]/feed",
        "http://[fe80::1]/feed",
    ):
        with pytest.raises(plugin.UnsafeURL):
            await plugin._http_fetch(url)
    assert dialer.ips == []


@pytest.mark.anyio
async def test_public_ipv6_pins_ipv6(plugin, monkeypatch):
    v6 = "2606:2800:220:1:248:1893:25c8:1946"  # public (2001:db8-external) space
    monkeypatch.setattr(plugin, "_resolve_host_sync", lambda host: [v6])
    dialer = RefusingDialer()
    install(plugin, monkeypatch, dialer)
    with pytest.raises(RuntimeError, match="network error"):
        await plugin._http_fetch("http://v6.example/feed")
    assert dialer.ips == [v6]


@pytest.mark.anyio
async def test_private_ipv6_resolution_rejected(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_resolve_host_sync", lambda host: ["fd00::5"])
    dialer = RefusingDialer()
    install(plugin, monkeypatch, dialer)
    with pytest.raises(plugin.UnsafeURL, match="non-public"):
        await plugin._http_fetch("http://v6private.example/feed")
    assert dialer.ips == []


# ---------------------------------------------------------------------------
# Hostname semantics survive pinning: SNI, Host header
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_sni_is_original_hostname_not_the_pinned_ip(plugin, monkeypatch):
    """For https, the TLS handshake must name the ORIGINAL hostname (SNI and
    certificate verification), while the TCP dial goes to the validated IP."""
    seen: dict[str, str] = {}

    class ProbeStream:
        async def read(self, max_bytes, timeout=None):
            return b""

        async def write(self, buffer, timeout=None):
            pass

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            import httpcore

            seen["sni"] = server_hostname
            raise httpcore.ConnectError("probe complete")

        async def aclose(self):
            pass

        def get_extra_info(self, info):
            return None

    class ProbeDialer:
        async def connect_tcp(self, host, port, timeout=None, local_address=None,
                              socket_options=None):
            seen["dial_ip"] = host
            return ProbeStream()

        async def connect_unix_socket(self, path, timeout=None, socket_options=None):
            raise AssertionError("unix sockets must never be dialed")

        async def sleep(self, seconds):
            await asyncio.sleep(seconds)

    monkeypatch.setattr(plugin, "_resolve_host_sync", lambda host: ["93.184.216.55"])
    install(plugin, monkeypatch, ProbeDialer())

    with pytest.raises(RuntimeError, match="network error"):
        await plugin._http_fetch("https://sni-observed.example/feed")
    assert seen["dial_ip"] == "93.184.216.55"          # TCP destination: validated IP
    assert seen["sni"] == "sni-observed.example"       # TLS: original hostname


@pytest.mark.anyio
async def test_host_header_is_original_hostname(plugin, monkeypatch):
    """The HTTP Host header keeps the original hostname on every hop."""
    monkeypatch.setattr(plugin, "_resolve_host_sync",
                        lambda host: {"example.com": ["93.184.216.34"],
                                      "other.example": ["8.8.8.8"]}.get(host, ["104.16.132.229"]))

    def handler(ip, method, target, host):
        if target == "/old":
            return 302, {"Location": "http://other.example/feed.xml"}, b""
        return 200, {"Content-Type": "application/rss+xml"}, RSS2

    dialer = ServingDialer(handler)
    install(plugin, monkeypatch, dialer)

    out = await plugin._http_fetch("http://example.com/old")
    assert out.status == 200
    hosts_by_dial = {ip: host for ip, _target, host in dialer.requests}
    assert hosts_by_dial["93.184.216.34"] == "example.com"
    assert hosts_by_dial["8.8.8.8"] == "other.example"


# ---------------------------------------------------------------------------
# Favicon/image path (issue #6): same pin gate as feed fetch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_icon_fetch_rebind_second_answer_never_dialed(plugin, monkeypatch):
    """Remote icons go through _http_fetch, so a rebind cannot land on loopback.

    Validation sees 93.184.216.34; every later resolution would return
    127.0.0.1. The icon fetch must dial only the validated IP (RefusingDialer
    records the connect_tcp target without needing a real HTTP conversation).
    """
    calls = {"n": 0}

    def rebind_dns(host):
        calls["n"] += 1
        return ["93.184.216.34"] if calls["n"] == 1 else ["127.0.0.1"]

    monkeypatch.setattr(plugin, "_resolve_host_sync", rebind_dns)
    dialer = RefusingDialer()
    install(plugin, monkeypatch, dialer)

    with pytest.raises(RuntimeError, match="network error"):
        await plugin._fetch_icon("http://icons.example/fav.png")
    assert calls["n"] == 1
    assert dialer.ips == ["93.184.216.34"]
    assert "127.0.0.1" not in dialer.ips
