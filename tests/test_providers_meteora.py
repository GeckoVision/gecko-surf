"""Phase 1: the Meteora Orquestra provider surface — the touchable demo.

The agent plans a swap in plain English; Gecko derives lb_pair (the root Orquestra can't)
+ the leaves, selects the bin_array remaining accounts from the pool's LIVE liquidity
bitmap, quotes min_amount_out from the active-bin price, and the plan points at
Orquestra's builder. Proves the derivation matches the real mainnet pool and the plan
carries the right execute target. The pool-state read is injected (offline, $0) with a
blob whose layout offsets are mainnet-verified (see test_meteora_math).
"""

from __future__ import annotations

import base64
import json
import os
import struct
from typing import Any

import pytest

import gecko.providers.meteora as meteora_module
from gecko.providers.meteora import (
    METEORA_PROGRAM_ID,
    build_meteora_surface,
    plan_swap,
)

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
USER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"

# Ground truth — a REAL, CURRENT (post-May-2024) mainnet DLMM pool, created with the
# `derive_lb_pair_pda2` scheme (`require_base_factor_seed == 1` on-chain). Values decoded
# from the pool's own LbPair account (mints at offsets 88/120, bin_step u16 @80, the
# base_factor_seed u16 @84) and cross-checked against MeteoraAg/dlmm-sdk's own fixture.
# This is the pool the OLD 3-seed recipe derives WRONG — the whole point of the fix.
TOKEN = "Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump"  # token_x of the pool
BIN_STEP = 250
BASE_FACTOR = 4000
CURRENT_POOL = "EtAdVRLFH22rjWh3mcUasKFF27WtHhsaCvK27tPFFWig"

# The pool's bin_array PDAs for indexes −1/0/1 — derived from
# ["bin_array", lb_pair, index(i64 LE)] and verified live on mainnet (each account
# exists, is owned by the DLMM program, and its `index` field reads back the seed).
BIN_ARRAY_M1 = "E6gur9Jw8675DCR7GpJVhoSrkruRgt8EdEVqLAc5RLUt"
BIN_ARRAY_0 = "5Sm2ecMeqohRkNpFJPWSqHL1BkA7AEW4ck8TmdF1gD4t"
BIN_ARRAY_P1 = "DTMpP9nUdBvJurud1QDKCD1vNpEe8i43ZmZ84NaMBacK"

# The pre-upgrade survivor: a 3-seed (`require_base_factor_seed == 0`) SOL/USDC pool that
# predates PR #49. It is NOT derivable by today's 4-seed recipe — it is exactly why the
# base_factor bug stayed invisible in our old tests (see test_legacy_pool_* below).
LEGACY_SOL_USDC = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"

# The real pool's active_id is −78 → active array −2, which is MISSING on-chain while
# −1/0/1 exist (live-verified) — the exact trap the bitmap walk exists for.
ACTIVE_ID = -78


def _pubkey_bytes(addr: str) -> bytes:
    from solders.pubkey import Pubkey

    return bytes(Pubkey.from_string(addr))


def _lb_pair_blob(liquidity_indexes: tuple[int, ...] = (-1, 0, 1)) -> str:
    """A canned LbPair blob for CURRENT_POOL at the mainnet-verified offsets:
    token_x = TOKEN, token_y = WSOL, active_id −78, bin_step 250."""
    raw = bytearray(904)
    struct.pack_into("<i", raw, 76, ACTIVE_ID)
    struct.pack_into("<H", raw, 80, BIN_STEP)
    raw[88:120] = _pubkey_bytes(TOKEN)
    raw[120:152] = _pubkey_bytes(WSOL)
    bitmap = 0
    for index in liquidity_indexes:
        bitmap |= 1 << (index + 512)
    for limb in range(16):
        struct.pack_into(
            "<Q", raw, 584 + 8 * limb, (bitmap >> (64 * limb)) & ((1 << 64) - 1)
        )
    return base64.b64encode(bytes(raw)).decode()


