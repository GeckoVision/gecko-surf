"""`plan_swap` for Whirlpool — offline, every seam injected, $0.

The plan this produces is the one four real mainnet `swap_v2` transactions were built
from (all landed, all with exact CU predictions) — the tests pin the assembly rules that
made those land rather than re-deriving them from hope.
"""

import base64

import pytest

from gecko.pay_route import SWAP_SLIPPAGE_BPS
from gecko.providers.whirlpool import (
    WHIRLPOOL_INTENTS,
    WHIRLPOOL_STARTS,
    WhirlpoolPlanError,
    plan_swap,
)

CONFIG = "2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ"
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
USER = "Ebd5bk3dxEdfHsc3bVt1u1Mob5tVGfDebzaivS6ipAdh"
VAULT_A = "6j9UtMmzmWuLu45XXmdUXN3NJBdiicxxoBEex8jUs3j6"
VAULT_B = "5Sokmb48nt8aH8TnnkrAcVea4SdRqGU3qTxhRFvTHJyn"
DISC = bytes([63, 149, 209, 12, 225, 128, 99, 9])

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58_decode(value: str) -> bytes:
    n = 0
    for ch in value:
        n = n * 58 + _ALPHABET.index(ch)
    raw = n.to_bytes(32, "big") if n else b"\x00" * 32
    pad = len(value) - len(value.lstrip("1"))
    return (b"\x00" * pad + raw)[-32:]


def _idl() -> dict:
    fields = [
        {"name": "whirlpools_config", "type": "pubkey"},
        {"name": "tick_spacing", "type": "u16"},
        {"name": "fee_rate", "type": "u16"},
        {"name": "liquidity", "type": "u128"},
        {"name": "sqrt_price", "type": "u128"},
        {"name": "tick_current_index", "type": "i32"},
        {"name": "token_mint_a", "type": "pubkey"},
        {"name": "token_vault_a", "type": "pubkey"},
        {"name": "token_mint_b", "type": "pubkey"},
        {"name": "token_vault_b", "type": "pubkey"},
    ]
    return {
        "accounts": [{"name": "Whirlpool", "discriminator": list(DISC)}],
        "types": [{"name": "Whirlpool", "type": {"kind": "struct", "fields": fields}}],
    }


def _pool_blob(layout, *, tick_current: int = 2) -> bytes:
    size = max(o + w for o, w, _ in layout.fields.values())
    buf = bytearray(size)
    buf[0:8] = DISC

    def put(name, value, *, pub=False, signed=False):
        off, width, _ = layout.fields[name]
        buf[off : off + width] = (
            _b58_decode(value)
            if pub
            else int(value).to_bytes(width, "little", signed=signed)
        )

    put("whirlpools_config", CONFIG, pub=True)
    put("token_mint_a", USDG, pub=True)
    put("token_mint_b", USDC, pub=True)
    put("token_vault_a", VAULT_A, pub=True)
    put("token_vault_b", VAULT_B, pub=True)
    put("tick_spacing", 1)
    put("fee_rate", 100)
    put("liquidity", 10**12)
    put("sqrt_price", 2**64)  # ~1:1
    put("tick_current_index", tick_current, signed=True)
    return bytes(buf)


def _fake_rpc(layout, pool_address: str, *, atas_exist: bool = True):
    blob = base64.b64encode(_pool_blob(layout)).decode()

    def call(url, method, params):
        if method == "getProgramAccounts":
            filters = params[1]["filters"]
            # honour the mint memcmp: only the (USDG, USDC) ordering matches
            if filters[1]["memcmp"]["bytes"] == USDG:
                return {
                    "result": [
                        {"pubkey": pool_address, "account": {"data": [blob, "base64"]}}
                    ]
                }
            return {"result": []}
        if method == "getAccountInfo":
            target = params[0]
            if target == pool_address:
                return {"result": {"value": {"data": [blob, "base64"], "owner": "x"}}}
            if target == USDG:
                return {
                    "result": {"value": {"owner": TOKEN_2022, "data": ["", "base64"]}}
                }
            if target == USDC:
                return {"result": {"value": {"owner": CLASSIC, "data": ["", "base64"]}}}
            # ATA existence probes
            return {"result": {"value": {"owner": "t"} if atas_exist else None}}
        raise AssertionError(f"unscripted method {method}")

    return call


