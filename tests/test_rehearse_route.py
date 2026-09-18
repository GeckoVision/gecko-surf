"""Convert, then buy, both relay-paid, judged as one route. Offline, on the landing fake.

The convert leg is a stand-in instruction (the rehearsal test IDL's `contribute`) shaped so
the landing fake moves tokens the way a swap would: sender token account at index 4,
recipient at index 5. What is under test is the sequencing and the judgement, not a venue.
"""

from __future__ import annotations

import base64
from typing import Any

from gecko.sandbox import ephemeral_signer
from gecko.sandbox.rehearse_route import RouteLeg, rehearse_gasless_route
from gecko.store_accounts import TOKEN_PROGRAM_ID, derive_ata
from tests.test_rehearse_instruction import PROGRAM as CONVERT_PROGRAM
from tests.test_rehearse_instruction import VAULT, idl_fetch
from tests.test_sandbox_rehearse import USDC, token_account_bytes
from tests.test_sandbox_rehearse_relay import FakeRelay, _builder, _setup

USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def _convert_builder(actor: str, sender_ata: str, recipient_ata: str) -> Any:
    """The relay pays; the actor signs; accounts 4 and 5 are the two token accounts."""

    def build(**kwargs: Any) -> str:
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import Message
        from solders.pubkey import Pubkey

        pad = Pubkey.from_string(VAULT)
        instruction = Instruction(
            Pubkey.from_string(CONVERT_PROGRAM),
            b"\x00" * 8,
            [
                AccountMeta(Pubkey.from_string(actor), True, True),
                AccountMeta(pad, False, False),
                AccountMeta(pad, False, False),
                AccountMeta(pad, False, False),
                AccountMeta(Pubkey.from_string(sender_ata), False, True),
                AccountMeta(Pubkey.from_string(recipient_ata), False, True),
            ],
        )
        message = Message.new_with_blockhash(
            [instruction],
            Pubkey.from_string(kwargs["payer"]),
            Hash.from_string("6xCk4Xgb64QofLjfh5Q5sy47W5dURHagdcDWWhhoAqgo"),
        )
        return base64.b64encode(bytes([1]) + bytes(64) + bytes(message)).decode()

    return build


def test_the_route_lands_both_legs_and_the_buyer_never_holds_sol() -> None:
    fork, proof, relay_kp = _setup(price=100_000)
    relay = FakeRelay(relay_kp)
    buyer = ephemeral_signer(proof)
    usdg_ata = derive_ata(buyer.pubkey, USDG, token_program=TOKEN_2022)
    usdc_ata = derive_ata(buyer.pubkey, USDC, token_program=TOKEN_PROGRAM_ID)
    # swap_v2 creates no token accounts: the USDC account exists, empty, before leg 1.
    fork.accounts[usdc_ata] = {
        "lamports": 2_039_280,
        "data": [token_account_bytes(USDC, buyer.pubkey, 0), "base64"],
        "owner": TOKEN_PROGRAM_ID,
        "space": 165,
    }

    route = rehearse_gasless_route(
        proof,
        buyer=buyer,
        relay=relay,
        convert=RouteLeg(
            program_id=CONVERT_PROGRAM,
            instruction="contribute",
            values={"amount": 100_000, "payment_vault": VAULT},
            fund_tokens=[(USDG, 100_000, TOKEN_2022)],
            idl_fetch=idl_fetch,
            build_call=_convert_builder(buyer.pubkey, usdg_ata, usdc_ata),
        ),
        store="teststore",
        product="Water",
        rpc_call=fork,
        purchase_build_call=_builder,
    )

    assert route.convert.landed, route.convert.refusals
    assert route.purchase is not None and route.purchase.landed, route.purchase
    assert route.landed
    # The buyer's SOL account never existed, before or after either leg.
    assert route.buyer_sol.before is None and route.buyer_sol.after is None
    # The relay paid both fees.
    assert route.relay_sol.moved == -10_000
    assert (
        route.convert.relay_sol is not None and route.convert.relay_sol.moved == -5_000
    )
    assert (
        route.purchase.relay_sol is not None
        and route.purchase.relay_sol.moved == -5_000
    )
    # Leg 2 spent what leg 1 produced: nothing topped the buyer up in between.
    assert "surfnet_setTokenAccount" in [m for _u, m in fork.seen]
    token_funds = [m for _u, m in fork.seen if m == "surfnet_setTokenAccount"]
    assert len(token_funds) == 1, "one cheatcode funding: the USDG for the convert"
    assert route.purchase.buyer_token is not None
    assert route.purchase.buyer_token.moved == -100_000
    # The one objection the fake cannot satisfy is the program's receipt row.
    assert len(route.objections) == 1 and "receipt" in route.objections[0]
    assert len(relay.asked) == 2, "the relay signed once per leg"
    assert len(fork.sent) == 2


def test_a_convert_that_does_not_land_stops_the_route_before_the_purchase() -> None:
    fork, proof, relay_kp = _setup(price=100_000)
    buyer = ephemeral_signer(proof)
    route = rehearse_gasless_route(
        proof,
        buyer=buyer,
        relay=FakeRelay(relay_kp, tamper="bm90LWEtdHJhbnNhY3Rpb24="),
        convert=RouteLeg(
            program_id=CONVERT_PROGRAM,
            instruction="contribute",
            values={"amount": 100_000, "payment_vault": VAULT},
            fund_tokens=[(USDG, 100_000, TOKEN_2022)],
            idl_fetch=idl_fetch,
            build_call=_convert_builder(buyer.pubkey, VAULT, VAULT),
        ),
        store="teststore",
        product="Water",
        rpc_call=fork,
        purchase_build_call=_builder,
    )
    assert not route.convert.landed
    assert route.purchase is None
    assert not route.landed
    assert any("did not land" in line for line in route.objections)
    assert fork.sent == []
