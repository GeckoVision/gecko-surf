"""The payment's bytes and the policy that may sign them, the SOL price and the
settlement's swap leg read from one pinned pool, plus the configured rebate address on
the Helius transport. Offline: a synthesized SOL/USDC pool, a fake keyring."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("solders")

from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from gecko.anyquery.billing import BillingError  # noqa: E402
from gecko.anyquery.payment_tx import (  # noqa: E402
    build_payment,
    whirlpool_sol_pricer,
)
from gecko.anyquery.settlement_tx import (  # noqa: E402
    build_settlement,
    whirlpool_swap_leg,
)
from gecko.autonomous_purchase import (  # noqa: E402
    SWAP_V2_DISCRIMINATOR,
    USDC_MINT,
    WHIRLPOOL_PROGRAM,
    PurchaseConfigurationError,
    PurchasePlan,
    run_purchase,
)
from gecko.credentials import ChainResolver, KeyringBackend  # noqa: E402
from gecko.landing import (  # noqa: E402
    NATIVE_SOL_MINT,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from gecko.send_endpoint import SendConfigError, helius_send  # noqa: E402
from gecko.signer import TransactionSigner  # noqa: E402
from gecko.spend_policy import SpendPolicyGate  # noqa: E402
from gecko.whirlpool_math import (  # noqa: E402
    quote_min_amount_out,
    size_input_for_output,
    spot_out,
)
from tests.test_anyquery_solana_billing import (  # noqa: E402
    FEE_WALLET,
    PAYER,
    RELAY,
    SETTLEMENT,
    SOL_IN,
    SWAP_V2_IDL,
    fixed_planner,
    fixed_pricer,
)

CONFIG = "2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ"
VAULT_A = "6j9UtMmzmWuLu45XXmdUXN3NJBdiicxxoBEex8jUs3j6"
VAULT_B = "5Sokmb48nt8aH8TnnkrAcVea4SdRqGU3qTxhRFvTHJyn"
POOL_DISC = bytes([63, 149, 209, 12, 225, 128, 99, 9])
#: 150 USDC per SOL in base units: 0.15 USDC-micro per lamport, as Q64.64 sqrt.
SQRT_PRICE = int((0.15**0.5) * 2**64)
FEE_RATE = 400
FAKE_KEY = "hk-SENTINEL-payment-DO-NOT-LEAK"


def _b58(value: str) -> bytes:
    return bytes(Pubkey.from_string(value))


def _idl() -> dict[str, Any]:
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
        "accounts": [{"name": "Whirlpool", "discriminator": list(POOL_DISC)}],
        "types": [{"name": "Whirlpool", "type": {"kind": "struct", "fields": fields}}],
        **SWAP_V2_IDL,
    }


def _pool_address() -> str:
    from gecko.pda import derive_pda
    from gecko.provider_config import load_packaged_provider

    _, apis = load_packaged_provider("orquestra")
    recipe = dict(apis["whirlpool"].program.pdas)["whirlpool"]
    return derive_pda(
        recipe,
        {
            "whirlpools_config": CONFIG,
            "token_mint_a": NATIVE_SOL_MINT,
            "token_mint_b": USDC_MINT,
            "tick_spacing": 4,
        },
    ).address


def _pool_blob() -> bytes:
    from gecko.whirlpool_venue import whirlpool_layout

    layout = whirlpool_layout(_idl())
    size = max(o + w for o, w, _ in layout.fields.values())
    buf = bytearray(size)
    buf[0:8] = POOL_DISC

    def put(name: str, value: Any, *, pub: bool = False, signed: bool = False) -> None:
        off, width, _ = layout.fields[name]
        buf[off : off + width] = (
            _b58(value) if pub else int(value).to_bytes(width, "little", signed=signed)
        )

    put("whirlpools_config", CONFIG, pub=True)
    put("token_mint_a", NATIVE_SOL_MINT, pub=True)
    put("token_mint_b", USDC_MINT, pub=True)
    put("token_vault_a", VAULT_A, pub=True)
    put("token_vault_b", VAULT_B, pub=True)
    put("tick_spacing", 4)
    put("fee_rate", FEE_RATE)
    put("liquidity", 10**15)
    put("sqrt_price", SQRT_PRICE)
    put("tick_current_index", -18_972, signed=True)
    return bytes(buf)


def _fake_rpc(pool: str, *, owner: str = WHIRLPOOL_PROGRAM) -> Any:
    blob = base64.b64encode(_pool_blob()).decode()

    def call(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getProgramAccounts":
            if params[1]["filters"][1]["memcmp"]["bytes"] == NATIVE_SOL_MINT:
                return {
                    "result": [{"pubkey": pool, "account": {"data": [blob, "base64"]}}]
                }
            return {"result": []}
        if method == "getAccountInfo":
            if params[0] == pool:
                return {"result": {"value": {"data": [blob, "base64"], "owner": owner}}}
            if params[0] in (NATIVE_SOL_MINT, USDC_MINT):
                return {
                    "result": {
                        "value": {"owner": TOKEN_PROGRAM_ID, "data": ["", "base64"]}
                    }
                }
            return {"result": {"value": None}}  # the payer's ATAs do not exist yet
        raise AssertionError(f"unscripted {method}")

    return call


def test_the_sol_price_is_the_smallest_input_that_clears_the_quote_with_the_margin() -> (
    None
):
    pool = _pool_address()
    price = whirlpool_sol_pricer(
        rpc_url="http://fake",
        rpc_call=_fake_rpc(pool),
        idl_fetch=lambda _p: _idl(),
        pool=pool,
        margin_bps=200,
    )

    sol = price(20_000)

    assert sol.pool == pool and sol.margin_bps == 200
    assert sol.lamports == size_input_for_output(
        20_000, SQRT_PRICE, FEE_RATE, a_to_b=True, slippage_bps=200
    )
    assert sol.rate_micro == spot_out(1_000_000_000, SQRT_PRICE, a_to_b=True)
    # ~150 USDC per SOL: 0.02 USDC is ~133k lamports before fee and margin.
    assert 133_000 < sol.lamports < 140_000


def test_a_pool_the_whirlpool_program_does_not_own_is_never_priced() -> None:
    pool = _pool_address()
    price = whirlpool_sol_pricer(
        rpc_url="http://fake",
        rpc_call=_fake_rpc(pool, owner=TOKEN_PROGRAM_ID),
        idl_fetch=lambda _p: _idl(),
        pool=pool,
        margin_bps=200,
    )
    with pytest.raises(BillingError, match="not a Whirlpool"):
        price(20_000)


def test_the_settlement_swap_is_exact_input_on_the_pinned_pool() -> None:
    pool = _pool_address()
    planner = whirlpool_swap_leg(
        rpc_url="http://fake",
        rpc_call=_fake_rpc(pool),
        idl_fetch=lambda _p: _idl(),
        slippage_bps=100,
        pool=pool,
    )

    leg = planner(SETTLEMENT, SOL_IN)

    assert leg.pool == pool and leg.vault_a == VAULT_A and leg.vault_b == VAULT_B
    assert leg.amount_in_lamports == SOL_IN
    _spot, floor = quote_min_amount_out(
        SOL_IN, SQRT_PRICE, FEE_RATE, a_to_b=True, slippage_bps=100
    )
    assert leg.min_out_usdc_micro == floor
    ix = leg.instruction
    assert str(ix.program_id) == WHIRLPOOL_PROGRAM
    assert bytes(ix.data[:8]) == SWAP_V2_DISCRIMINATOR
    metas = list(ix.accounts)
    assert len(metas) == 15
    assert str(metas[3].pubkey) == SETTLEMENT and metas[3].is_signer
    assert str(metas[4].pubkey) == pool
    oracle, _ = Pubkey.find_program_address(
        [b"oracle", bytes(Pubkey.from_string(pool))],
        Pubkey.from_string(WHIRLPOOL_PROGRAM),
    )
    assert metas[14].pubkey == oracle and metas[14].is_writable
    assert SETTLEMENT not in leg.writes and pool in leg.writes


def test_a_settlement_swap_needs_a_pinned_pool() -> None:
    with pytest.raises(BillingError, match="GECKO_SETTLEMENT_POOL"):
        whirlpool_swap_leg(
            rpc_url="http://fake",
            rpc_call=_fake_rpc("x"),
            idl_fetch=lambda _p: _idl(),
            slippage_bps=100,
            pool="",
        )


def _sol_payment() -> Any:
    return build_payment(
        payer=PAYER,
        fee_wallet=FEE_WALLET,
        amount_usdc_micro=20_000,
        pay_with="SOL",
        settlement_wallet=SETTLEMENT,
        sol_pricer=fixed_pricer(),
    )


def test_the_sol_payment_is_one_transfer_of_the_priced_lamports() -> None:
    build = _sol_payment()
    tx = Transaction.from_bytes(base64.b64decode(build.built.tx))
    keys = [str(k) for k in tx.message.account_keys]
    [ix] = tx.message.instructions
    assert keys[ix.program_id_index] == SYSTEM_PROGRAM_ID
    assert int.from_bytes(bytes(ix.data)[4:12], "little") == SOL_IN
    assert [keys[i] for i in bytes(ix.accounts)] == [PAYER, SETTLEMENT]
    assert tx.message.header.num_required_signatures == 1 and keys[0] == PAYER
    assert build.sol is not None and build.sol.lamports == SOL_IN
    assert build.settlement_wallet == SETTLEMENT
    # The policy allows this one transfer and no token at all.
    assert build.policy.allowed_destinations == frozenset({SETTLEMENT})
    assert build.policy.token_caps is not None and build.policy.token_caps.caps == {}
    assert build.policy.per_transaction_cap_lamports is not None
    assert build.policy.per_transaction_cap_lamports >= SOL_IN


def test_paying_yourself_or_nothing_is_refused() -> None:
    with pytest.raises(BillingError):
        build_payment(
            payer=PAYER, fee_wallet=PAYER, amount_usdc_micro=1, pay_with="USDC"
        )
    with pytest.raises(BillingError):
        build_payment(
            payer=PAYER, fee_wallet=FEE_WALLET, amount_usdc_micro=0, pay_with="USDC"
        )
    with pytest.raises(BillingError, match="settlement wallet"):
        build_payment(
            payer=PAYER, fee_wallet=FEE_WALLET, amount_usdc_micro=1, pay_with="SOL"
        )
    with pytest.raises(BillingError, match="pool price"):
        build_payment(
            payer=PAYER,
            fee_wallet=FEE_WALLET,
            amount_usdc_micro=1,
            pay_with="SOL",
            settlement_wallet=SETTLEMENT,
        )
    for wrong in (PAYER, FEE_WALLET):
        with pytest.raises(BillingError, match="neither"):
            build_payment(
                payer=PAYER,
                fee_wallet=FEE_WALLET,
                amount_usdc_micro=1,
                pay_with="SOL",
                settlement_wallet=wrong,
                sol_pricer=fixed_pricer(),
            )


# --------------------------------------------------------------------------------------
# The configured rebate address on the Helius transport
# --------------------------------------------------------------------------------------


class _Keyring:
    def get_keyring(self) -> object:
        return object()

    def get_password(self, service: str, user: str) -> str | None:
        return FAKE_KEY if service == "gecko:HELIUS_API_KEY" else None


def _helius(urls: list[str], relay: str | None) -> Any:
    def post(url: str, body: bytes) -> dict[str, Any]:
        urls.append(url)
        json.loads(body)
        return {"result": "Sig1111"}

    return helius_send(
        network="mainnet",
        resolver=ChainResolver([KeyringBackend(module=_Keyring())]),
        post=post,
        relay_fee_payer=relay,
    )


def _sol_tx() -> str:
    """A settlement: the swap is what a searcher backruns, so it carries the rebate."""
    return build_settlement(
        owner=SETTLEMENT,
        fee_wallet=FEE_WALLET,
        lamports_in=SOL_IN,
        held_usdc_micro=0,
        swap_planner=fixed_planner(),
        max_usdc_micro=10_000_000,
    ).built.tx


def test_the_configured_relay_wins_over_the_named_authority() -> None:
    urls: list[str] = []
    send = _helius(urls, RELAY)

    decision = send.rebate_for(_sol_tx(), authority=PAYER)
    send("sendTransaction", [_sol_tx(), {}], authority=PAYER)

    assert decision.requested and decision.address == RELAY
    assert parse_qs(urlsplit(urls[0]).query)["rebate-address"] == [RELAY]


@pytest.mark.parametrize("pay_with", ["USDC", "SOL"])
def test_a_payment_gets_no_rebate_address_even_when_one_is_configured(
    pay_with: str,
) -> None:
    urls: list[str] = []
    send = _helius(urls, RELAY)
    transfer = (
        _sol_payment()
        if pay_with == "SOL"
        else build_payment(
            payer=PAYER, fee_wallet=FEE_WALLET, amount_usdc_micro=1, pay_with="USDC"
        )
    ).built.tx

    send("sendTransaction", [transfer, {}])

    assert "rebate-address" not in parse_qs(urlsplit(urls[0]).query)
    assert not send.rebate_for(transfer).requested


def test_a_configured_relay_must_be_an_address() -> None:
    with pytest.raises(SendConfigError):
        _helius([], "not-an-address")


def test_without_a_configured_relay_no_rebate_is_requested() -> None:
    """Founder ruling, 8 Oct: the only rebate address is the configured relay."""
    urls: list[str] = []
    send = _helius(urls, None)
    send("sendTransaction", [_sol_tx(), {}])
    assert "rebate-address" not in parse_qs(urlsplit(urls[0]).query)


def test_run_purchase_refuses_a_mainnet_only_send_on_another_network() -> None:
    gate = SpendPolicyGate()
    with pytest.raises(PurchaseConfigurationError, match="mainnet only"):
        run_purchase(
            network="devnet",
            rpc_url="http://fake",
            plan=PurchasePlan("x", "y", {}, {}, fee_payer=PAYER),
            signer=TransactionSigner(spend_gate=gate),
            spend_gate=gate,
            build_call=lambda _r: None,  # type: ignore[arg-type,return-value]
            send=_helius([], FEE_WALLET),
        )
