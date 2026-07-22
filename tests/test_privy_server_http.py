"""The real server-side Privy passwordless client — offline (injected transport).

These endpoints are NOT in Privy's public API reference (which documents only
wallets/policies/intents/key-quorums), and three doc sources said no server-side OTP
existed. An empirical probe proved otherwise, so the payload/response shapes asserted
here are copied from the VERIFIED live responses:

    POST /api/v1/passwordless/init          -> 200 {"success": true}
    POST /api/v1/passwordless/authenticate  -> 200 {"user": {"id": "did:privy:…",
                                                     "linked_accounts": [{"type": "email",
                                                     "address": "…"}]}, "token": "…"}
                                            -> 422 {"code": "invalid_credentials"}

Pattern B: the transport is injected, so none of this touches the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.privy_server import (
    HttpPrivyServerClient,
    PrivyServerError,
    privy_server_from_env,
)

APP_ID = "test-app-id"
EMAIL = "dev@example.com"
SUBJECT = "did:privy:cmnqye23c008u0cjtkn2x2zm4"
CODE = "513407"

AUTH_OK: dict[str, Any] = {
    "user": {
        "id": SUBJECT,
        "created_at": 1775707487,
        "linked_accounts": [
            {"type": "email", "address": EMAIL, "verified_at": 1775707487},
            {"type": "wallet", "address": "0xabc"},
        ],
    },
    "token": "x" * 413,
    "privy_access_token": "y" * 469,
    "refresh_token": "z" * 86,
    "is_new_user": False,
}


class _Transport:
    """Records calls and replays scripted (status, body) pairs."""

    def __init__(self, *responses: tuple[int, dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(
        self, url: str, body: dict[str, Any], *, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, body, dict(headers or {})))
        return self._responses.pop(0) if self._responses else (200, {})


def _client(*responses: tuple[int, dict[str, Any]]) -> tuple[Any, _Transport]:
    transport = _Transport(*responses)
    return (
        HttpPrivyServerClient(app_id=APP_ID, app_secret="unused", post=transport),
        transport,
    )


# --- start ------------------------------------------------------------------------


def test_start_posts_the_email_and_returns_the_handle() -> None:
    client, transport = _client((200, {"success": True}))
    handle = client.start(EMAIL)

    url, body, _headers = transport.calls[0]
    assert url.endswith("/api/v1/passwordless/init")
    assert body == {"email": EMAIL}
    # The handle IS the email: Privy's authenticate takes {email, code} and issues no
    # login id, and a stateless handle is required because the service runs >1 task.
    assert handle == EMAIL


def test_start_sends_only_the_public_app_id_never_the_secret() -> None:
    client, transport = _client((200, {"success": True}))
    client.start(EMAIL)
    _url, _body, headers = transport.calls[0]

    assert headers["privy-app-id"] == APP_ID
    blob = " ".join(f"{k}:{v}" for k, v in headers.items())
    assert "unused" not in blob  # the app_secret must not travel on this call
    assert "authorization" not in {k.lower() for k in headers}


def test_start_rejects_a_blank_email_before_any_call() -> None:
    client, transport = _client()
    with pytest.raises(PrivyServerError):
        client.start("   ")
    assert transport.calls == []


def test_start_surfaces_a_non_2xx_as_a_redacted_error() -> None:
    client, _t = _client((429, {"error": "slow down", "email": EMAIL}))
    with pytest.raises(PrivyServerError) as excinfo:
        client.start(EMAIL)
    assert "429" in str(excinfo.value)
    assert EMAIL not in str(excinfo.value)  # a provider body can echo the address


# --- verify -----------------------------------------------------------------------


def test_verify_returns_the_subject_and_email() -> None:
    client, transport = _client((200, AUTH_OK))
    identity = client.verify(EMAIL, CODE)

    url, body, _headers = transport.calls[0]
    assert url.endswith("/api/v1/passwordless/authenticate")
    assert body == {"email": EMAIL, "code": CODE}
    assert identity.subject == SUBJECT
    assert identity.email == EMAIL
    assert identity.account_id() == SUBJECT  # subject wins over email


def test_verify_picks_the_email_account_not_the_wallet() -> None:
    """linked_accounts carries several types; only the email entry is an address."""
    client, _t = _client((200, AUTH_OK))
    assert client.verify(EMAIL, CODE).email == EMAIL


def test_verify_drops_every_token() -> None:
    """Identity only. Holding a Privy access/refresh token would be custody of a
    credential we have no use for (invariant #1)."""
    client, _t = _client((200, AUTH_OK))
    identity = client.verify(EMAIL, CODE)
    blob = repr(identity)
    for token in (
        AUTH_OK["token"],
        AUTH_OK["privy_access_token"],
        AUTH_OK["refresh_token"],
    ):
        assert token not in blob


def test_a_wrong_code_raises_the_documented_422() -> None:
    client, _t = _client(
        (
            422,
            {
                "error": "Invalid email and code combination",
                "code": "invalid_credentials",
            },
        )
    )
    with pytest.raises(PrivyServerError) as excinfo:
        client.verify(EMAIL, "000000")
    assert "422" in str(excinfo.value)


def test_verify_never_echoes_the_code() -> None:
    client, _t = _client((422, {"error": f"bad code {CODE}"}))
    with pytest.raises(PrivyServerError) as excinfo:
        client.verify(EMAIL, CODE)
    assert CODE not in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},  # no user
        {"user": "not-a-dict"},
        {"user": {}},  # neither id nor email
        {"user": {"id": "", "linked_accounts": []}},
        "not-a-dict",
    ],
)
def test_an_unusable_payload_raises_instead_of_minting_against_nobody(
    payload: Any,
) -> None:
    """A blank subject AND blank email makes account_id() None, which would mint a key
    bound to no one — fail loudly instead."""
    client, _t = _client((200, payload))
    with pytest.raises(PrivyServerError):
        client.verify(EMAIL, CODE)


def test_an_email_only_payload_still_yields_an_identity() -> None:
    client, _t = _client(
        (200, {"user": {"linked_accounts": [{"type": "email", "address": EMAIL}]}})
    )
    identity = client.verify(EMAIL, CODE)
    assert identity.account_id() == EMAIL  # falls back to email when there's no subject


# --- env wiring -------------------------------------------------------------------


def test_login_is_enabled_by_the_app_id_alone() -> None:
    """The OTP endpoints authenticate with the PUBLIC app id. Requiring the secret would
    disable login for no security gain — this flow never sends it."""
    client = privy_server_from_env({"PRIVY_APP_ID": APP_ID})
    assert client is not None
    assert client.app_secret == ""


def test_the_unset_sentinel_secret_does_not_disable_login() -> None:
    client = privy_server_from_env(
        {"PRIVY_APP_ID": APP_ID, "PRIVY_APP_SECRET": "__unset__"}
    )
    assert client is not None
    assert client.app_secret == ""


@pytest.mark.parametrize("app_id", ["", "   ", "__unset__"])
def test_no_app_id_keeps_login_disabled(app_id: str) -> None:
    assert privy_server_from_env({"PRIVY_APP_ID": app_id}) is None
