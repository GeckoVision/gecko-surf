"""Jupiter — the call that needs two surfaces.

Offline first (Pattern B): the HTTP surface is an injected fake, so every claim below is
falsifiable with no network and no RPC. The live fork run is the final check, not the
debugger — it is env-gated at the bottom.

The finding these tests pin: the program surface declares **9** accounts for `route`; the
instruction that lands carries **25**. The other 16 are route legs that only exist once an
HTTP quote picks a venue, so no IDL — however good — can hold them.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Mapping

import pytest

from gecko.providers.jupiter_landing import (
    DECLARED_ROUTE_ACCOUNTS,
    JUPITER_PROGRAM,
    JupiterLandingError,
    jupiter_instruction_to_solders,
    plan_route,
    quote_route,
)

USER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
HYUSD = "5YMkXAYccHSGnHn9nob9xEvv6Pvka9DZWH7nTbotTu9E"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Jupiter's event authority — derived from seeds ["__event_authority"], which the program
# surface reports as `pda: null`.
EVENT_AUTHORITY = "D8cy77BBepLMngZx6ZukaTff5hCt1HrWyKk3Hnd9oitf"

_FILLER = [
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
]


def _account(pubkey: str) -> dict[str, Any]:
    return {"pubkey": pubkey, "isSigner": False, "isWritable": False}


def _fake_quote(hops: int = 1) -> dict[str, Any]:
    return {
        "inAmount": "10000000",
        "outAmount": "10019563",
        "priceImpactPct": "0.0002",
        "routePlan": [
            {"percent": 100, "swapInfo": {"label": "Whirlpool", "ammKey": "4tJW2axb"}}
            for _ in range(hops)
        ],
    }


def _fake_swap_instructions(account_count: int = 25) -> dict[str, Any]:
    """25 accounts: the 9 declared (with event_authority in its declared slot) + legs."""
    declared = [
        _account(EVENT_AUTHORITY if n == "event_authority" else p)
        for n, p in zip(
            DECLARED_ROUTE_ACCOUNTS, [f"{i}" * 0 or _FILLER[i % 3] for i in range(9)]
        )
    ]
    legs = [_account(_FILLER[i % 3]) for i in range(account_count - len(declared))]
    return {
        "swapInstruction": {
            "programId": JUPITER_PROGRAM,
            "accounts": declared + legs,
            "data": base64.b64encode(b"\x01\x02\x03").decode(),
        },
        "setupInstructions": [],
        "addressLookupTableAddresses": ["GnMsEEyF6XKMajwtBsjxcBv8QoEM71QyUzz4Lf7vkeRu"],
    }


def _get(_url: str) -> Mapping[str, Any]:
    return _fake_quote()


def _post(_url: str, _body: Mapping[str, Any]) -> Mapping[str, Any]:
    return _fake_swap_instructions()


BINDINGS = {
    "input_mint": HYUSD,
    "output_mint": USDC,
    "amount": 10_000_000,
    "user": USER,
}


# --------------------------------------------------------------------------- #
# The finding.
# --------------------------------------------------------------------------- #
def test_the_program_surface_declares_nine_accounts() -> None:
    """The baseline we measure the gap against. Not a criticism of the surface — this is
    genuinely everything the program declares."""
    assert len(DECLARED_ROUTE_ACCOUNTS) == 9


def test_the_instruction_that_lands_needs_far_more_than_the_surface_declares() -> None:
    plan = plan_route(BINDINGS, http_get=_get, http_post=_post)

    assert len(plan["accounts"]) == 25
    assert len(plan["accounts"]) > plan["declared_account_count"]
    assert plan["lookup_tables"]  # and an address lookup table on top


def test_every_account_carries_the_origin_we_can_defend() -> None:
    """Three origins, and the majority come from neither the IDL nor its source."""
    plan = plan_route(BINDINGS, http_get=_get, http_post=_post)
    counts: dict[str, int] = {}
    for account in plan["accounts"]:
        counts[account.provenance] = counts.get(account.provenance, 0) + 1

    assert counts["cross_surface"] == 16
    assert (
        counts["recovered"] == 1
    )  # event_authority — seeds the surface reports as null
    assert counts["extracted"] == 8
    assert sum(counts.values()) == 25


def test_the_event_authority_is_recovered_not_extracted() -> None:
    """The surface says `pda: null`; the seed is a const in program source. Claiming the
    IDL gave it to us would be a lie about provenance, which is the one thing this ladder
    exists to prevent."""
    plan = plan_route(BINDINGS, http_get=_get, http_post=_post)
    authority = [a for a in plan["accounts"] if a.pubkey == EVENT_AUTHORITY]

    assert authority and all(a.provenance == "recovered" for a in authority)


def test_no_route_is_an_honest_error_not_a_fabricated_plan() -> None:
    """When the HTTP surface has no route, we refuse — we do not invent legs."""

    def empty(_url: str) -> Mapping[str, Any]:
        return {"routePlan": []}

    with pytest.raises(JupiterLandingError, match="no route"):
        quote_route(HYUSD, USDC, 10_000_000, http_get=empty)


def test_missing_bindings_fail_loudly() -> None:
    with pytest.raises(JupiterLandingError, match="missing binding"):
        plan_route({"input_mint": HYUSD}, http_get=_get, http_post=_post)


# --------------------------------------------------------------------------- #
# The wire disagreement between the two surfaces.
# --------------------------------------------------------------------------- #
def test_jupiter_sends_base64_where_orquestra_sends_hex() -> None:
    """Both correct for their own API; neither declares the other's convention."""
    instruction = jupiter_instruction_to_solders(
        {
            "programId": JUPITER_PROGRAM,
            "accounts": [{"pubkey": USDC, "isSigner": False, "isWritable": True}],
            "data": base64.b64encode(b"\xde\xad\xbe\xef").decode(),
        }
    )

    assert bytes(instruction.data) == b"\xde\xad\xbe\xef"


def test_non_base64_instruction_data_is_rejected_without_echoing_it() -> None:
    with pytest.raises(JupiterLandingError) as caught:
        jupiter_instruction_to_solders(
            {"programId": JUPITER_PROGRAM, "accounts": [], "data": "not-base64!!"}
        )

    assert "not-base64" not in str(caught.value)


# --------------------------------------------------------------------------- #
# Live fork check — the FINAL step, never the debugger.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not os.getenv("GECKO_SIMULATE_E2E"),
    reason="live: needs surfpool on :8899 + network (GECKO_SIMULATE_E2E=1)",
)
def test_route_lands_on_a_fork_e2e() -> None:
    from gecko.providers.jupiter_landing import simulate_route_landing
    from gecko.rpc import default_rpc_call

    def rpc(url: str, method: str, params: list) -> dict:
        if method == "getAccountInfo":
            params = list(params)
            opts = (
                dict(params[1])
                if len(params) > 1 and isinstance(params[1], dict)
                else {}
            )
            opts["encoding"] = "base64"
            params = [params[0], opts]
        return default_rpc_call(url, method, params)

    result = simulate_route_landing(
        BINDINGS,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=rpc,
        include_derive_only=True,
    )

    assert result.landing_receipt.status == "pass"
    assert result.landing_receipt.units_consumed > 0
    # The program-surface-only call cannot land: the legs are simply absent.
    assert (
        result.derive_only_receipt is None
        or result.derive_only_receipt.status == "fail"
    )
    assert (
        result.provenance_counts["cross_surface"]
        > result.provenance_counts["extracted"]
    )
