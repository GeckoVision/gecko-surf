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
