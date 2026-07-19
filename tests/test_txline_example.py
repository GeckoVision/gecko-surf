"""The bundled `txline-mcp` surface — TxLINE served with zero local files.

Proves the package ships the comprehended spec, that recorded mode needs no
tokens (stub session keeps the two-token endpoints visible, $0), that live mode
builds the two-token `MultiSession`, and that live-without-sealed-tokens guides
the user instead of crashing with a traceback. All offline, no network.
"""

from __future__ import annotations

import pytest

from gecko.access import MultiSession
from gecko.credentials import ChainResolver, CredentialError, CredentialRef
from gecko.examples import txline


def test_spec_is_bundled_in_the_package_no_network() -> None:
    spec = txline.load_spec()
    assert "TxLINE" in (spec.get("info") or {}).get("title", "")
    # both declared header schemes are present — the whole reason this surface exists.
    schemes = (spec.get("components") or {}).get("securitySchemes") or {}
    assert {"httpAuth", "apiKeyAuth"} <= set(schemes)


def test_recorded_mode_needs_no_tokens_and_keeps_gated_tools_visible() -> None:
    # A stub session yields both headers, so the auth-gated TxLINE ops stay visible
    # for the agent even though no real credential exists — recorded is $0/offline.
    client = txline.build_client(mode="recorded")
    assert client.base_url == txline.BASE_URL
    assert len(client.list_tools()) > 0


class _FakeBackend:
    name = "fake"

    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def available(self) -> bool:
        return True

    def get(self, ref: CredentialRef) -> str | None:
        return self._store.get(ref.slot())


def test_live_mode_builds_a_two_token_multisession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With both per-scheme creds sealed, live mode resolves a MultiSession that emits
    # both TxLINE headers together.
    store = {"txline:httpAuth": "jwt-x", "txline:apiKeyAuth": "tok-y"}
    monkeypatch.setattr(
        "gecko.access.default_resolver", lambda: ChainResolver([_FakeBackend(store)])
    )
    client = txline.build_client(mode="live")
    assert isinstance(client.session, MultiSession)
    assert client.session.auth_headers() == {
        "Authorization": "Bearer jwt-x",
        "X-Api-Token": "tok-y",
    }


def test_live_without_sealed_tokens_guides_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No creds sealed → constructing the live client raises CredentialError (visibility
    # check resolves the session). build_client must propagate it so main() can guide;
    # it must NOT be swallowed into a silent recorded fallback.
    monkeypatch.setattr(
        "gecko.access.default_resolver", lambda: ChainResolver([_FakeBackend({})])
    )
    with pytest.raises(CredentialError):
        txline.build_client(mode="live")


def test_main_live_without_tokens_returns_1_not_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "gecko.access.default_resolver", lambda: ChainResolver([_FakeBackend({})])
    )
    rc = txline.main(["--mode", "live"])
    assert rc == 1
    err = capsys.readouterr().err
    # the two exact seal commands are surfaced so the user knows how to go live.
    assert "gecko auth set txline --account httpAuth --scheme bearer" in err
    assert "gecko auth set txline --account apiKeyAuth" in err
