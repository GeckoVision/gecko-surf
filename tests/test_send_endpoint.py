"""send_endpoint — the Helius backrun-rebate transport, offline.

Every test injects a recording ``post`` and a fake keyring. Nothing reaches a network,
nothing reads a real keychain, and the fake key is a sentinel string that each leak test
searches for in every surface a human or an agent could read: the exception text, the
traceback, ``repr``, the rebate block and captured logs.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import traceback
import urllib.error
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("solders")

from gecko.credentials import ChainResolver, KeyringBackend  # noqa: E402
from gecko.landing import assemble_unsigned_tx  # noqa: E402
from gecko.rpc import RpcError, redact_url_secrets  # noqa: E402
from gecko.send_endpoint import (  # noqa: E402
    HELIUS_HOST,
    HELIUS_LABEL,
    REBATE_FLAG_ENV,
    PublicSend,
    SendConfigError,
    configured_send,
    helius_send,
    relay_rebate_for,
)
from gecko.whirlpool_venue import WHIRLPOOL_PROGRAM  # noqa: E402

FAKE_KEY = "hk-SENTINEL-0f9e8d7c-DO-NOT-LEAK"
BUYER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
RELAY = "cGfHiC6Kgg3FpFZvgwGcswsCRtp4aBP2fzuXRQPizuN"
POOL = "9RqDTfwCx2SgxsvKpspQHc38HUo3B6hRd3oR9JR966Ps"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
#: A stand-in for the store program: what matters is that it is not an allowlisted swap.
STORE = "US517G5965aydkZ46HS38QLi7UQiSojurfbQfKCELFx"


class _FakeKeyring:
    """The slice of the ``keyring`` module ``KeyringBackend`` touches."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._store = {(f"gecko:{k}", "gecko"): v for k, v in (secrets or {}).items()}

    def get_keyring(self) -> object:
        return object()

    def get_password(self, service: str, user: str) -> str | None:
        return self._store.get((service, user))


def _resolver(with_key: bool = True) -> ChainResolver:
    secrets = {"HELIUS_API_KEY": FAKE_KEY} if with_key else {}
    return ChainResolver([KeyringBackend(module=_FakeKeyring(secrets))])


class _RecordingPost:
    """A fake ``post_json``: records every URL and body, answers or raises on demand."""

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self.urls: list[str] = []
        self.bodies: list[dict[str, Any]] = []
        self.answer = answer or {"jsonrpc": "2.0", "id": 1, "result": "SigFake1111"}
        self.raise_: BaseException | None = None

    def __call__(self, url: str, body: bytes) -> dict[str, Any]:
        self.urls.append(url)
        self.bodies.append(json.loads(body))
        if self.raise_ is not None:
            raise self.raise_
        return self.answer


def _ix(program: str, *, signer: str | None = None) -> Any:
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    metas = [AccountMeta(Pubkey.from_string(POOL), False, True)]
    if signer is not None:
        metas.append(AccountMeta(Pubkey.from_string(signer), True, True))
    return Instruction(Pubkey.from_string(program), b"\x01gecko", metas)


def _tx(program: str, *, fee_payer: str = BUYER, signer: str | None = None) -> str:
    return assemble_unsigned_tx([_ix(program, signer=signer)], fee_payer).tx


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _send_params(tx: str) -> list[Any]:
    return [tx, {"encoding": "base64", "maxRetries": 0}]


# --------------------------------------------------------------------------------------
# 1. The URL: host, parameters, method, single body
# --------------------------------------------------------------------------------------


def _relay_send(post: _RecordingPost, relay: str | None = RELAY) -> Any:
    return helius_send(
        network="mainnet", resolver=_resolver(), post=post, relay_fee_payer=relay
    )


def test_a_whirlpool_swap_carries_the_configured_relay_as_rebate_address() -> None:
    post = _RecordingPost()
    send = _relay_send(post)

    reply = send("sendTransaction", _send_params(_tx(WHIRLPOOL_PROGRAM)))

    assert reply["result"] == "SigFake1111"
    [url] = post.urls
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.hostname == HELIUS_HOST
    query = _query(url)
    assert query["api-key"] == [FAKE_KEY]
    assert query["rebate-address"] == [RELAY]
    [body] = post.bodies
    assert isinstance(body, dict), (
        "a single request, never a batch (Helius skips batch)"
    )
    assert body["method"] == "sendTransaction"