def _fake_rpc(pool_blob: str | None = None):
    blob = pool_blob or _lb_pair_blob()

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        assert method == "getAccountInfo"
        addr = params[0]
        if addr == CURRENT_POOL:
            return {"result": {"value": {"owner": "x", "data": [blob, "base64"]}}}
        if addr in (TOKEN, WSOL):
            return {"result": {"value": {"owner": TOKEN_PROGRAM}}}
        raise AssertionError(f"unexpected getAccountInfo for {addr}")

    return rpc


def _bindings() -> dict[str, Any]:
    # Buying TOKEN with SOL: input WSOL (the pool's token_y) → the walk goes UP.
    return {
        "input_mint": WSOL,
        "output_mint": TOKEN,
        "bin_step": BIN_STEP,
        "base_factor": BASE_FACTOR,
        "user": USER,
        "amount_in": 1_000_000,
    }


def test_surface_lists_derive_and_swap_intent() -> None:
    tools = {t["name"] for t in build_meteora_surface().list_tools()}
    assert {"get_program_graph", "derive_pda", "plan_swap"} <= tools


def test_derive_pda_gives_the_real_current_pool() -> None:
    s = build_meteora_surface()
    out = s.call_tool(
        "derive_pda",
        {
            "account": "lb_pair",
            "bindings": {
                "token_x_mint": TOKEN,
                "token_y_mint": WSOL,
                "bin_step": BIN_STEP,
                "base_factor": BASE_FACTOR,
            },
        },
    )
    assert out["address"] == CURRENT_POOL


def test_derive_pda_bin_array_negative_index_gives_real_account() -> None:
    # The new recipe: ["bin_array", lb_pair, index(i64 LE)] with a SIGNED index —
    # index −1 derives the account verified live on mainnet.
    s = build_meteora_surface()
    out = s.call_tool(
        "derive_pda",
        {"account": "bin_array", "bindings": {"lb_pair": CURRENT_POOL, "index": -1}},
    )
    assert out["address"] == BIN_ARRAY_M1


def test_base_factor_is_load_bearing_old_recipe_derived_wrong_pool() -> None:
    # The differential guard: the deprecated 3-seed scheme derives a DIFFERENT address for
    # this current pool (the silent wrong-pool bug), while the shipped 4-seed scheme lands
    # it. If someone ever drops base_factor again, this test fails instead of the bug
    # shipping silently.
    from gecko.pda import PdaNode, derive_pda
    from gecko.provider_config import node_from_spec

    seeds = [
        {"kind": "ordered_pair", "left": "x", "right": "y", "select": "min"},
        {"kind": "ordered_pair", "left": "x", "right": "y", "select": "max"},
        {
            "kind": "variable",
            "name": "bin_step",
            "source": "argument",
            "encoding": "le",
            "width": 2,
        },
    ]
    bindings = {"x": TOKEN, "y": WSOL, "bin_step": BIN_STEP, "base_factor": BASE_FACTOR}
    old_node: PdaNode = node_from_spec(
        "lb_pair_legacy", {"program_id": METEORA_PROGRAM_ID, "seeds": seeds}
    )
    old_addr = derive_pda(old_node, bindings).address
    assert old_addr != CURRENT_POOL  # the bug: old recipe → wrong pool

    new_node = node_from_spec(
        "lb_pair",
        {
            "program_id": METEORA_PROGRAM_ID,
            "seeds": [
                *seeds,
                {
                    "kind": "variable",
                    "name": "base_factor",
                    "source": "argument",
                    "encoding": "le",
                    "width": 2,
                },
            ],
        },
    )
    assert derive_pda(new_node, bindings).address == CURRENT_POOL


