"""submit_transaction — verify -> send -> rebroadcast -> confirm, offline.

Driven by the friction report: an agent with no shell could sign and then STOP,
and the agent under the clock dropped verification. The tool bundles both, and
the binding requirement doubles as relay-abuse prevention: nothing broadcasts
unless it verifies against a Gecko receipt's binding at exact strength."""

from __future__ import annotations

import base64

import pytest

pytest.importorskip("solders")

from gecko.landing import assemble_unsigned_tx  # noqa: E402
from gecko.submit_transaction import submit_transaction_result  # noqa: E402
from gecko.txbind import message_binding  # noqa: E402

PAYER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _tx_with_binding() -> tuple[str, str]:
    """A real unsigned memo tx with a stamped (unverifiable) signature — what a
    signer hands back — plus the exact binding a receipt would have issued."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    meta = AccountMeta(Pubkey.from_string(USDC), False, False)
    unsigned = assemble_unsigned_tx([Instruction(program, b"gecko", [meta])], PAYER).tx
    raw = bytearray(base64.b64decode(unsigned))
    raw[1:65] = bytes(
        range(64)
    )  # non-empty signature slot; binding is over the message
    signed = base64.b64encode(bytes(raw)).decode()
    return signed, message_binding(signed, strength="exact")


def test_refuses_without_binding() -> None:
    out = submit_transaction_result(
        {"transaction": "AAAA", "last_valid_block_height": 1}
    )
    assert out["refused"] and out["code"] == "binding-required"


def test_refuses_without_expiry() -> None:
    tx, binding = _tx_with_binding()
    out = submit_transaction_result({"transaction": tx, "binding": binding})
    assert out["refused"] and out["code"] == "expiry-required"


def test_refuses_private_rpc() -> None:
    tx, binding = _tx_with_binding()
    out = submit_transaction_result(
        {
            "transaction": tx,
            "binding": binding,
            "last_valid_block_height": 1,
            "rpc_url": "http://192.168.1.10:8899",
        }
    )
    assert out["refused"] and out["code"] == "rpc-url-refused"


def test_refuses_a_binding_mismatch() -> None:
    tx, _ = _tx_with_binding()
    out = submit_transaction_result(
        {"transaction": tx, "binding": "0" * 64, "last_valid_block_height": 1}
    )
    assert out["refused"] and out["code"] == "verify-failed"


def _fake_rpc(confirm_after: int, height: int = 10):
    calls = {"status_polls": 0, "sends": 0}

    def call(url, method, params):
        if method == "sendTransaction":
            calls["sends"] += 1
            return {"result": "SigFake1111"}
        if method == "getSignatureStatuses":
            calls["status_polls"] += 1
            if calls["status_polls"] >= confirm_after:
                return {
                    "result": {
                        "value": [
                            {"confirmationStatus": "confirmed", "slot": 42, "err": None}
                        ]
                    }
                }
            return {"result": {"value": [None]}}
        if method == "getBlockHeight":
            return {"result": height}
        raise AssertionError(method)

    return call, calls


def test_confirms_after_rebroadcast() -> None:
    tx, binding = _tx_with_binding()
    call, calls = _fake_rpc(confirm_after=3)
    out = submit_transaction_result(
        {"transaction": tx, "binding": binding, "last_valid_block_height": 100},
        rpc_call=call,
        sleep=lambda _s: None,
    )
    assert out == {
        "refused": False,
        "confirmed": True,
        "signature": "SigFake1111",
        "slot": 42,
        "err": None,
        "confirmation_status": "confirmed",
    }
    # the loop actually rebroadcast (first send + at least the retries before confirm)
    assert calls["sends"] >= 3


def test_expiry_is_honest_and_names_the_next_step() -> None:
    tx, binding = _tx_with_binding()
    call, _ = _fake_rpc(confirm_after=999, height=200)
    out = submit_transaction_result(
        {"transaction": tx, "binding": binding, "last_valid_block_height": 100},
        rpc_call=call,
        sleep=lambda _s: None,
    )
    assert out["expired"] is True and out["spent"] is False
    assert "never re-submit" in out["reason"]


def test_a_transaction_already_landed_is_reported_without_waiting() -> None:
    """Ask before waiting.

    The loop used to sleep, then rebroadcast, then finally poll — so a transaction
    confirmed 400ms after the first send was reported roughly 2.7s later, and the
    resend it did in between was pure waste. The poll is the only call that can end
    the loop, so it goes first.
    """
    tx, binding = _tx_with_binding()
    call, calls = _fake_rpc(confirm_after=1)
    slept: list[float] = []

    out = submit_transaction_result(
        {"transaction": tx, "binding": binding, "last_valid_block_height": 100},
        rpc_call=call,
        sleep=lambda seconds: slept.append(seconds),
    )

    assert out["confirmed"] is True
    assert slept == [], "a transaction that already landed must not wait a full tick"
    assert calls["sends"] == 1, "and it must not rebroadcast one it can already see"


def test_the_height_read_is_skipped_on_the_first_pass() -> None:
    """A 600ms round trip must not sit in front of the fast confirmation above.

    Skipping height reads is safe in ONE direction only: it can make the expiry
    declaration later, never earlier. Declaring expiry early would be the loop's one
    real lie — `spent: false` asserts that no funds moved.
    """
    tx, binding = _tx_with_binding()
    heights: list[int] = []

    def call(url, method, params):
        if method == "sendTransaction":
            return {"result": "SigFake1111"}
        if method == "getSignatureStatuses":
            return {"result": {"value": [None]}}
        if method == "getBlockHeight":
            heights.append(1)
            # Expired, so the loop ends as soon as it actually looks.
            return {"result": 10_000}
        raise AssertionError(method)

    out = submit_transaction_result(
        {"transaction": tx, "binding": binding, "last_valid_block_height": 100},
        rpc_call=call,
        sleep=lambda _s: None,
    )
    assert out["expired"] is True
    assert out["spent"] is False
    # Not read on the first pass; read once the loop has actually been round a few times.
    assert len(heights) == 1, "height is read periodically, not every tick"


# --------------------------------------------------------------------------------------
# The send endpoint (gecko.send_endpoint): rebate routing is opt-in and changes only
# where the sendTransaction calls go.
# --------------------------------------------------------------------------------------

WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
HELIUS_FAKE_KEY = "hk-SUBMIT-SENTINEL-DO-NOT-LEAK"


def _swap_tx_with_binding() -> tuple[str, str]:
    """The same stamped shape as `_tx_with_binding`, but the program is Whirlpool."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    meta = AccountMeta(Pubkey.from_string(USDC), False, True)
    ix = Instruction(Pubkey.from_string(WHIRLPOOL), b"\x01swap", [meta])
    unsigned = assemble_unsigned_tx([ix], PAYER).tx
    raw = bytearray(base64.b64decode(unsigned))
    raw[1:65] = bytes(range(64))
    signed = base64.b64encode(bytes(raw)).decode()
    return signed, message_binding(signed, strength="exact")


