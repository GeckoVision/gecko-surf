"""`gecko auth test --live` — the real "does this credential authenticate?" probe.

`resolved ✓` only proves the keychain returns a value; it cannot tell a live token
from an expired one. `live_probe` makes one safe auth-gated GET and classifies the
HTTP status. Proven offline with an injected transport + a fake resolver — the exact
trap this closes (resolvable-but-dead token → 401) is a first-class test.
"""

from __future__ import annotations

import pytest

from gecko import authcheck
from gecko.credentials import ChainResolver, CredentialRef

_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Probe API", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "components": {
        "securitySchemes": {
            "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}
        }
    },
    "security": [{"apiKeyAuth": []}],
    "paths": {
        "/ping": {
            "get": {
                "operationId": "getPing",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


class _FakeBackend:
    name = "fake"

    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def available(self) -> bool:
        return True

    def get(self, ref: CredentialRef) -> str | None:
        return self._store.get(ref.slot())


def _seal(monkeypatch: pytest.MonkeyPatch, store: dict[str, str]) -> None:
    monkeypatch.setattr(
        "gecko.access.default_resolver", lambda: ChainResolver([_FakeBackend(store)])
    )


def test_pick_probe_op_finds_the_auth_gated_zero_arg_get() -> None:
    from gecko.client import AgentApiClient
    from gecko.access import stub_session

    client = AgentApiClient(
        _SPEC, base_url="https://api.example.com", session=stub_session()
    )
    assert authcheck.pick_probe_op(client) == "getPing"


def test_live_probe_reports_ok_on_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _seal(monkeypatch, {"probe-api": "sk-live"})
    r = authcheck.live_probe(
        _SPEC, "probe-api", live_transport=lambda req: (200, {"ok": True})
    )
    assert r.ok and r.status == 200 and r.op == "getPing"


def test_live_probe_flags_a_resolvable_but_dead_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The exact TxODDS trap: the keychain resolves a value, but the API rejects it.
    _seal(monkeypatch, {"probe-api": "sk-expired"})
    r = authcheck.live_probe(
        _SPEC, "probe-api", live_transport=lambda req: (401, {"error": "unauthorized"})
    )
    assert not r.ok and r.status == 401
    assert "REJECTED" in r.detail


def test_live_probe_reports_missing_credential_not_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seal(monkeypatch, {})  # nothing sealed
    r = authcheck.live_probe(_SPEC, "probe-api", live_transport=lambda req: (200, {}))
    assert not r.ok
    assert "missing" in r.detail.lower()


def test_bundled_target_resolves_txline_no_spec_needed() -> None:
    target = authcheck.bundled_probe_target("txline")
    assert target is not None
    spec, base_url = target
    assert base_url == "https://txline.txodds.com"
    assert "TxLINE" in (spec.get("info") or {}).get("title", "")


def test_bundled_target_is_none_for_unknown_surface() -> None:
    assert authcheck.bundled_probe_target("some-random-api") is None
