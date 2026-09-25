"""The purchase is built HERE, pinned to the bytes mainnet landed.

Measured 2026-09-25, 06:20 UTC: six identical ``/build`` requests to Orquestra, five
answered ``HTTP 500`` — "Failed to fetch recent blockhash: RPC request failed: HTTP 429" —
and one ``200``. Their upstream RPC, for a blockhash Gecko discards. Every purchase on
Claude web and on Grok died at that call that morning. These tests are the reason it
cannot happen again: the instruction is encoded from the IDL, offline, and compared
byte for byte with a transaction that landed on mainnet.

The reference is signature ``4WmhBh4yZewsWZnASvBAtHwxXwtuBkCmF3uM2rQ5v165M8bjkkt4hut85WcVRBZEWV3Y832GZNhPiC7YCmTXjnRN``
(``docs/mainnet-ledger.jsonl``, leg 2 of the first gasless route, 2026-09-20): the relay
``6Q5Ki3…`` paid the fee, the buyer ``GpaLFM…`` signed the spend, ``geckocoffee`` sold one
``Espresso`` to table 1. Decoded with solders from ``getTransaction`` (base64), not
transcribed by hand.
"""

from __future__ import annotations

import base64

import pytest

from gecko.prepare_purchase import prepare_purchase_result
from gecko.providers.let_me_buy_build import (
    LOCAL_BUILDER_ID,
    MAKE_PURCHASE_ACCOUNTS,
    MAKE_PURCHASE_DISCRIMINATOR,
    LocalBuildError,
    build_make_purchase,
    encode_make_purchase_args,
    make_purchase_instruction,
)
from test_prepare_purchase_tool import BUYER, FakeRpc, _blockhash_of

# ---- what mainnet landed (signature 4WmhBh4y…) ------------------------------------------

LANDED_DATA_HEX = (
    "c13ee38869d4c914"  # discriminator
    "0b0000006765636b6f636f66666565"  # "geckocoffee" (u32 len 11 + bytes)
    "08000000457370726573736f"  # "Espresso" (u32 len 8 + bytes)
    "01"  # table_number u8
)
RELAY = "6Q5Ki322q5tRxyzfU9uAeAXsN3DzoMV8W9C8xAVTtiq9"
LANDED_BUYER = "GpaLFMwQWh2xuBkMQGKmcYT5A1WgYJekofu6DJjp8W9c"
LANDED_ACCOUNTS = {
    "receipts": "HVkbYf9PBF49WVViFf7eM1VescsgRHNeu4XJv1XveC8x",
    "signer": LANDED_BUYER,
    "authority": "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy",
    "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "sender_token_account": "DwwMEu6CYdevytmKM68xEfmBVtzrGQetUhNZyvbUKKvQ",
    "recipient_token_account": "AzNx1xhhXNAWueYhuusvzYessN23RNXk1xfmU7iJ5rjB",
    "token_program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "system_program": "11111111111111111111111111111111",
    "associated_token_program": "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
}
LANDED_ARGS = {
    "store_name": "geckocoffee",
    "product_name": "Espresso",
    "table_number": 1,
}
#: The landed message's account keys, in order, BEFORE the relay's own Lighthouse
#: assertion (key 11), which is the relay's instruction and not the purchase.
LANDED_KEYS = [
    RELAY,
    LANDED_BUYER,
    LANDED_ACCOUNTS["receipts"],
    LANDED_ACCOUNTS["authority"],
    LANDED_ACCOUNTS["sender_token_account"],
    LANDED_ACCOUNTS["recipient_token_account"],
    LANDED_ACCOUNTS["mint"],
    LANDED_ACCOUNTS["token_program"],
    LANDED_ACCOUNTS["system_program"],
    LANDED_ACCOUNTS["associated_token_program"],
    "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya",
]
#: The instruction's account indexes into that key list, as landed.
LANDED_IX_ACCOUNT_INDEXES = [2, 1, 3, 6, 4, 5, 7, 8, 9]


def _decode_legacy(tx_base64: str):  # type: ignore[no-untyped-def]
    from solders.transaction import Transaction

    return Transaction.from_bytes(base64.b64decode(tx_base64))


def test_the_discriminator_is_anchors_and_matches_the_chain() -> None:
    assert MAKE_PURCHASE_DISCRIMINATOR.hex() == "c13ee38869d4c914"


def test_the_args_encode_to_the_bytes_mainnet_landed() -> None:
    assert (
        encode_make_purchase_args("geckocoffee", "Espresso", 1).hex() == LANDED_DATA_HEX
    )


def test_the_instruction_carries_the_idl_order_and_flags() -> None:
    ix = make_purchase_instruction(LANDED_ACCOUNTS, LANDED_ARGS)
    assert bytes(ix.data).hex() == LANDED_DATA_HEX
    assert str(ix.program_id) == "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
    got = [(str(m.pubkey), m.is_signer, m.is_writable) for m in ix.accounts]
    want = [
        (LANDED_ACCOUNTS[name], is_signer, is_writable)
        for name, is_signer, is_writable in MAKE_PURCHASE_ACCOUNTS
    ]
    assert got == want
    # the IDL's flags, as the chain accepted them
    assert [m.is_writable for m in ix.accounts] == [
        True,
        True,
        True,
        False,
        True,
        True,
        False,
        False,
        False,
    ]
    assert [m.is_signer for m in ix.accounts] == [
        False,
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    ]