#: The Kora relay's fee payer, the only rebate address (founder ruling, 8 Oct).
RELAY = "cGfHiC6Kgg3FpFZvgwGcswsCRtp4aBP2fzuXRQPizuN"


def _helius(post, relay=RELAY):
    from gecko.credentials import ChainResolver, KeyringBackend
    from gecko.send_endpoint import helius_send

    class _Keyring:
        def get_keyring(self):
            return object()

        def get_password(self, service, user):
            return HELIUS_FAKE_KEY if service == "gecko:HELIUS_API_KEY" else None

    resolver = ChainResolver([KeyringBackend(module=_Keyring())])
    return helius_send(
        network="mainnet", resolver=resolver, post=post, relay_fee_payer=relay
    )


class _HeliusPost:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.methods: list[str] = []

    def __call__(self, url, body):
        import json

        self.urls.append(url)
        self.methods.append(json.loads(body)["method"])
        return {"result": "SigFake1111"}


def _reads_only(confirm_after: int, height: int = 10):
    """An rpc_call that answers reads and FAILS on any send: sends must not reach it."""
    seen: list[tuple[str, str]] = []
    polls = {"n": 0}

    def call(url, method, params):
        seen.append((url, method))
        if method == "sendTransaction":
            raise AssertionError("a send leaked to the public endpoint")
        if method == "getSignatureStatuses":
            polls["n"] += 1
            if polls["n"] >= confirm_after:
                return {
                    "result": {
                        "value": [
                            {"confirmationStatus": "confirmed", "slot": 7, "err": None}
                        ]
                    }
                }
            return {"result": {"value": [None]}}
        if method == "getBlockHeight":
            return {"result": height}
        raise AssertionError(method)

    return call, seen


