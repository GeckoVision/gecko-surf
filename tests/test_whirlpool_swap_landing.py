"""The Whirlpool swap_v2 landing orchestrator (gecko/providers/whirlpool_landing.py).

Offline, $0, Pattern B: the pool read and the ATA-existence probes come from the same
canned fake `test_providers_whirlpool` already uses, a canned `simulateTransaction` is
added on top, and an injected fetch stands in for Orquestra `/build`. Nothing here
reaches the network.

Whirlpool is the endpoint with real mainnet swaps behind it and, until this module, the
one that could not be PROVEN: no dispatch key meant `gecko prove` could not run it and
`gecko watch` could not notice the day it broke.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.providers.whirlpool_landing import (
    WhirlpoolLandingError,
    build_url,
    simulate_swap_v2_landing,
)

from test_providers_whirlpool import (  # the shared canned-pool fixtures
    USDC,
    USDG,
    USER,
    VAULT_A,
    VAULT_B,
    _fake_rpc,
    _idl,
    _real_pool_address,
)
from gecko.whirlpool_venue import whirlpool_layout

WSOL = "So11111111111111111111111111111111111111112"


def _layout():
    return whirlpool_layout(_idl())


def _rpc(*, atas_exist: bool = True, err: Any = None):
    """The planner's fake, plus the one method a landing bundle needs."""
    layout = _layout()
    pool = _real_pool_address(layout)
    inner = _fake_rpc(layout, pool, atas_exist=atas_exist)

    def call(url, method, params):
        if method == "simulateTransaction":
            return {
                "result": {
                    "context": {"slot": 440_510_159},
                    "value": {
                        "err": err,
                        "unitsConsumed": 87_412,
                        "logs": [],
                        "accounts": None,
                    },
                }
            }
        if method in ("getLatestBlockhash", "getFeeForMessage"):
            return {
                "result": {
                    "context": {"slot": 440_510_159},
                    "value": {
                        "blockhash": "11111111111111111111111111111111",
                        "lastValidBlockHeight": 1,
                        **({"value": 5000} if method == "getFeeForMessage" else {}),
                    },
                }
            }
        return inner(url, method, params)

    return call


def _canned_swap(accounts: dict[str, Any], args: dict[str, Any], fee_payer: str):
    """An Orquestra-shaped built swap_v2: named accounts in a positional list.

    The ORDER here is this fixture's, not Whirlpool's. That is the honest limit of an
    offline test and exactly what the module's docstring says: the authoritative order
    lives in the live IDL, so this proves the ASSEMBLY around the instruction, never
    that the instruction itself is correctly ordered.
    """
    order = [
        ("token_program_a", False, False),
        ("token_program_b", False, False),
        ("token_authority", True, False),
        ("whirlpool", False, True),
        ("token_mint_a", False, False),
        ("token_mint_b", False, False),
        ("token_owner_account_a", False, True),
        ("token_vault_a", False, True),
        ("token_owner_account_b", False, True),
        ("token_vault_b", False, True),
        ("tick_array_0", False, True),
        ("tick_array_1", False, True),
        ("tick_array_2", False, True),
        ("oracle", False, True),
    ]
    missing = [name for name, _, _ in order if name not in accounts]
    assert not missing, f"the orchestrator did not supply {missing}"
    return {
        "name": "swap_v2",
        "programId": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
        "data": "2b04ed0b1ac91e62",
        "accounts": [
            {"pubkey": accounts[name], "isSigner": s, "isWritable": w}
            for name, s, w in order
        ],
    }


def _run(**over):
    kwargs: dict[str, Any] = dict(
        bindings={
            "input_mint": USDG,
            "output_mint": USDC,
            "user": USER,
            "amount_in": 98_928,
        },
        rpc_url="http://fake",
        rpc_call=_rpc(atas_exist=over.pop("atas_exist", True)),
        idl_fetch=lambda _p: _idl(),
        fetch_instruction=_canned_swap,
        include_derive_only=False,
    )
    kwargs.update(over)
    return simulate_swap_v2_landing(**kwargs)


