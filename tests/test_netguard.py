"""SSRF guard — failing-test-first (Pattern B). No real network: DNS resolution
is injected via a fake resolver, so these run fully offline."""

import pytest

from gecko.netguard import UnsafeUrlError, validate_public_url


def _resolver(mapping: dict[str, list[str]]):
    """A fake DNS resolver: host -> list of IP strings."""

    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise UnsafeUrlError(f"unresolvable test host: {host}")
        return mapping[host]

    return resolve


PUBLIC = _resolver({"api.example.com": ["93.184.216.34"]})


# --- scheme rejection ---


def test_rejects_file_scheme():
    with pytest.raises(UnsafeUrlError):
        validate_public_url("file:///etc/passwd", resolver=PUBLIC)


@pytest.mark.parametrize(
    "url", ["ftp://example.com/x", "gopher://example.com/", "data:text/plain,hi"]
)
def test_rejects_non_http_schemes(url):
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url, resolver=PUBLIC)


def test_rejects_missing_host():
    with pytest.raises(UnsafeUrlError):
        validate_public_url("http:///nohost", resolver=PUBLIC)


# --- IP-range rejection (literal IPs, no resolver needed) ---


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",  # loopback
        "http://10.0.0.5/",  # private
        "http://192.168.1.1/",  # private
        "http://172.16.0.1/",  # private
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata / link-local
        "http://[::1]/",  # IPv6 loopback
        "http://0.0.0.0/",  # unspecified
    ],
)
def test_rejects_dangerous_ip_literals(url):
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url, resolver=PUBLIC)


# --- hostname resolving into dangerous ranges ---


def test_rejects_host_resolving_to_loopback():
    r = _resolver({"evil.example.com": ["127.0.0.1"]})
    with pytest.raises(UnsafeUrlError):
        validate_public_url("https://evil.example.com/openapi.json", resolver=r)


def test_rejects_host_resolving_to_private():
    r = _resolver({"evil.example.com": ["10.1.2.3"]})
    with pytest.raises(UnsafeUrlError):
        validate_public_url("https://evil.example.com/openapi.json", resolver=r)


def test_rejects_host_resolving_to_metadata_ip():
    r = _resolver({"rebind.example.com": ["169.254.169.254"]})
    with pytest.raises(UnsafeUrlError):
        validate_public_url("https://rebind.example.com/", resolver=r)


def test_rejects_when_any_resolved_ip_is_dangerous():
    # one public, one private -> must reject (defense against split DNS)
    r = _resolver({"mixed.example.com": ["93.184.216.34", "10.0.0.1"]})
    with pytest.raises(UnsafeUrlError):
        validate_public_url("https://mixed.example.com/", resolver=r)


# --- the allow path ---


def test_allows_normal_public_host():
    # returns None (no raise)
    assert (
        validate_public_url("https://api.example.com/openapi.json", resolver=PUBLIC)
        is None
    )


def test_allows_public_ip_literal():
    assert validate_public_url("https://93.184.216.34/openapi.json") is None


# --- safe_get redirect handling (regression: 307 was raising HTTPError -> 500) ---
import urllib.error  # noqa: E402

from gecko import netguard  # noqa: E402


class _Resp:
    def __init__(self, body: str) -> None:
        self._b = body.encode()
        self.status = 200
        self.headers: dict[str, str] = {}

    def read(self, n: int = -1) -> bytes:
        return self._b if n < 0 else self._b[:n]

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *a: object) -> bool:
        return False


class _FakeOpener:
    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[str] = []

    def open(self, request: object, timeout: object = None) -> object:
        self.calls.append(request.full_url)  # type: ignore[attr-defined]
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_safe_get_follows_307_and_revalidates_each_hop(monkeypatch) -> None:
    r = _resolver(
        {"a.example.com": ["93.184.216.34"], "b.example.com": ["93.184.216.34"]}
    )
    err = urllib.error.HTTPError(
        "https://a.example.com/docs",
        307,
        "Temporary Redirect",
        {"Location": "https://b.example.com/final"},  # type: ignore[arg-type]
        None,
    )
    fake = _FakeOpener([err, _Resp("hello docs")])
    monkeypatch.setattr(netguard.urllib.request, "build_opener", lambda *a, **k: fake)
    out = netguard.safe_get("https://a.example.com/docs", resolver=r)
    assert out == "hello docs"
    assert fake.calls == [
        "https://a.example.com/docs",
        "https://b.example.com/final",
    ]


def test_safe_get_blocks_redirect_onto_private_host(monkeypatch) -> None:
    r = _resolver(
        {"pub.example.com": ["93.184.216.34"], "evil.example.com": ["10.0.0.1"]}
    )
    err = urllib.error.HTTPError(
        "https://pub.example.com/",
        302,
        "Found",
        {"Location": "https://evil.example.com/steal"},  # type: ignore[arg-type]
        None,
    )
    fake = _FakeOpener([err, _Resp("should-not-reach")])
    monkeypatch.setattr(netguard.urllib.request, "build_opener", lambda *a, **k: fake)
    with pytest.raises(netguard.UnsafeUrlError):
        netguard.safe_get("https://pub.example.com/", resolver=r)