def test_every_rebroadcast_goes_to_the_endpoint_of_the_first_send() -> None:
    """Five rebroadcast ticks, all to Helius; a copy landing via public RPC first
    would make the backrun bundle fail."""
    from urllib.parse import parse_qs, urlsplit

    tx, binding = _swap_tx_with_binding()
    post = _HeliusPost()
    call, seen = _reads_only(confirm_after=6)

    out = submit_transaction_result(
        {"transaction": tx, "binding": binding, "last_valid_block_height": 100},
        rpc_call=call,
        send=_helius(post),
        sleep=lambda _s: None,
    )

    assert out["confirmed"] is True
    assert post.methods == ["sendTransaction"] * 6, "first send + 5 rebroadcasts"
    hosts = {urlsplit(url).hostname for url in post.urls}
    assert hosts == {"mainnet.helius-rpc.com"}
    rebates = {parse_qs(urlsplit(url).query)["rebate-address"][0] for url in post.urls}
    assert rebates == {RELAY}
    assert all(method != "sendTransaction" for _url, method in seen)
    assert out["rebate"] == {
        "requested": True,
        "endpoint": "helius-mainnet",
        "address": RELAY,
        "reason": out["rebate"]["reason"],
    }
    assert HELIUS_FAKE_KEY not in repr(out)


def test_a_non_swap_through_helius_is_still_sent_without_the_parameter() -> None:
    from urllib.parse import parse_qs, urlsplit

    tx, binding = _tx_with_binding()  # a memo transaction: no pool, no backrun
    post = _HeliusPost()
    call, _ = _reads_only(confirm_after=1)

    out = submit_transaction_result(
        {"transaction": tx, "binding": binding, "last_valid_block_height": 100},
        rpc_call=call,
        send=_helius(post),
        sleep=lambda _s: None,
    )

    assert out["confirmed"] is True
    assert len(post.urls) == 1
    assert "rebate-address" not in parse_qs(urlsplit(post.urls[0]).query)
    assert out["rebate"]["requested"] is False


def test_a_helius_failure_is_a_redacted_refusal() -> None:
    import urllib.error

    tx, binding = _swap_tx_with_binding()

    def post(url, body):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    call, _ = _reads_only(confirm_after=1)
    out = submit_transaction_result(
        {"transaction": tx, "binding": binding, "last_valid_block_height": 100},
        rpc_call=call,
        send=_helius(post),
        sleep=lambda _s: None,
    )
    assert out["refused"] and out["code"] == "send-failed"
    assert "HTTP 401" in out["reason"]
    assert HELIUS_FAKE_KEY not in repr(out)


def test_the_default_path_is_unchanged_call_for_call() -> None:
    """No `send`: every call goes to the default URL through rpc_call, with the exact
    params it always had, and the result carries no rebate block. A golden of the
    calls, not just the outcome."""
    from gecko.submit_transaction import DEFAULT_RPC

    tx, binding = _tx_with_binding()
    recorded: list[tuple[str, str, list]] = []
    polls = {"n": 0}

    def call(url, method, params):
        recorded.append((url, method, params))
        if method == "sendTransaction":
            return {"result": "SigFake1111"}
        if method == "getSignatureStatuses":
            polls["n"] += 1
            if polls["n"] >= 2:
                return {
                    "result": {
                        "value": [
                            {"confirmationStatus": "confirmed", "slot": 1, "err": None}
                        ]
                    }
                }
            return {"result": {"value": [None]}}
        raise AssertionError(method)

    out = submit_transaction_result(
        {"transaction": tx, "binding": binding, "last_valid_block_height": 100},
        rpc_call=call,
        sleep=lambda _s: None,
    )

    assert "rebate" not in out
    assert recorded == [
        (
            DEFAULT_RPC,
            "sendTransaction",
            [
                tx,
                {
                    "encoding": "base64",
                    "preflightCommitment": "confirmed",
                    "maxRetries": 0,
                },
            ],
        ),
        (DEFAULT_RPC, "getSignatureStatuses", [["SigFake1111"]]),
        (
            DEFAULT_RPC,
            "sendTransaction",
            [tx, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}],
        ),
        (DEFAULT_RPC, "getSignatureStatuses", [["SigFake1111"]]),
    ]


def test_operator_send_is_off_by_default_and_never_reads_the_arguments() -> None:
    from gecko.submit_transaction import operator_send

    assert operator_send({"rebate_address": "anything"}, environ={}) is None


def test_operator_send_leaves_a_caller_named_rpc_url_alone() -> None:
    from gecko.send_endpoint import PublicSend
    from gecko.submit_transaction import operator_send

    send = operator_send(
        {"rpc_url": "https://rpc.example.org"}, environ={"GECKO_SEND_REBATE": "1"}
    )
    assert isinstance(send, PublicSend)
    assert send.rpc_url == "https://rpc.example.org"
