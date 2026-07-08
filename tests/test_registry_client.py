"""Runner fetch: registry -> cache -> offline fallback (stale flag)."""

import io
import json
import urllib.error

import pytest

from gecko import netguard
from gecko.registry.client import RegistryFetchError, fetch_surface

MANIFEST = {
    "name": "colosseum",
    "surface_rev": "abc12345",
    "tier": "free",
    "spec": {"openapi": "3.1.0", "info": {"title": "T", "version": "1"}, "paths": {}},
}


def test_fetch_writes_cache(tmp_path):
    calls = []

    def transport(url, headers):
        calls.append((url, headers))
        return 200, json.dumps(MANIFEST)

    got = fetch_surface(
        "https://registry.example.com",
        "colosseum",
        cache_dir=tmp_path,
        transport=transport,
    )
    assert got.surface_rev == "abc12345" and got.stale is False
    assert calls[0][0] == "https://registry.example.com/registry/surfaces/colosseum"
    cached = json.loads((tmp_path / "colosseum.json").read_text("utf-8"))
    assert cached["surface_rev"] == "abc12345"


def test_key_header_sent_when_given(tmp_path):
    seen = {}

    def transport(url, headers):
        seen.update(headers)
        return 200, json.dumps(MANIFEST)

    fetch_surface(
        "https://registry.example.com",
        "colosseum",
        key="gk_live_x",
        cache_dir=tmp_path,
        transport=transport,
    )
    assert seen.get("X-Gecko-Key") == "gk_live_x"


def test_network_failure_falls_back_to_cache_stale(tmp_path):
    (tmp_path / "colosseum.json").write_text(json.dumps(MANIFEST), "utf-8")

    def transport(url, headers):
        raise OSError("network down")

    got = fetch_surface(
        "https://registry.example.com",
        "colosseum",
        cache_dir=tmp_path,
        transport=transport,
    )
    assert got.stale is True and got.spec == MANIFEST["spec"]


def test_network_failure_no_cache_raises(tmp_path):
    def transport(url, headers):
        raise OSError("network down")

    with pytest.raises(RegistryFetchError):
        fetch_surface(
            "https://registry.example.com",
            "nope",
            cache_dir=tmp_path,
            transport=transport,
        )


def test_entitlement_402_raises_with_remediation(tmp_path):
    def transport(url, headers):
        return 402, json.dumps(
            {"error": "entitlement_required", "remediation": "ask for access"}
        )

    with pytest.raises(RegistryFetchError, match="entitlement_required"):
        fetch_surface(
            "https://registry.example.com",
            "txline",
            cache_dir=tmp_path,
            transport=transport,
        )


def test_corrupt_cache_raises_typed_error(tmp_path):
    (tmp_path / "colosseum.json").write_text("{not json", "utf-8")

    def transport(url, headers):
        raise OSError("network down")

    with pytest.raises(RegistryFetchError, match="corrupt cache"):
        fetch_surface(
            "https://registry.example.com",
            "colosseum",
            cache_dir=tmp_path,
            transport=transport,
        )


def test_malformed_wire_manifest_raises_typed_error(tmp_path):
    def transport(url, headers):
        return 200, json.dumps({"name": "colosseum"})  # missing surface_rev/tier/spec

    with pytest.raises(RegistryFetchError, match="malformed manifest"):
        fetch_surface(
            "https://registry.example.com",
            "colosseum",
            cache_dir=tmp_path,
            transport=transport,
        )


def test_cache_write_is_atomic(tmp_path):
    def transport(url, headers):
        return 200, json.dumps(MANIFEST)

    fetch_surface(
        "https://registry.example.com",
        "colosseum",
        cache_dir=tmp_path,
        transport=transport,
    )
    assert (tmp_path / "colosseum.json").exists()
    assert not (tmp_path / "colosseum.tmp").exists()


# --- exercising the REAL transport (_default_transport) via safe_get's injectable
# seams, so the fix for "a real 402 must surface, not degrade to stale cache" is
# proven against the actual wire path, not just a fake `transport` callable.


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

    def open(self, request: object, timeout: object = None) -> object:
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _patch_public_dns(monkeypatch) -> None:
    # No injected resolver reaches _default_transport (it calls safe_get with no
    # resolver/opener_factory kwargs), so we patch the real DNS + opener seams
    # that safe_get falls back to.
    monkeypatch.setattr(
        netguard.socket,
        "getaddrinfo",
        lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))],
    )


def test_default_transport_end_to_end_success(monkeypatch, tmp_path):
    _patch_public_dns(monkeypatch)
    fake = _FakeOpener([_Resp(json.dumps(MANIFEST))])
    monkeypatch.setattr(netguard.urllib.request, "build_opener", lambda *a, **k: fake)

    got = fetch_surface(
        "https://registry.example.com",
        "colosseum",
        cache_dir=tmp_path,
    )
    assert got.surface_rev == "abc12345" and got.stale is False


def test_default_transport_surfaces_real_402(monkeypatch, tmp_path):
    _patch_public_dns(monkeypatch)
    body = json.dumps(
        {"error": "entitlement_required", "remediation": "ask for access"}
    ).encode()
    err = urllib.error.HTTPError(
        "https://registry.example.com/registry/surfaces/txline",
        402,
        "Payment Required",
        {},  # type: ignore[arg-type]
        io.BytesIO(body),
    )
    fake = _FakeOpener([err])
    monkeypatch.setattr(netguard.urllib.request, "build_opener", lambda *a, **k: fake)

    with pytest.raises(RegistryFetchError, match="entitlement_required"):
        fetch_surface(
            "https://registry.example.com",
            "txline",
            cache_dir=tmp_path,
        )