def _real_pool_address(layout) -> str:
    from gecko.pda import derive_pda
    from gecko.provider_config import load_packaged_provider

    _, apis = load_packaged_provider("orquestra")
    recipe = dict(apis["whirlpool"].program.pdas)["whirlpool"]
    return derive_pda(
        recipe,
        {
            "whirlpools_config": CONFIG,
            "token_mint_a": USDG,
            "token_mint_b": USDC,
            "tick_spacing": 1,
        },
    ).address


def _plan(**overrides):
    layout_idl = _idl()
    from gecko.whirlpool_venue import whirlpool_layout

    layout = whirlpool_layout(layout_idl)
    pool = _real_pool_address(layout)
    kwargs = dict(
        args={
            "input_mint": USDG,
            "output_mint": USDC,
            "user": USER,
            "amount_in": 98_928,
        },
        rpc_url="http://fake",
        rpc_call=_fake_rpc(layout, pool, atas_exist=overrides.pop("atas_exist", True)),
        idl_fetch=lambda _p: layout_idl,
    )
    kwargs["args"] = {**kwargs["args"], **overrides.pop("args", {})}
    kwargs.update(overrides)
    return plan_swap(**kwargs)


def test_the_plan_carries_every_value_swap_v2_needs() -> None:
    plan = _plan()
    v = plan["values"]
    assert plan["instruction"] == "swap_v2"  # never v1 — it cannot carry Token-2022
    assert v["token_program_a"] == TOKEN_2022  # read from the mint, never assumed
    assert v["token_program_b"] == CLASSIC
    assert v["a_to_b"] is True
    assert {"tick_array_0", "tick_array_1", "tick_array_2"} <= set(v)
    assert len({v["tick_array_0"], v["tick_array_1"], v["tick_array_2"]}) == 3
    assert v["amount"] == 98_928
    assert v["other_amount_threshold"] > 0
    assert int(v["sqrt_price_limit"]) < 2**64  # bounded in the direction of travel


def test_each_ata_derives_under_its_own_mints_program() -> None:
    from gecko.store_accounts import derive_ata

    v = _plan()["values"]
    assert v["token_owner_account_a"] == derive_ata(
        USER, USDG, token_program=TOKEN_2022
    )
    assert v["token_owner_account_b"] == derive_ata(USER, USDC, token_program=CLASSIC)
    assert v["token_owner_account_a"] != v["token_owner_account_b"]


def test_the_floor_is_sized_under_the_one_shared_bound() -> None:
    from gecko.whirlpool_math import quote_min_amount_out

    plan = _plan()
    assert plan["quote"]["slippage_bps"] == SWAP_SLIPPAGE_BPS
    _spot, floor = quote_min_amount_out(
        98_928, 2**64, 100, a_to_b=True, slippage_bps=SWAP_SLIPPAGE_BPS
    )
    assert plan["values"]["other_amount_threshold"] == floor


def test_a_missing_ata_is_named_with_its_fix_not_discovered_as_a_3012() -> None:
    plan = _plan(atas_exist=False)
    assert len(plan["missing_atas"]) == 2
    for entry in plan["missing_atas"]:
        assert entry["fix"].startswith("spl-token create-account ")


def test_an_absurd_bound_is_refused() -> None:
    with pytest.raises(WhirlpoolPlanError):
        _plan(args={"slippage_bps": 10_000})


def test_a_nonpositive_amount_is_refused() -> None:
    with pytest.raises(WhirlpoolPlanError):
        _plan(args={"amount_in": 0})


def test_the_intent_is_registered_so_a_hosted_agent_can_reach_it() -> None:
    """The gap this module closes: the packaged config declared plan_swap all along and
    the registry never had the key, so find_start skipped the card and a hosted agent
    could receive a route it could not execute."""
    from gecko.providers.cli import intent_registries, start_specs

    assert "plan_swap" in WHIRLPOOL_INTENTS
    assert intent_registries()["whirlpool"] is WHIRLPOOL_INTENTS
    assert "plan_swap" in start_specs()["whirlpool"]
    assert "plan_swap" in WHIRLPOOL_STARTS


# --- the hosted tool ------------------------------------------------------------------