# --- Fix 3: DNS-rebind TOCTOU — resolve ONCE, pin the socket to the validated IP ---
class _PinFake:
    """A fake opener that records the IP its factory was pinned to and returns a body."""

    def __init__(self, pinned_ip: str | None, body: str) -> None:
        self.pinned_ip = pinned_ip
        self._body = body

    def open(self, request: object, timeout: object = None) -> object:
        return _Resp(self._body)


def test_safe_get_pins_validated_ip_and_defeats_dns_rebind(monkeypatch) -> None:
    # The rebind: a resolver that would return a PUBLIC ip on the validate call and a
    # PRIVATE (metadata) ip on any LATER resolution. safe_get must resolve ONCE and pin
    # the socket to the validated public ip — so urllib never independently re-resolves
    # into the private/metadata address.
    calls = {"n": 0}
    pinned_seen: dict[str, str | None] = {}

    def rebind(host: str) -> list[str]:
        calls["n"] += 1
        # first (validate) resolution -> public; a second resolution -> private/metadata.
        return ["93.184.216.34"] if calls["n"] == 1 else ["169.254.169.254"]

    def factory(pinned_ip: str | None) -> object:
        pinned_seen["ip"] = pinned_ip
        return _PinFake(pinned_ip, "spec-bytes")

    out = netguard.safe_get(
        "https://rebind.example.com/openapi.json",
        resolver=rebind,
        opener_factory=factory,
    )
    assert out == "spec-bytes"
    assert calls["n"] == 1  # resolved ONCE — no independent re-resolution window
    assert pinned_seen["ip"] == "93.184.216.34"  # socket pinned to the VALIDATED ip


def test_safe_get_still_refuses_when_single_resolution_is_private() -> None:
    # A rebind that resolves straight to a private ip on the single resolution is caught
    # by validation before any pin/connect happens.
    r = _resolver({"rebind.example.com": ["169.254.169.254"]})
    with pytest.raises(netguard.UnsafeUrlError):
        netguard.safe_get("https://rebind.example.com/", resolver=r)


# --- credential leak on cross-host redirect: caller headers must not follow ---


class _CapturingOpener:
    """Like _FakeOpener but records each request's header dict as it's made."""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.header_snapshots: list[dict[str, str]] = []

    def open(self, request: object, timeout: object = None) -> object:
        self.header_snapshots.append(dict(request.headers))  # type: ignore[attr-defined]
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_safe_get_drops_caller_headers_on_cross_host_redirect(monkeypatch) -> None:
    r = _resolver(
        {"a.example.com": ["93.184.216.34"], "b.example.com": ["93.184.216.34"]}
    )
    err = urllib.error.HTTPError(
        "https://a.example.com/docs",
        302,
        "Found",
        {"Location": "https://b.example.com/final"},  # type: ignore[arg-type]
        None,
    )
    fake = _CapturingOpener([err, _Resp("hello")])
    monkeypatch.setattr(netguard.urllib.request, "build_opener", lambda *a, **k: fake)
    out = netguard.safe_get(
        "https://a.example.com/docs",
        resolver=r,
        headers={"X-Gecko-Key": "secret-bearer-token"},
    )
    assert out == "hello"
    same_host, cross_host = fake.header_snapshots
    same_host_lower = {k.lower(): v for k, v in same_host.items()}
    cross_host_lower = {k.lower(): v for k, v in cross_host.items()}
    # first hop is same-host: caller header travels, alongside the UA.
    assert same_host_lower.get("x-gecko-key") == "secret-bearer-token"
    assert "user-agent" in same_host_lower
    # second hop crosses to b.example.com: caller header must be dropped, UA kept.
    assert "x-gecko-key" not in cross_host_lower
    assert "user-agent" in cross_host_lower


def test_safe_get_keeps_caller_headers_on_same_host_redirect(monkeypatch) -> None:
    r = _resolver({"a.example.com": ["93.184.216.34"]})
    err = urllib.error.HTTPError(
        "https://a.example.com/docs",
        302,
        "Found",
        {"Location": "https://a.example.com/final"},  # type: ignore[arg-type]
        None,
    )
    fake = _CapturingOpener([err, _Resp("hello")])
    monkeypatch.setattr(netguard.urllib.request, "build_opener", lambda *a, **k: fake)
    out = netguard.safe_get(
        "https://a.example.com/docs",
        resolver=r,
        headers={"X-Gecko-Key": "secret-bearer-token"},
    )
    assert out == "hello"
    first_hop, second_hop = fake.header_snapshots
    assert {k.lower(): v for k, v in first_hop.items()}.get(
        "x-gecko-key"
    ) == "secret-bearer-token"
    assert {k.lower(): v for k, v in second_hop.items()}.get(
        "x-gecko-key"
    ) == "secret-bearer-token"