def test_legacy_pool_needs_the_deprecated_3seed_scheme() -> None:
    # Honesty: the pre-upgrade SOL/USDC pool used the 3-seed `derive_lb_pair_pda`
    # (require_base_factor_seed == 0) and therefore CANNOT be reproduced by the shipped
    # 4-seed recipe with any base_factor — it is a legacy artifact, not a current pool.
    from gecko.pda import derive_pda
    from gecko.provider_config import node_from_spec

    three_seed = node_from_spec(
        "lb_pair_legacy",
        {
            "program_id": METEORA_PROGRAM_ID,
            "seeds": [
                {"kind": "ordered_pair", "left": "x", "right": "y", "select": "min"},
                {"kind": "ordered_pair", "left": "x", "right": "y", "select": "max"},
                {
                    "kind": "variable",
                    "name": "bin_step",
                    "source": "argument",
                    "encoding": "le",
                    "width": 2,
                },
            ],
        },
    )
    addr = derive_pda(three_seed, {"x": WSOL, "y": USDC, "bin_step": 4}).address
    assert addr == LEGACY_SOL_USDC  # the deprecated scheme still derives its own pool


# --- plan_swap: the full DECLARED plan (offline, injected state read) ---------


def test_plan_swap_derives_the_full_named_account_set() -> None:
    plan = plan_swap(_bindings(), rpc_call=_fake_rpc())
    assert plan["instruction"] == "swap"
    accounts = plan["accounts"]
    # the complete named set the swap instruction takes (Orquestra-verified), minus
    # the two OPTIONAL accounts, which are declared as honestly omitted
    assert set(accounts) == {
        "lb_pair",
        "reserve_x",
        "reserve_y",
        "user_token_in",
        "user_token_out",
        "token_x_mint",
        "token_y_mint",
        "oracle",
        "user",
        "token_x_program",
        "token_y_program",
        "event_authority",
        "program",
    }
    assert plan["optional_accounts_omitted"] == [
        "bin_array_bitmap_extension",
        "host_fee_in",
    ]
    assert accounts["lb_pair"] == CURRENT_POOL  # the root Orquestra can't derive
    assert accounts["token_x_mint"] == TOKEN and accounts["token_y_mint"] == WSOL
    assert accounts["user"] == USER
    assert accounts["program"] == METEORA_PROGRAM_ID
    assert plan["build_url"] == (
        "https://api.orquestra.dev/api/v48gsz901w84zriqe0elsl/instructions/swap/build"
    )


def test_plan_swap_selects_bin_arrays_from_the_live_bitmap() -> None:
    # Buying TOKEN with SOL walks UP from active array −2. Array −2 is MISSING on the
    # real chain (and unflagged in the canned bitmap): the walk must skip to −1/0/1 —
    # the pinned, live-verified accounts — not fabricate the dead −2.
    plan = plan_swap(_bindings(), rpc_call=_fake_rpc())
    assert [e["index"] for e in plan["remaining_accounts"]] == [-1, 0, 1]
    assert [e["pubkey"] for e in plan["remaining_accounts"]] == [
        BIN_ARRAY_M1,
        BIN_ARRAY_0,
        BIN_ARRAY_P1,
    ]
    assert all(
        e["isWritable"] and not e["isSigner"] for e in plan["remaining_accounts"]
    )


def test_plan_swap_quotes_min_amount_out_from_state() -> None:
    plan = plan_swap(_bindings(), rpc_call=_fake_rpc())
    quote = plan["quote"]
    assert quote["active_id"] == ACTIVE_ID and quote["bin_step"] == BIN_STEP
    # snapshot semantics are stamped on the plan, not hidden
    assert "SNAPSHOT" in quote["note"]
    # the guard is the quoted snapshot minus the default 5% slippage, and it is the
    # arg /build receives — never a caller guess
    assert plan["args"]["amount_in"] == 1_000_000
    assert plan["args"]["min_amount_out"] == quote["min_amount_out"]
    assert 0 < quote["min_amount_out"] < quote["expected_out_snapshot"]