def _result(args, **kw):
    from gecko.providers.whirlpool import plan_swap_result
    from gecko.whirlpool_venue import whirlpool_layout

    layout_idl = _idl()
    layout = whirlpool_layout(layout_idl)
    pool = _real_pool_address(layout)
    kw.setdefault("rpc_call", _fake_rpc(layout, pool))
    kw.setdefault("idl_fetch", lambda _p: layout_idl)
    kw.setdefault("url_guard", lambda _u: None)
    base = {
        "input_mint": USDG,
        "output_mint": USDC,
        "user": USER,
        "amount_in": 150_000,
        "network": "fork",
        "rpc_url": "http://127.0.0.1:1",
    }
    base.update(args)
    return plan_swap_result(base, **kw)


def test_the_hosted_tool_is_listed_after_plan_payment() -> None:
    """plan_swap sits beside the decision tool it executes for, and the free-browse
    ordering invariant survives the insertion."""
    from gecko.providers.catalog_surface import OrquestraCatalogSurface

    names = [t["name"] for t in OrquestraCatalogSurface().list_tools()]
    assert "plan_swap" in names
    assert names.index("plan_swap") == names.index("plan_payment") + 1
    assert names.index("try_purchase") == names.index("prepare_purchase") + 1


def test_the_hosted_tool_plans_end_to_end_offline() -> None:
    out = _result({})
    assert "error" not in out
    assert out["instruction"] == "swap_v2"
    assert out["values"]["token_program_a"] == TOKEN_2022
    assert out["network"] == "fork"
    assert out["next_tool"] == "prepare_instruction"


def test_a_malformed_wallet_never_reaches_the_network() -> None:
    calls = []

    def rpc(url, method, params):
        calls.append(method)
        return {}

    out = _result({"user": "not a pubkey"}, rpc_call=rpc)
    assert "error" in out
    assert calls == []


def test_a_loopback_rpc_url_is_refused_without_the_injected_guard() -> None:
    from gecko.providers.whirlpool import plan_swap_result

    out = plan_swap_result(
        {
            "input_mint": USDG,
            "output_mint": USDC,
            "user": USER,
            "amount_in": 1000,
            "network": "fork",
            "rpc_url": "http://127.0.0.1:8899",
        }
    )
    assert "rpc_url refused" in out.get("error", "")


def test_a_plan_refusal_is_marked_refused_with_its_reason() -> None:
    """WhirlpoolPlanError is an honest refusal, not a transport failure — the caller
    must be able to tell 'no pool survived re-derivation' from 'the node was down'."""

    def no_pools(url, method, params):
        if method == "getProgramAccounts":
            return {"result": []}
        raise AssertionError(method)

    out = _result({}, rpc_call=no_pools)
    assert out.get("refused") is True
    assert "re-derivation" in out["error"]


def test_the_description_says_it_does_not_consult_the_peg() -> None:
    """The honesty line: this tool executes an explicitly-requested conversion;
    plan_payment is the one that DECIDES, and its gate can refuse. Hiding that split
    would let a card read as peg-checked when nothing checked it."""
    from gecko.providers.whirlpool import PLAN_SWAP_TOOL

    text = PLAN_SWAP_TOOL["description"]
    assert "NOT" in text and "peg" in text
    assert "plan_payment" in text


def test_the_plan_carries_the_execution_breadcrumb() -> None:
    """The first web session had the plan and still executed through the wallet's
    aggregator — the only swap path carrying instructions. Now this one carries them:
    build -> sign -> verify -> submit, with the signer named and the cold-client
    warning attached where the budget actually burns."""
    out = _result({})
    steps = [s["step"] for s in out["next_steps"]]
    assert steps == ["build", "sign", "verify", "submit"]
    sign = out["next_steps"][1]["note"]
    assert "request_wallet_sign" in sign
    assert "BEFORE" in sign
    verify = out["next_steps"][2]
    assert verify["tool"] == "verify_signed_transaction"


def test_plan_swap_schema_declares_the_pool_pin() -> None:
    """The plan_payment→plan_swap rail hands the agent a checked pool; a schema
    that hides the parameter leaves the venue re-derivation to chance."""
    from gecko.providers.whirlpool import PLAN_SWAP_TOOL

    properties = PLAN_SWAP_TOOL["inputSchema"]["properties"]
    assert "pool" in properties
    assert "route.quote.pool" in properties["pool"]["description"]