def test_safe_get_drops_caller_headers_on_scheme_change_redirect(monkeypatch) -> None:
    r = _resolver({"a.example.com": ["93.184.216.34"]})
    err = urllib.error.HTTPError(
        "https://a.example.com/docs",
        302,
        "Found",
        {"Location": "http://a.example.com/final"},  # type: ignore[arg-type]
        None,
    )
    fake = _CapturingOpener([err, _Resp("hello")])
    monkeypatch.setattr(netguard.urllib.request, "build_opener", lambda *a, **k: fake)
    out = netguard.safe_get(
        "https://a.example.com/docs",
        resolver=r,
        headers={"X-Gecko-Key": "secret-bearer-token"},
    )
    assert out == "hello"
    same_scheme, diff_scheme = fake.header_snapshots
    same_scheme_lower = {k.lower(): v for k, v in same_scheme.items()}
    diff_scheme_lower = {k.lower(): v for k, v in diff_scheme.items()}
    # first hop is https: caller header travels
    assert same_scheme_lower.get("x-gecko-key") == "secret-bearer-token"
    assert "user-agent" in same_scheme_lower
    # second hop downgrades to http: caller header must be dropped, UA kept
    assert "x-gecko-key" not in diff_scheme_lower
    assert "user-agent" in diff_scheme_lower


# --- safe_get_status: the status a peg oracle answers with is itself the fact ----------
#
# `safe_get` raises HTTPError on 404 and swallows only 3xx (see the comment at the
# HTTPError handler). That is right for fetching a spec — a 404 there IS a failure. It is
# wrong for asking an oracle whether it tracks an asset, where 404 is a real ANSWER
# ("I do not track this") and every other non-2xx is "I could not answer". Collapsing
# those two into one exception is what forces a caller to guess from the body, and a
# degraded 200 then forges "no opinion". `safe_get_status` returns the status instead.


def test_safe_get_status_returns_404_as_an_answer_not_an_exception() -> None:
    err = urllib.error.HTTPError(
        "https://peg.example.com/v1/assets/by-mint/M", 404, "Not Found", None, None
    )

    def factory(pinned_ip: str | None) -> object:
        return _FakeOpener([err])

    status, body = netguard.safe_get_status(
        "https://peg.example.com/v1/assets/by-mint/M",
        resolver=_resolver({"peg.example.com": ["93.184.216.34"]}),
        opener_factory=factory,
    )
    assert (status, body) == (404, "")


def test_safe_get_status_returns_429_and_500_rather_than_raising() -> None:
    for code in (429, 500, 503):
        err = urllib.error.HTTPError(
            "https://peg.example.com/x", code, "nope", None, None
        )

        def factory(pinned_ip: str | None, _e: object = err) -> object:
            return _FakeOpener([_e])

        status, body = netguard.safe_get_status(
            "https://peg.example.com/x",
            resolver=_resolver({"peg.example.com": ["93.184.216.34"]}),
            opener_factory=factory,
        )
        assert (status, body) == (code, "")


def test_safe_get_status_still_follows_and_revalidates_redirects() -> None:
    err = urllib.error.HTTPError(
        "https://a.example.com/x",
        307,
        "Temporary Redirect",
        {"Location": "https://b.example.com/final"},  # type: ignore[arg-type]
        None,
    )
    fake = _FakeOpener([err, _Resp("card-json")])

    def factory(pinned_ip: str | None) -> object:
        return fake

    status, body = netguard.safe_get_status(
        "https://a.example.com/x",
        resolver=_resolver(
            {"a.example.com": ["93.184.216.34"], "b.example.com": ["93.184.216.34"]}
        ),
        opener_factory=factory,
    )
    assert (status, body) == (200, "card-json")
    assert fake.calls == ["https://a.example.com/x", "https://b.example.com/final"]


def test_safe_get_status_does_not_soften_an_ssrf_refusal() -> None:
    # A status is only ever returned for a hop that already PASSED validation. A private
    # address must still raise, never come back as a readable (status, body).
    with pytest.raises(netguard.UnsafeUrlError):
        netguard.safe_get_status(
            "https://internal.example.com/x",
            resolver=_resolver({"internal.example.com": ["169.254.169.254"]}),
        )


def test_safe_get_is_unchanged_and_still_raises_on_404() -> None:
    # The refactor must not move the existing contract: every current caller of safe_get
    # keeps getting the ORIGINAL HTTPError object, not a re-wrapped one.
    err = urllib.error.HTTPError("https://a.example.com/x", 404, "Not Found", None, None)

    def factory(pinned_ip: str | None) -> object:
        return _FakeOpener([err])

    with pytest.raises(urllib.error.HTTPError) as caught:
        netguard.safe_get(
            "https://a.example.com/x",
            resolver=_resolver({"a.example.com": ["93.184.216.34"]}),
            opener_factory=factory,
        )
    assert caught.value is err