def test_plan_swap_declares_the_ordered_landing_plan_with_wsol_legs() -> None:
    # input = native SOL → the declared order must carry the wrap before the swap and
    # the unwrap (CloseAccount) after it.
    plan = plan_swap(_bindings(), rpc_call=_fake_rpc())
    kinds = [step["kind"] for step in plan["landing_plan"]]
    assert kinds == [
        "compute_budget",
        "compute_budget",
        "create_idempotent_ata",
        "create_idempotent_ata",
        "wrap_sol",
        "swap",
        "close_wsol_ata",
    ]
    swap_step = next(s for s in plan["landing_plan"] if s["kind"] == "swap")
    assert [r["pubkey"] for r in swap_step["remaining_accounts"]] == [
        BIN_ARRAY_M1,
        BIN_ARRAY_0,
        BIN_ARRAY_P1,
    ]
    close_step = plan["landing_plan"][-1]
    assert close_step["accounts"]["account"] == plan["accounts"]["user_token_in"]
    assert close_step["accounts"]["destination"] == USER


def test_plan_swap_without_sol_leg_has_no_wrap_or_close() -> None:
    # TOKEN as input and WSOL as output: the output leg is SOL, so close appears but
    # wrap does not (nothing to fund before the swap). Selling X walks DOWN, so this
    # fixture needs liquidity at/below the active array too.
    plan = plan_swap(
        {**_bindings(), "input_mint": TOKEN, "output_mint": WSOL},
        rpc_call=_fake_rpc(_lb_pair_blob(liquidity_indexes=(-4, -3, -2, -1, 0, 1))),
    )
    kinds = [step["kind"] for step in plan["landing_plan"]]
    assert "wrap_sol" not in kinds
    assert kinds[-1] == "close_wsol_ata"  # the received wSOL is unwrapped
    close_step = plan["landing_plan"][-1]
    assert close_step["accounts"]["account"] == plan["accounts"]["user_token_out"]


def test_plan_swap_drained_pool_refuses() -> None:
    from gecko.meteora_math import MeteoraMathError

    with pytest.raises(MeteoraMathError):
        plan_swap(_bindings(), rpc_call=_fake_rpc(_lb_pair_blob(liquidity_indexes=())))


def test_plan_swap_missing_bindings_raise() -> None:
    with pytest.raises(ValueError, match="base_factor"):
        plan_swap(
            {k: v for k, v in _bindings().items() if k != "base_factor"},
            rpc_call=_fake_rpc(),
        )


# --- the MCP surface routes the intent to the same plan -----------------------


def _patch_state_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the intent's control-plane reads to the canned pool state (no network)."""
    from gecko.meteora_math import decode_lb_pair_state

    state = decode_lb_pair_state(_lb_pair_blob())
    monkeypatch.setattr(meteora_module, "read_lb_pair_state", lambda addr, **kw: state)
    monkeypatch.setattr(
        meteora_module, "read_account_owner", lambda addr, **kw: TOKEN_PROGRAM
    )


def test_plan_swap_intent_missing_input_is_reported() -> None:
    # base_factor is a required input (the fee tier); omitting it must be an honest
    # error, not a silently-wrong pool.
    plan = build_meteora_surface().call_tool(
        "plan_swap",
        {"input_mint": WSOL, "output_mint": TOKEN, "bin_step": str(BIN_STEP)},
    )
    assert "error" in plan and "base_factor" in str(plan)


def test_plan_swap_intent_derives_and_points_at_orquestra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_state_reads(monkeypatch)
    s = build_meteora_surface()
    plan = s.call_tool(
        "plan_swap",
        {
            "input_mint": WSOL,
            "output_mint": TOKEN,
            "bin_step": str(BIN_STEP),
            "base_factor": str(BASE_FACTOR),
            "user": USER,
            "amount_in": "1000000",
        },
    )
    assert plan["instruction"] == "swap"
    assert plan["derived"]["lb_pair"] == CURRENT_POOL
    assert plan["derived"]["reserve_x"] and plan["derived"]["oracle"]
    # the plan points at Orquestra's builder — we don't proxy it
    ex = plan["execute"]
    assert ex["method"] == "POST"
    assert ex["url"] == (
        "https://api.orquestra.dev/api/v48gsz901w84zriqe0elsl/instructions/swap/build"
    )