# --- the orchestrator supplies what plan_swap does not ---------------------------


def test_it_supplies_the_two_accounts_the_planner_does_not() -> None:
    """`oracle` is derived from the packaged recipe and `token_authority` is the signer.

    `_canned_swap` asserts on every name it needs, so this passing IS the assertion —
    but pin it explicitly, because a build that silently dropped a slot would otherwise
    read as a pass.
    """
    seen: dict[str, Any] = {}

    def capture(accounts, args, fee_payer):
        seen.update(accounts)
        return _canned_swap(accounts, args, fee_payer)

    _run(fetch_instruction=capture)
    assert seen["token_authority"] == USER
    assert seen["oracle"] and seen["oracle"] != seen["whirlpool"]


def test_accounts_and_args_are_split_by_NAME_not_by_type() -> None:
    """`a_to_b` is a bool and `sqrt_price_limit` an int; no type rule separates those
    from an account, which is why the split is a declared name set."""
    captured: dict[str, dict] = {}

    def capture(accounts, args, fee_payer):
        captured["accounts"], captured["args"] = dict(accounts), dict(args)
        return _canned_swap(accounts, args, fee_payer)

    _run(fetch_instruction=capture)
    assert set(captured["args"]) == {
        "amount",
        "other_amount_threshold",
        "sqrt_price_limit",
        "amount_specified_is_input",
        "a_to_b",
        "remaining_accounts_info",
    }
    assert "a_to_b" not in captured["accounts"]
    assert captured["accounts"]["token_vault_a"] == VAULT_A
    assert captured["accounts"]["token_vault_b"] == VAULT_B


# --- the gap this module exists to close -----------------------------------------


def test_a_missing_ata_becomes_a_prelude_instead_of_a_3012() -> None:
    """swap_v2 creates NO token accounts, including the one you only receive into.
    Anchor calls that 3012 AccountNotInitialized, which names a slot and no fix."""
    out = _run(atas_exist=False)
    assert out.created_atas, "a missing ATA must be created, not discovered in the sim"
    assert out.landing_receipt.status == "pass"


def test_nothing_is_created_when_both_accounts_already_exist() -> None:
    """A CreateIdempotent on an existing account is a no-op, but paying for one anyway
    makes the CU figure a worse prediction of the real transaction."""
    out = _run(atas_exist=True)
    assert out.created_atas == []


def test_the_quote_travels_with_the_receipt() -> None:
    out = _run()
    assert out.min_amount_out > 0
    assert out.expected_out_spot >= out.min_amount_out, (
        "the floor must sit at or below the spot quote, never above it"
    )
    assert out.pool and out.direction


# --- refusals ---------------------------------------------------------------------


@pytest.mark.parametrize("drop", ["input_mint", "output_mint", "user", "amount_in"])
def test_a_missing_binding_is_named_not_guessed(drop: str) -> None:
    bindings = {
        "input_mint": USDG,
        "output_mint": USDC,
        "user": USER,
        "amount_in": 1_000,
    }
    bindings.pop(drop)
    with pytest.raises(WhirlpoolLandingError) as err:
        _run(bindings=bindings)
    assert drop in str(err.value)


def test_the_build_url_is_composed_from_the_packaged_config() -> None:
    """Not a literal. A hardcoded URL is a second place the project id can be wrong,
    and the config is the first."""
    url = build_url()
    assert url.startswith("https://api.orquestra.dev/api/")
    assert url.endswith("/instructions/swap_v2/build")
    assert "4tj9k6117wn4xla89szsj" in url, "the project slug comes from whirlpool.json"


# --- the reason this module exists: it is now reachable ---------------------------


def test_whirlpool_is_dispatchable_from_both_tables() -> None:
    """R5 was the gate's complaint: no dispatch key meant no prove and no drift-watch
    for the only program with real mainnet swaps behind it."""
    from gecko.ingest_gate import gate
    from gecko.prove import landing_table

    assert ("whirlpool", "swap_v2") in landing_table()
    check = next(
        c for c in gate("whirlpool").checks if c.name == "registry-consistency"
    )
    assert check.outcome == "ok", check.detail