def test_a_relay_paid_build_resolves_to_the_landed_instruction() -> None:
    """Same program, same data, same accounts in the same positions as what landed.

    The MESSAGE key table is not compared: it is a compiler's choice (web3.js keeps
    first-appearance order inside each class, solana-sdk sorts by pubkey) and the
    program never sees it — it sees the instruction's accounts, by position, after the
    table is resolved. That resolved sequence is what must match, and it does.
    """
    built = build_make_purchase(
        {"accounts": LANDED_ACCOUNTS, "args": LANDED_ARGS, "feePayer": RELAY}
    )
    assert built.encoding == "base64"
    tx = _decode_legacy(built.tx)
    message = tx.message
    keys = [str(k) for k in message.account_keys]
    assert keys[:2] == [RELAY, LANDED_BUYER], "payer first, then the buyer: the signers"
    assert set(keys) == set(LANDED_KEYS)
    assert message.header.num_required_signatures == 2
    assert message.header.num_readonly_signed_accounts == 0
    # mint, token, system, ata, program — the landed tx had one more (Lighthouse)
    assert message.header.num_readonly_unsigned_accounts == 5
    (ix,) = message.instructions
    assert str(message.account_keys[ix.program_id_index]) == LANDED_KEYS[-1]
    resolved = [keys[i] for i in ix.accounts]
    assert resolved == [LANDED_KEYS[i] for i in LANDED_IX_ACCOUNT_INDEXES]
    assert bytes(ix.data).hex() == LANDED_DATA_HEX
    # unsigned: two zero slots, one per required signature
    assert len(tx.signatures) == 2
    assert all(bytes(s) == bytes(64) for s in tx.signatures)


def test_a_self_paid_build_has_one_signer() -> None:
    built = build_make_purchase(
        {"accounts": LANDED_ACCOUNTS, "args": LANDED_ARGS, "feePayer": LANDED_BUYER}
    )
    message = _decode_legacy(built.tx).message
    assert message.header.num_required_signatures == 1
    assert str(message.account_keys[0]) == LANDED_BUYER


@pytest.mark.parametrize(
    "plan, needle",
    [
        ({"accounts": {}, "args": LANDED_ARGS, "feePayer": RELAY}, "`receipts`"),
        (
            {
                "accounts": LANDED_ACCOUNTS,
                "args": {"store_name": "x"},
                "feePayer": RELAY,
            },
            "`product_name`",
        ),
        (
            {"accounts": LANDED_ACCOUNTS, "args": LANDED_ARGS, "feePayer": ""},
            "feePayer",
        ),
        (
            {
                "accounts": {**LANDED_ACCOUNTS, "mint": "not-a-key"},
                "args": LANDED_ARGS,
                "feePayer": RELAY,
            },
            "`mint`",
        ),
    ],
)
def test_a_plan_that_cannot_be_encoded_names_what_is_missing(
    plan: dict[str, object], needle: str
) -> None:
    with pytest.raises(LocalBuildError, match=needle):
        build_make_purchase(plan)


@pytest.mark.parametrize("table", [-1, 256, True, "1"])
def test_a_table_number_that_is_not_a_u8_is_refused(table: object) -> None:
    with pytest.raises(LocalBuildError):
        encode_make_purchase_args("geckocoffee", "Espresso", table)  # type: ignore[arg-type]


def test_prepare_purchase_builds_locally_when_no_builder_is_injected() -> None:
    """The default path: no build_call, no network beyond the fake RPC, a passing receipt.

    Before this module the default was Orquestra ``/build`` over HTTP; a test without an
    injected builder would have tried the internet. Now it must not.
    """
    rpc = FakeRpc()
    out = prepare_purchase_result(
        {"store": "jonasbar", "product": "Water", "buyer": BUYER, "network": "mainnet"},
        rpc_call=rpc,
    )
    assert out["refused"] is False, out
    assert out["status"] == "pass"
    assert out["instruction"]["built_by"] == LOCAL_BUILDER_ID
    assert "orquestra" not in out["instruction"]["built_by"].lower()

    tx = _decode_legacy(out["transaction"]["unsigned_transaction"])
    (ix,) = tx.message.instructions
    assert bytes(ix.data) == encode_make_purchase_args("jonasbar", "Water", 0)
    # the fresh blockhash was stamped over the placeholder, as it was over the stale one
    assert _blockhash_of(out["transaction"]["unsigned_transaction"]) == rpc_blockhash(
        rpc
    )
    # only the node was spoken to: store read, blockhash, height, buyer balance, simulate
    assert "simulateTransaction" in rpc.calls
    assert out["expires"]["blockhash"] == rpc_blockhash(rpc)


def rpc_blockhash(rpc: FakeRpc) -> str:
    from test_prepare_purchase_tool import FRESH_BLOCKHASH

    return FRESH_BLOCKHASH
