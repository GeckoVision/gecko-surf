import json
import pytest
from gecko.netguard import UnsafeUrlError
from gecko.onboard import resolve_spec, pin_base_url, OnboardError

_SPEC = {"openapi": "3.0.3", "info": {"title": "T", "version": "1"}, "paths": {}}


def _resolver(mapping: dict[str, list[str]]):
    """A fake DNS resolver: host -> list of IP strings."""

    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise UnsafeUrlError(f"unresolvable test host: {host}")
        return mapping[host]

    return resolve


PUBLIC = _resolver({"api.example.com": ["93.184.216.34"]})


def test_resolves_openapi_url_via_injected_fetch():
    resolved = resolve_spec(
        "https://api.example.com/openapi.json",
        fetch=lambda u: json.dumps(_SPEC),
        resolver=PUBLIC,
    )
    assert resolved.spec["openapi"] == "3.0.3"
    # http(s) URL that yielded a direct JSON OpenAPI -> the fetch origin IS provenance.
    assert resolved.spec_url == "https://api.example.com/openapi.json"


def test_resolves_local_path(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(_SPEC))
    resolved = resolve_spec(str(p))
    assert resolved.spec["info"]["title"] == "T"
    # A local path is never pinning provenance (see surfaces.anchor_for).
    assert resolved.spec_url is None


def test_rejects_unsafe_url():
    with pytest.raises(OnboardError):
        resolve_spec("http://169.254.169.254/openapi.json", fetch=lambda u: "{}")


# --- pin_base_url: reconcile the fetch origin against the spec's own servers[] -------


def test_pin_base_url_none_when_no_spec_url():
    # docs-recovery / local-path sources stay unverified — CORRECT, do not pin.
    base_url, warning = pin_base_url(None, _SPEC)
    assert base_url is None
    assert warning is None


def test_pin_base_url_trusts_servers_url_when_host_matches_fetch_origin():
    spec = {**_SPEC, "servers": [{"url": "https://api.example.com/v2"}]}
    base_url, warning = pin_base_url("https://api.example.com/openapi.json", spec)
    # Host matches provenance -> trust the full server URL (keeps any path prefix).
    assert base_url == "https://api.example.com/v2"
    assert warning is None


def test_pin_base_url_falls_back_to_fetch_origin_when_host_mismatches():
    spec = {**_SPEC, "servers": [{"url": "https://evil.example.net/v2"}]}
    base_url, warning = pin_base_url("https://api.example.com/openapi.json", spec)
    # NEVER trust the spec's own server host when it disagrees with provenance.
    assert base_url == "https://api.example.com"
    assert warning is not None
    assert "evil.example.net" in warning
    assert "api.example.com" in warning


def test_pin_base_url_falls_back_to_fetch_origin_when_no_servers():
    base_url, warning = pin_base_url("https://api.example.com/openapi.json", _SPEC)
    assert base_url == "https://api.example.com"
    assert warning is not None
    assert "api.example.com" in warning