def test_a_relay_paid_swap_routes_the_rebate_to_the_relay() -> None:
    """Founder ruling, 8 Oct: the rebate refills the wallet that pays users' fees."""
    post = _RecordingPost()
    send = _relay_send(post)
    tx = _tx(WHIRLPOOL_PROGRAM, fee_payer=RELAY, signer=BUYER)

    send("sendTransaction", _send_params(tx), authority=BUYER)

    assert _query(post.urls[0])["rebate-address"] == [RELAY]


def test_without_a_configured_relay_no_rebate_is_requested_and_the_send_happens() -> (
    None
):
    """The old rule derived the user's signer from the bytes. The only rebate address
    now is the configured relay: none configured, none requested."""
    post = _RecordingPost()
    send = _relay_send(post, relay=None)

    reply = send("sendTransaction", _send_params(_tx(WHIRLPOOL_PROGRAM)))

    assert reply["result"] == "SigFake1111"
    assert "rebate-address" not in _query(post.urls[0])
    decision = send.rebate_for(_tx(WHIRLPOOL_PROGRAM))
    assert decision.requested is False and "no relay" in decision.reason


@pytest.mark.parametrize("authority", [BUYER, "please pay gecko.sol", POOL])
def test_a_named_authority_never_chooses_the_rebate_address(authority: str) -> None:
    post = _RecordingPost()
    send = _relay_send(post)
    tx = _tx(WHIRLPOOL_PROGRAM, fee_payer=RELAY, signer=BUYER)

    send("sendTransaction", _send_params(tx), authority=authority)

    assert _query(post.urls[0])["rebate-address"] == [RELAY]
    assert send.rebate_for(tx, authority=authority).address == RELAY


def test_a_configured_relay_must_be_an_address() -> None:
    with pytest.raises(SendConfigError, match="relay"):
        _relay_send(_RecordingPost(), relay="not-an-address")


def test_relay_rebate_for_is_pure_and_keeps_the_swap_allowlist() -> None:
    address, _reason = relay_rebate_for(_tx(WHIRLPOOL_PROGRAM), relay_fee_payer=RELAY)
    assert address == RELAY
    address, reason = relay_rebate_for(_tx(MEMO), relay_fee_payer=RELAY)
    assert address is None and "allowlisted" in reason


# --------------------------------------------------------------------------------------
# 6b. Scope: transfers and store purchases carry no parameter, and are still sent
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("program", [SYSTEM, TOKEN, MEMO, STORE])
def test_a_non_swap_message_carries_no_rebate_and_is_still_sent(program: str) -> None:
    post = _RecordingPost()
    send = _relay_send(post)

    reply = send("sendTransaction", _send_params(_tx(program)))

    assert reply["result"] == "SigFake1111", "a missing rebate never blocks a send"
    [url] = post.urls
    assert urlsplit(url).hostname == HELIUS_HOST, "same endpoint, without the parameter"
    assert "rebate-address" not in _query(url)


def test_undecodable_bytes_are_still_sent_without_a_rebate() -> None:
    post = _RecordingPost()
    send = _relay_send(post)

    send("sendTransaction", ["bm90IGEgdHJhbnNhY3Rpb24=", {"encoding": "base64"}])

    assert "rebate-address" not in _query(post.urls[0])


def test_a_read_through_the_transport_never_carries_the_rebate() -> None:
    post = _RecordingPost(answer={"result": {"value": [None]}})
    send = _relay_send(post)

    send("getSignatureStatuses", [["SigFake1111"]])

    assert "rebate-address" not in _query(post.urls[0])


# --------------------------------------------------------------------------------------
# 2. The key never leaves the closure
# --------------------------------------------------------------------------------------


