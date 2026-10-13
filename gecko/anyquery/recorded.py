"""The transport edge of recorded mode: every outside party any-query talks to, as data.

Recorded and live share one code path (the planner, ``execute``, ``SolanaBilling``, the
service); they differ only here. Each class answers in the shape the real party answers
in, from fixtures or from the bytes it is handed, and none of them holds a key:

* :class:`RecordedOrthogonal` replays ``/v1/run`` replies per run, by (api, filled path);
* :mod:`gecko.anyquery.recorded_chain` holds the node and Helius's sender;
* :func:`fixed_rate_sol_pricer`, :func:`fixed_rate_swap_planner`,
  :func:`recorded_wallet_creator` and :func:`recorded_model_call` stand in for the pool
  reads, Privy's create and the model.

What this proves is what the engine does GIVEN such answers, not that the parties give
them. The signer is not here: a key-holding fake cannot live in ``gecko/``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..autonomous_purchase import USDC_MINT
from ..caller import PreparedRequest
from ..landing import NATIVE_SOL_MINT, TOKEN_PROGRAM_ID
from ..privy_backend import CreatedWallet
from .payment_tx import SolPricer, SolQuote, usdc_ata
from .recorded_chain import RecordedChain, RecordedHeliusPost
from .settlement_tx import SwapLeg, SwapPlanner, leg_from_values, wsol_ata

__all__ = [
    "RecordedChain",
    "RecordedHeliusPost",
    "RECORDED_KEY",
    "RecordedKeyring",
    "RecordedOrthogonal",
    "fixed_rate_sol_pricer",
    "fixed_rate_swap_planner",
    "recorded_model_call",
    "recorded_wallet_creator",
]

#: What the recorded keyring hands the Helius sender. Not a key; it only proves the
#: sender asked the keychain rather than the environment.
RECORDED_KEY = "recorded-not-a-key"
_SQRT_PRICE_LIMIT = 4_295_048_016


@dataclass
class RecordedOrthogonal:
    """``PreparedRequest -> (status, body)`` from ``runs[i]`` while run ``i`` is current.

    A request with no recorded reply is a 404: a plan that drifts from the fixture shows
    up as "no data", never as an invented value.
    """

    runs: Sequence[Sequence[Mapping[str, Any]]]
    current: int = 0
    seen: list[tuple[str, str]] = field(default_factory=list)

    def next_run(self) -> None:
        self.current = min(self.current + 1, len(self.runs) - 1)

    def __call__(self, req: PreparedRequest) -> tuple[int, Any]:
        body = req.json_body if isinstance(req.json_body, dict) else {}
        key = (str(body.get("api")), str(body.get("path")))
        self.seen.append(key)
        for row in self.runs[self.current]:
            if (row["api"], row["path"]) != key:
                continue
            status = int(row["status"])
            if status != 200:
                return status, {"success": False, "error": "recorded upstream error"}
            return 200, {
                "success": True,
                "priceCents": int(row["cents"]),
                "requestId": f"rec-{self.current}-{len(self.seen)}",
                "data": row["data"],
            }
        return 404, {}


class RecordedKeyring:
    """A keyring module for :class:`gecko.credentials.KeyringBackend` that knows one
    name, so the Helius sender resolves through the keychain path it uses live."""

    def get_keyring(self) -> object:
        return object()

    def get_password(self, service: str, user: str) -> str | None:
        return RECORDED_KEY if service == "gecko:HELIUS_API_KEY" else None


def fixed_rate_sol_pricer(*, pool: str, rate_micro: int, margin_bps: int) -> SolPricer:
    """A :data:`SolPricer` at a recorded price: the smallest lamports whose value, less
    ``margin_bps``, clears the quote, as ``whirlpool_sol_pricer`` sizes it live."""

    def price(usdc_micro: int) -> SolQuote:
        floor_rate = rate_micro * (10_000 - margin_bps) // 10_000
        lamports = -(-usdc_micro * 1_000_000_000 // floor_rate)
        return SolQuote(lamports, rate_micro, pool, margin_bps)

    return price


def fixed_rate_swap_planner(
    idl: Mapping[str, Any],
    *,
    pool: str,
    vault_a: str,
    vault_b: str,
    tick_arrays: Sequence[str],
    rate_micro: int,
    slippage_bps: int,
) -> SwapPlanner:
    """A settlement :data:`SwapPlanner` at a recorded price: exact input, the floor is
    the input's value less ``slippage_bps``, as ``whirlpool_swap_leg`` plans it live."""

    def plan(owner: str, lamports_in: int) -> SwapLeg:
        floor_rate = rate_micro * (10_000 - slippage_bps) // 10_000
        values = {
            "whirlpool": pool,
            "token_mint_a": NATIVE_SOL_MINT,
            "token_mint_b": USDC_MINT,
            "token_program_a": TOKEN_PROGRAM_ID,
            "token_program_b": TOKEN_PROGRAM_ID,
            "token_owner_account_a": wsol_ata(owner),
            "token_owner_account_b": usdc_ata(owner),
            "token_vault_a": vault_a,
            "token_vault_b": vault_b,
            "tick_array_0": tick_arrays[0],
            "tick_array_1": tick_arrays[1],
            "tick_array_2": tick_arrays[2],
            "amount": lamports_in,
            "other_amount_threshold": lamports_in * floor_rate // 1_000_000_000,
            "sqrt_price_limit": _SQRT_PRICE_LIMIT,
            "amount_specified_is_input": True,
            "a_to_b": True,
            "remaining_accounts_info": None,
        }
        return leg_from_values(idl, values, owner=owner, pool=pool)

    return plan


def recorded_wallet_creator(wallet_id: str, address: str) -> Any:
    """Privy's create, answered once with a fixed wallet. Records the idempotency keys
    it was asked with, which are all a caller can hand it."""
    asked: list[str] = []

    def create(idempotency_key: str) -> CreatedWallet:
        asked.append(idempotency_key)
        return CreatedWallet(wallet_id, address)

    create.asked = asked  # type: ignore[attr-defined]
    return create


def recorded_model_call(reply: str) -> Any:
    """A ``ModelCall`` that answers every prompt with one recorded reply."""

    def call(system: str, user: str) -> str:
        return reply

    return call