def test_surface_identity_comes_from_config() -> None:
    # the config-driven backbone: identity + project base come from packaged config,
    # not hardcoded literals in meteora.py — proven end to end by the real derivation.
    surface = build_meteora_surface()
    assert surface.program_id == METEORA_PROGRAM_ID
    assert (
        surface.project_base_url
        == "https://api.orquestra.dev/api/v48gsz901w84zriqe0elsl"
    )
    assert (
        surface.derive(
            "lb_pair",
            {
                "token_x_mint": TOKEN,
                "token_y_mint": WSOL,
                "bin_step": BIN_STEP,
                "base_factor": BASE_FACTOR,
            },
        )
        == CURRENT_POOL
    )


def test_get_program_graph_summarizes_pdas_and_intents() -> None:
    g = build_meteora_surface().call_tool("get_program_graph", {})
    assert g["program_id"] == METEORA_PROGRAM_ID
    assert "lb_pair" in g["pdas"]
    assert g["pdas"]["lb_pair"]["needs"] == [
        "token_x_mint",
        "token_y_mint",
        "bin_step",
        "base_factor",
    ]
    assert g["pdas"]["bin_array"]["needs"] == ["lb_pair", "index"]
    assert "bin_array_bitmap_extension" in g["pdas"]
    assert "plan_swap" in g["intents"]


@pytest.mark.skipif(
    os.environ.get("GECKO_SURFPOOL_E2E") != "1",
    reason="on-chain gate: set GECKO_SURFPOOL_E2E=1 with a surfpool mainnet fork",
)
def test_current_pool_holds_a_real_account_on_fork() -> None:
    # Strongest gate ($0, no key): the 4-seed recipe derives an address that actually
    # holds the live Meteora account on a mainnet fork. Reads owner/existence only —
    # never the payload (control-plane invariant #1).
    from gecko.pda_testkit import verify_derivation

    node = build_meteora_surface().pdas["lb_pair"]
    rpc_url = os.environ.get("GECKO_SURFPOOL_RPC", "http://127.0.0.1:8899")
    check = verify_derivation(
        node,
        {
            "token_x_mint": TOKEN,
            "token_y_mint": WSOL,
            "bin_step": BIN_STEP,
            "base_factor": BASE_FACTOR,
        },
        rpc_url=rpc_url,
    )
    assert check.address == CURRENT_POOL
    assert check.exists and check.owner_matches


# --- served over the wire (in-process ASGI, so Berkay's `claude mcp add` works) ---

mcp = pytest.importorskip("mcp")

import anyio  # noqa: E402
import httpx  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from gecko.http_server import build_http_app  # noqa: E402

BASE = "http://meteora.test"


async def _connect(app: Any, fn: Any) -> Any:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as http_client:
            async with streamable_http_client(
                f"{BASE}/mcp", http_client=http_client
            ) as (
                read,
                write,
                _sid,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(session)


def test_plan_swap_over_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_state_reads(monkeypatch)
    app = build_http_app(
        build_meteora_surface(), server_name="meteora", allowed_hosts=["meteora.test"]
    )

    async def body(session: ClientSession) -> str:
        res = await session.call_tool(
            "plan_swap",
            {
                "input_mint": WSOL,
                "output_mint": TOKEN,
                "bin_step": str(BIN_STEP),
                "base_factor": str(BASE_FACTOR),
                "user": USER,
                "amount_in": "1000000",
            },
        )
        return res.content[0].text  # type: ignore[union-attr]

    plan = json.loads(anyio.run(_connect, app, body))
    assert plan["derived"]["lb_pair"] == CURRENT_POOL