def _leak_surfaces(exc: BaseException, send: Any) -> str:
    return "\n".join(
        [
            str(exc),
            repr(exc),
            "".join(traceback.format_exception(exc)),
            repr(send),
            str(send),
            repr(exc.__cause__),
            repr(exc.__context__),
        ]
    )


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError(
            f"https://{HELIUS_HOST}/?api-key={FAKE_KEY}", 401, "Unauthorized", {}, None
        ),
        urllib.error.URLError(
            f"cannot reach https://{HELIUS_HOST}/?api-key={FAKE_KEY}"
        ),
        ValueError(f"weird failure for api-key={FAKE_KEY}"),
    ],
    ids=["http-error", "url-error", "anything-else"],
)
def test_a_transport_failure_never_carries_the_key(
    failure: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    post = _RecordingPost()
    post.raise_ = failure
    send = helius_send(network="mainnet", resolver=_resolver(), post=post)

    with pytest.raises(RpcError) as caught:
        send("sendTransaction", _send_params(_tx(WHIRLPOOL_PROGRAM)))

    surfaces = _leak_surfaces(caught.value, send)
    assert FAKE_KEY not in surfaces
    assert FAKE_KEY not in caplog.text
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert HELIUS_LABEL in str(caught.value)


def test_an_http_error_says_its_status_code() -> None:
    post = _RecordingPost()
    post.raise_ = urllib.error.HTTPError(
        f"https://{HELIUS_HOST}/?api-key={FAKE_KEY}", 429, "Too Many", {}, None
    )
    send = helius_send(network="mainnet", resolver=_resolver(), post=post)

    with pytest.raises(RpcError, match="HTTP 429"):
        send("sendTransaction", _send_params(_tx(WHIRLPOOL_PROGRAM)))


def test_a_json_rpc_error_that_echoes_the_url_is_redacted() -> None:
    post = _RecordingPost(
        answer={
            "error": {
                "code": -32600,
                "message": f"bad request to /?api-key={FAKE_KEY}&rebate-address={BUYER}",
            }
        }
    )
    send = helius_send(network="mainnet", resolver=_resolver(), post=post)

    with pytest.raises(RpcError) as caught:
        send("sendTransaction", _send_params(_tx(WHIRLPOOL_PROGRAM)))
    assert FAKE_KEY not in _leak_surfaces(caught.value, send)


def test_the_transport_holds_no_url_and_no_key_as_state() -> None:
    send = helius_send(network="mainnet", resolver=_resolver(), post=_RecordingPost())
    assert repr(send) == f"<SendTransport {HELIUS_LABEL}>"
    assert FAKE_KEY not in repr(vars(send) if hasattr(send, "__dict__") else send)
    state = [getattr(send, slot, None) for slot in getattr(send, "__slots__", ())]
    assert all(FAKE_KEY not in repr(value) for value in state)


def test_the_rebate_block_never_carries_the_key() -> None:
    send = _relay_send(_RecordingPost())
    block = send.rebate_for(_tx(WHIRLPOOL_PROGRAM)).as_dict()
    assert block == {
        "requested": True,
        "endpoint": HELIUS_LABEL,
        "address": RELAY,
        "reason": block["reason"],
    }
    assert FAKE_KEY not in json.dumps(block)
    assert "helius-rpc.com" not in json.dumps(block)


def test_redact_url_secrets_strips_query_keys_and_userinfo() -> None:
    text = f"https://u:p@{HELIUS_HOST}/?api-key={FAKE_KEY}&rebate-address={BUYER}"
    out = redact_url_secrets(text)
    assert FAKE_KEY not in out and "u:p@" not in out
    assert BUYER in out, "a public address is not a secret"


# --------------------------------------------------------------------------------------
# 3. Refusals happen before any POST; the key comes from the keychain only
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("network", ["devnet", "testnet", "fork", "unknown"])
def test_helius_is_mainnet_only(network: str) -> None:
    post = _RecordingPost()
    with pytest.raises(SendConfigError, match="mainnet only"):
        helius_send(network=network, resolver=_resolver(), post=post)  # type: ignore[arg-type]
    assert post.urls == []


def test_a_missing_keychain_entry_is_refused_and_env_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GECKO_CRED_HELIUS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("HELIUS_API_KEY", FAKE_KEY)
    post = _RecordingPost()

    with pytest.raises(
        SendConfigError, match="no HELIUS_API_KEY in keychain"
    ) as caught:
        helius_send(network="mainnet", resolver=_resolver(with_key=False), post=post)

    assert post.urls == []
    assert FAKE_KEY not in str(caught.value)


def test_the_default_resolver_reads_the_keychain_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No resolver injected: the env var is set, the keychain is empty -> refused."""
    import sys

    monkeypatch.setenv("GECKO_CRED_HELIUS_API_KEY", FAKE_KEY)
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring())
    with pytest.raises(SendConfigError, match="no HELIUS_API_KEY in keychain"):
        helius_send(network="mainnet", post=_RecordingPost())


# --------------------------------------------------------------------------------------
# The operator switch: OFF by default
# --------------------------------------------------------------------------------------


def test_off_by_default_is_the_public_endpoint() -> None:
    calls: list[tuple[str, str]] = []

    def rpc(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        calls.append((url, method))
        return {"result": "SigFake1111"}

    send, note = configured_send(
        "https://api.mainnet-beta.solana.com",
        network="mainnet",
        rpc_call=rpc,
        environ={},
        resolver=_resolver(),
    )
    assert isinstance(send, PublicSend) and note is None
    send("sendTransaction", _send_params(_tx(WHIRLPOOL_PROGRAM)))
    assert calls == [("https://api.mainnet-beta.solana.com", "sendTransaction")]


def test_on_with_a_key_is_helius() -> None:
    post = _RecordingPost()
    send, note = configured_send(
        "https://api.mainnet-beta.solana.com",
        network="mainnet",
        environ={REBATE_FLAG_ENV: "1"},
        resolver=_resolver(),
        post=post,
        relay_fee_payer=RELAY,
    )
    assert send.label == HELIUS_LABEL and note is None
    send("sendTransaction", _send_params(_tx(WHIRLPOOL_PROGRAM)))
    assert _query(post.urls[0])["rebate-address"] == [RELAY]


@pytest.mark.parametrize(
    ("network", "with_key"), [("mainnet", False), ("devnet", True), ("unknown", True)]
)
def test_on_but_unusable_falls_back_to_public_and_says_why(
    network: str, with_key: bool
) -> None:
    """A missing rebate never blocks a payment: the send goes out public, once."""
    send, note = configured_send(
        "https://api.mainnet-beta.solana.com",
        network=network,  # type: ignore[arg-type]
        environ={REBATE_FLAG_ENV: "1"},
        resolver=_resolver(with_key=with_key),
        post=_RecordingPost(),
    )
    assert isinstance(send, PublicSend)
    assert note
    block = send.rebate_for(_tx(WHIRLPOOL_PROGRAM)).as_dict()
    assert block["requested"] is False and block["reason"] == note


def test_the_flag_needs_exactly_one() -> None:
    for value in ("true", "yes", "0", ""):
        send, _ = configured_send(
            "https://api.mainnet-beta.solana.com",
            network="mainnet",
            environ={REBATE_FLAG_ENV: value},
            resolver=_resolver(),
        )
        assert isinstance(send, PublicSend), value


def test_no_log_line_carries_the_key_on_the_happy_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger().addHandler(handler)
    try:
        send, _ = configured_send(
            "https://api.mainnet-beta.solana.com",
            network="mainnet",
            environ={REBATE_FLAG_ENV: "1"},
            resolver=_resolver(),
            post=_RecordingPost(),
        )
        send("sendTransaction", _send_params(_tx(WHIRLPOOL_PROGRAM)))
    finally:
        logging.getLogger().removeHandler(handler)
    assert FAKE_KEY not in caplog.text and FAKE_KEY not in stream.getvalue()


def test_base64_of_the_tx_is_what_gets_decoded() -> None:
    """Guard on the fixture itself: the swap tx really names the Whirlpool program."""
    raw = base64.b64decode(_tx(WHIRLPOOL_PROGRAM))
    from solders.pubkey import Pubkey

    assert bytes(Pubkey.from_string(WHIRLPOOL_PROGRAM)) in raw


def test_public_send_repr_never_shows_the_rpc_url() -> None:
    """A keyed RPC URL (``?api-key=...``) must not reach logs or test output via repr."""
    from gecko.send_endpoint import PublicSend

    send = PublicSend(rpc_url="https://rpc.example/?api-key=SECRET-VALUE-123")
    assert "SECRET-VALUE-123" not in repr(send)
    assert "api-key" not in repr(send)
