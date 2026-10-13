"""Settlement, offline: Gecko's settlement wallet swaps a landed SOL charge and pays the
treasury. Option B (founder ruling, 9 Oct).

Every RPC, the settlement wallet's signer (a throwaway key standing in for Privy) and the
Helius POST are injected fakes. The bytes are REAL (solders), the node's answers are
mainnet-SHAPED and synthesized: this proves what the settlement does GIVEN such answers,
not that mainnet gives them. The live check is founder-run,
never part of this suite.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

pytest.importorskip("solders")

from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from gecko.anyquery.billing import BillingError  # noqa: E402
from gecko.anyquery.config import AnyQueryConfig  # noqa: E402
from gecko.anyquery.payment_tx import usdc_ata  # noqa: E402
from gecko.anyquery.settlement import MAX_ATTEMPTS, Settlement  # noqa: E402
from gecko.anyquery.settlement_policy import (  # noqa: E402
    SettlementPolicyGate,
    check_settlement_policy,
    privy_settlement_body,
    privy_settlement_rules,
    settlement_policy_from_config,
)
from gecko.anyquery.settlement_tx import (  # noqa: E402
    SETTLEMENT_FEE_LAMPORTS,
    build_settlement,
    wsol_ata,
)
from gecko.anyquery.store import DuplicateChargeError, MemoryStore  # noqa: E402
from gecko.anyquery.types import LedgerEntry  # noqa: E402
from gecko.autonomous_purchase import (  # noqa: E402
    COMPUTE_BUDGET_PROGRAM,
    USDC_MINT,
    WHIRLPOOL_PROGRAM,
)
from gecko.credentials import ChainResolver, KeyringBackend  # noqa: E402
from gecko.landing import (  # noqa: E402
    NATIVE_SOL_MINT,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    assemble_unsigned_tx,
    close_account_ix,
    create_idempotent_ata_ix,
)
from gecko.send_endpoint import HeliusSend  # noqa: E402
from gecko.signer import SigningAttestation  # noqa: E402
from gecko.txbind import decode_message  # noqa: E402
from tests.test_anyquery_solana_billing import (  # noqa: E402
    AMOUNT,
    BLOCKHASH,
    CONFIG,
    CUSTOMER,
    FAKE_KEY,
    FEE_WALLET,
    PAYER,
    POOL,
    RELAY,
    SETTLEMENT,
    SLOT,
    SOL_IN,
    STRANGER,
    SWAP_V2_IDL,
    USDC_OUT,
    _FakeKeyring,
    _key,
    _row,
    _swap_values,
    fixed_planner,
)

SETTLEMENT_KEY = _key(40)
assert str(SETTLEMENT_KEY.pubkey()) == SETTLEMENT
LAMPORTS_IN = SOL_IN - SETTLEMENT_FEE_LAMPORTS
FLOOR = AMOUNT  # fixed_planner's floor
SETTLE_SIG = "8" + "T" * 86
POLICY = settlement_policy_from_config(CONFIG)
#: The tests written before the float (founder, 9 Oct) forward everything above zero.
NO_FLOAT = replace(CONFIG, settlement_float_usdc_micro=0)


@dataclass
class SettleChain:
    """A replaying node for the settlement wallet. Mainnet-shaped, synthesized."""

    held_usdc: int = 0
    wsol_exists: bool = True
    usdc_exists: bool = True
    sim_err: Any = None
    landed_err: Any = None
    visible: bool = True
    #: What the treasury's USDC account gained in the landed settlement; None = exact.
    treasury_received: int | None = None
    settlement_lamports: int = 3_000_000 + SOL_IN
    calls: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    simulations: list[list[Any]] = field(default_factory=list)

    def _transfer(self, tx_b64: str) -> int:
        tx = Transaction.from_bytes(base64.b64decode(tx_b64))
        keys = [str(k) for k in tx.message.account_keys]
        for ix in tx.message.instructions:
            data = bytes(ix.data)
            if keys[ix.program_id_index] == TOKEN_PROGRAM_ID and data[:1] == b"\x0c":
                return struct.unpack_from("<Q", data, 1)[0]
        return 0

    def _rows(self, forwarded: int, received: int) -> tuple[list, list]:
        pre = [
            _row(1, SETTLEMENT, USDC_MINT, self.held_usdc),
            _row(2, FEE_WALLET, USDC_MINT, 0),
            _row(5, POOL, NATIVE_SOL_MINT, 10**12, 9),
            _row(6, POOL, USDC_MINT, 10**11),
        ]
        post = [
            _row(1, SETTLEMENT, USDC_MINT, self.held_usdc + USDC_OUT - forwarded),
            _row(2, FEE_WALLET, USDC_MINT, received),
            _row(5, POOL, NATIVE_SOL_MINT, 10**12 + LAMPORTS_IN, 9),
            _row(6, POOL, USDC_MINT, 10**11 - USDC_OUT),
        ]
        return pre, post

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == "getLatestBlockhash":
            return {
                "result": {
                    "value": {"blockhash": BLOCKHASH, "lastValidBlockHeight": 655}
                }
            }
        if method == "getRecentPrioritizationFees":
            return {"result": []}
        if method == "getAccountInfo":
            target = params[0]
            if target == wsol_ata(SETTLEMENT) and not self.wsol_exists:
                return {"result": {"value": None}}
            if target == usdc_ata(SETTLEMENT) and not self.usdc_exists:
                return {"result": {"value": None}}
            lamports = self.settlement_lamports if target == SETTLEMENT else 2_039_280
            return {"result": {"value": {"lamports": lamports, "data": ["", "base64"]}}}
        if method == "getTokenAccountBalance":
            assert params[0] == usdc_ata(SETTLEMENT)
            return {"result": {"value": {"amount": str(self.held_usdc)}}}
        if method == "simulateTransaction":
            self.simulations.append(params)
            forwarded = self._transfer(params[0])
            pre, post = self._rows(forwarded, forwarded)
            value = {
                "err": self.sim_err,
                "logs": ["Program log: gecko settlement"],
                "unitsConsumed": 60_000,
                "accounts": [
                    {
                        "lamports": self.settlement_lamports - SOL_IN,
                        "data": ["", "base64"],
                    }
                ],
                "preTokenBalances": pre,
                "postTokenBalances": post,
            }
            return {"result": {"context": {"slot": SLOT}, "value": value}}
        if method == "getSlot":
            return {"result": SLOT}
        if method == "sendTransaction":
            self.sent.append(params[0])
            return {
                "result": SETTLE_SIG
                if len(self.sent) == 1
                else f"9{len(self.sent)}" + "U" * 85
            }
        if method == "getSignatureStatuses":
            return {
                "result": {
                    "value": [
                        {
                            "confirmationStatus": "confirmed",
                            "err": None,
                            "slot": SLOT + 3,
                        }
                    ]
                }
            }
        if method == "getTransaction":
            if not self.visible:
                return {"result": None}
            forwarded = self._transfer(self.sent[-1])
            received = (
                forwarded if self.treasury_received is None else self.treasury_received
            )
            pre, post = self._rows(forwarded, received)
            return {
                "result": {
                    "slot": SLOT + 3,
                    "meta": {
                        "err": self.landed_err,
                        "preTokenBalances": pre,
                        "postTokenBalances": post,
                    },
                }
            }
        raise AssertionError(f"unscripted RPC method {method}")


@dataclass
class SettlementSigner:
    """Signs with the throwaway key whose pubkey IS the settlement wallet."""

    asked: int = 0
    attestations: list[SigningAttestation] = field(default_factory=list)

    @property
    def pubkey(self) -> str:
        return SETTLEMENT

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        self.asked += 1
        self.attestations.append(attestation)
        tx = Transaction.from_bytes(unsigned_transaction)
        signature = SETTLEMENT_KEY.sign_message(bytes(tx.message))
        return bytes(Transaction.populate(tx.message, [signature]))


@dataclass
class HeliusPost:
    chain: SettleChain
    urls: list[str] = field(default_factory=list)

    def __call__(self, url: str, body: bytes) -> dict[str, Any]:
        self.urls.append(url)
        request = json.loads(body)
        return self.chain("helius", request["method"], request["params"])


@dataclass
class Rig:
    settlement: Settlement
    store: MemoryStore
    chain: SettleChain
    signer: SettlementSigner
    post: HeliusPost


def _rig(
    *, planner: Any = None, config: AnyQueryConfig = NO_FLOAT, **chain_kw: Any
) -> Rig:
    chain = SettleChain(**chain_kw)
    store = MemoryStore()
    signer = SettlementSigner()
    post = HeliusPost(chain)
    helius = HeliusSend(
        resolver=ChainResolver([KeyringBackend(module=_FakeKeyring())]),
        post=post,
        relay_fee_payer=RELAY,
    )
    counter = iter(range(1, 10_000))
    settlement = Settlement(
        config=config,
        store=store,
        rpc_url="https://rpc.example",
        network="mainnet",
        backend=lambda: signer,
        swap_planner=planner or fixed_planner(),
        rpc_call=chain,
        send=helius,
        sleep=lambda _s: None,
        clock=lambda: "2026-10-08T12:00:05Z",
        new_id=lambda: f"st{next(counter):04d}",
    )
    return Rig(settlement, store, chain, signer, post)


def _charge(**kw: Any) -> LedgerEntry:
    base = LedgerEntry(
        entry_id="charge-1",
        customer=CUSTOMER,
        kind="charge",
        run_id="20261008-120000-aaaaaa",
        quote_id="q1",
        amount_usdc_micro=AMOUNT,
        status="landed",
        pay_with="SOL",
        signature="5" + "P" * 86,
        slot=SLOT,
        send_label="public-rpc",
        rebate_requested=False,
        rebate_lamports=None,
        split_user_pct=None,
        sol_usdc_rate_micro=150_000_000,
        created_at="2026-10-08T12:00:00Z",
        note="",
        amount_lamports=SOL_IN,
    )
    return replace(base, **kw)


def _settled(rig: Rig, charge: LedgerEntry | None = None) -> LedgerEntry:
    charge = charge or _charge()
    rig.store.append_ledger(charge)
    return rig.settlement.settle(charge)


def _programs(tx_b64: str) -> list[tuple[str, bytes]]:
    decoded = decode_message(tx_b64)
    return [(ix.program_id, ix.discriminator) for ix in decoded.instructions]


# --------------------------------------------------------------------------------------
# The settlement: one transaction from the settlement wallet, through Helius
# --------------------------------------------------------------------------------------


def test_a_landed_sol_charge_is_settled_in_one_transaction_through_helius() -> None:
    rig = _rig(held_usdc=300)

    entry = _settled(rig)

    assert entry.kind == "settlement" and entry.status == "landed"
    assert entry.run_id == "20261008-120000-aaaaaa" and entry.signature == SETTLE_SIG
    assert entry.slot == SLOT + 3
    # The floor plus what the wallet already held (an earlier swap's surplus).
    assert entry.amount_usdc_micro == FLOOR + 300
    assert entry.amount_lamports == LAMPORTS_IN
    # The rate the settlement realised, from the pool's own movements.
    assert entry.sol_usdc_rate_micro == USDC_OUT * 1_000_000_000 // LAMPORTS_IN
    assert entry.send_label == "helius-mainnet" and entry.rebate_requested is True
    assert POOL in entry.note and str(FLOOR + 300) in entry.note
    [url] = rig.post.urls
    assert parse_qs(urlsplit(url).query)["rebate-address"] == [RELAY]
    assert rig.signer.asked == 1
    [sent] = rig.chain.sent
    assert [p for p, _d in _programs(sent)] == [
        SYSTEM_PROGRAM_ID,
        TOKEN_PROGRAM_ID,
        WHIRLPOOL_PROGRAM,
        TOKEN_PROGRAM_ID,
    ]
    message = decode_message(sent)
    assert message.fee_payer == SETTLEMENT
    # Signed by the settlement wallet alone; the user's wallet is not in it at all.
    assert PAYER not in message.writable_accounts
    assert usdc_ata(FEE_WALLET) in message.writable_accounts


def test_the_wrap_swap_and_forward_amounts_are_exact() -> None:
    build = build_settlement(
        owner=SETTLEMENT,
        fee_wallet=FEE_WALLET,
        lamports_in=LAMPORTS_IN,
        held_usdc_micro=0,
        swap_planner=fixed_planner(),
        max_usdc_micro=CONFIG.max_settlement_usdc_micro,
    )
    tx = Transaction.from_bytes(base64.b64decode(build.built.tx))
    keys = [str(k) for k in tx.message.account_keys]
    wrap, sync, swap, forward = tx.message.instructions
    assert struct.unpack_from("<Q", bytes(wrap.data), 4)[0] == LAMPORTS_IN
    assert keys[wrap.accounts[1]] == wsol_ata(SETTLEMENT)
    assert bytes(sync.data) == b"\x11"
    assert struct.unpack_from("<Q", bytes(swap.data), 8)[0] == LAMPORTS_IN
    assert bytes(swap.data)[40] == 1  # exact input
    assert struct.unpack_from("<Q", bytes(forward.data), 1)[0] == FLOOR
    assert keys[forward.accounts[2]] == usdc_ata(FEE_WALLET)


def test_settling_twice_returns_the_first_settlement_and_sends_nothing() -> None:
    rig = _rig()
    first = _settled(rig)
    calls = len(rig.chain.sent)

    again = rig.settlement.settle(_charge())

    assert again == first and len(rig.chain.sent) == calls
    assert rig.signer.asked == 1


def test_a_settlement_not_yet_visible_is_reread_by_signature_never_resent() -> None:
    rig = _rig(visible=False)

    entry = _settled(rig)

    assert (entry.status, entry.signature) == ("pending", SETTLE_SIG)
    assert rig.settlement.settle(_charge()) == entry  # still not visible
    rig.chain.visible = True
    settled = rig.settlement.settle(_charge())
    assert settled.status == "landed" and settled.signature == SETTLE_SIG
    assert len(rig.chain.sent) == 1 and rig.signer.asked == 1


def test_a_failed_settlement_is_retried_and_never_charges_the_user_again() -> None:
    rig = _rig(sim_err={"InstructionError": [2, {"Custom": 6017}]})
    failed = _settled(rig)
    assert failed.status == "failed" and "receipt-failed" in failed.note
    assert rig.signer.asked == 0 and rig.chain.sent == []

    rig.chain.sim_err = None
    retried = rig.settlement.settle(_charge())

    assert retried.status == "landed" and retried.entry_id != failed.entry_id
    kinds = [(e.kind, e.status) for e in rig.store.ledger(CUSTOMER)]
    # One charge, never repeated; two settlement attempts.
    assert sorted(kinds) == [
        ("charge", "landed"),
        ("settlement", "failed"),
        ("settlement", "landed"),
    ]


def test_settlement_stops_after_the_last_attempt_and_says_so() -> None:
    rig = _rig(sim_err={"InstructionError": [2, {"Custom": 6017}]})
    _settled(rig)
    for _ in range(MAX_ATTEMPTS - 1):
        rig.settlement.settle(_charge())

    with pytest.raises(BillingError, match="reconcile by hand"):
        rig.settlement.settle(_charge())

    attempts = [e for e in rig.store.ledger(CUSTOMER) if e.kind == "settlement"]
    assert len(attempts) == MAX_ATTEMPTS and rig.chain.sent == []


def test_a_settlement_the_treasury_did_not_fully_receive_is_failed_for_a_human() -> (
    None
):
    rig = _rig(treasury_received=FLOOR - 1)
    entry = _settled(rig)
    assert entry.status == "failed" and entry.signature == SETTLE_SIG
    assert "reconcile" in entry.note


def test_only_one_settlement_may_be_open_per_run() -> None:
    store = MemoryStore()
    open_one = replace(
        _charge(), entry_id="s1", kind="settlement", status="pending", signature=None
    )
    store.append_ledger(open_one)
    with pytest.raises(DuplicateChargeError, match="already open"):
        store.append_ledger(replace(open_one, entry_id="s2"))


@pytest.mark.parametrize(
    "charge",
    [
        _charge(pay_with="USDC"),
        _charge(status="pending"),
        _charge(kind="discount"),
        _charge(amount_lamports=SETTLEMENT_FEE_LAMPORTS),
    ],
    ids=["usdc", "not-landed", "not-a-charge", "too-little-sol"],
)
def test_only_a_landed_sol_charge_is_settled(charge: LedgerEntry) -> None:
    rig = _rig()
    with pytest.raises(BillingError):
        rig.settlement.settle(charge)
    assert rig.chain.calls == [] and rig.store.ledger(CUSTOMER) == []


@pytest.mark.parametrize("missing", ["wsol_exists", "usdc_exists"])
def test_a_missing_token_account_stops_before_anything_is_written(missing: str) -> None:
    rig = _rig(**{missing: False})
    rig.store.append_ledger(_charge())
    with pytest.raises(BillingError, match="create it once"):
        rig.settlement.settle(_charge())
    assert [e.kind for e in rig.store.ledger(CUSTOMER)] == ["charge"]
    assert rig.signer.asked == 0


def test_the_helius_key_never_reaches_the_ledger() -> None:
    rig = _rig()
    _settled(rig)
    assert FAKE_KEY not in repr(rig.store.ledger(CUSTOMER)) + repr(rig.settlement)


def test_a_settlement_rehearsal_simulates_without_signing_or_sending() -> None:
    rig = _rig()

    receipt = rig.settlement.rehearse(LAMPORTS_IN)

    assert receipt.status == "pass"
    [params] = rig.chain.simulations
    assert params[1]["sigVerify"] is False and params[1]["replaceRecentBlockhash"]
    assert rig.signer.asked == 0 and rig.chain.sent == []
    assert rig.store.ledger(CUSTOMER) == []


def test_settlement_without_a_pool_or_wallet_is_not_configured() -> None:
    for broken in (
        replace(CONFIG, settlement_pool=None),
        replace(CONFIG, settlement_wallet=None),
    ):
        with pytest.raises(BillingError, match="not configured"):
            replace(_rig().settlement, config=broken)


# --------------------------------------------------------------------------------------
# Our own pre-sign check: what Privy's program-wide Whirlpool rule cannot pin
# --------------------------------------------------------------------------------------


def _planner_with(**overrides: Any) -> Any:
    from gecko.anyquery.settlement_tx import leg_from_values

    def plan(owner: str, lamports_in: int) -> Any:
        values = {**_swap_values(owner, lamports_in, FLOOR), **overrides}
        return leg_from_values(SWAP_V2_IDL, values, owner=owner, pool=POOL)

    return plan


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "whirlpool": STRANGER
        },  # another pool, perhaps one the app secret's holder owns
        {"token_owner_account_b": usdc_ata(STRANGER)},  # USDC out to someone else
        {"token_owner_account_a": usdc_ata(SETTLEMENT)},  # not the wallet's wSOL in
        {"a_to_b": False},  # sells USDC instead of SOL
        {"amount_specified_is_input": False},  # exact output
        {"token_mint_b": NATIVE_SOL_MINT},  # not a SOL/USDC pool
    ],
    ids=[
        "other-pool",
        "output-elsewhere",
        "input-not-own-wsol",
        "sells-usdc",
        "exact-output",
        "not-sol-usdc",
    ],
)
def test_a_swap_the_privy_rule_would_allow_is_refused_before_signing(
    overrides: dict[str, Any],
) -> None:
    rig = _rig(planner=_planner_with(**overrides))
    entry = _settled(rig)
    assert entry.status == "failed"
    assert "refused before signing" in entry.note and "-not-allowlisted]" in entry.note
    assert rig.signer.asked == 0 and rig.chain.sent == []


def test_a_settlement_that_pays_anyone_but_the_treasury_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gecko.anyquery.settlement as settlement_module

    def hijacked(**kwargs: Any) -> Any:
        return build_settlement(**{**kwargs, "fee_wallet": STRANGER})

    monkeypatch.setattr(settlement_module, "build_settlement", hijacked)
    rig = _rig()
    entry = _settled(rig)
    assert entry.status == "failed" and "destination-not-allowlisted" in entry.note
    assert rig.signer.asked == 0


def _tx(*instructions: Any) -> str:
    return str(assemble_unsigned_tx(list(instructions), SETTLEMENT).tx)


def _transfer(to: str, lamports: int) -> Instruction:
    return Instruction(
        Pubkey.from_string(SYSTEM_PROGRAM_ID),
        struct.pack("<IQ", 2, lamports),
        [
            AccountMeta(Pubkey.from_string(SETTLEMENT), True, True),
            AccountMeta(Pubkey.from_string(to), False, True),
        ],
    )


@pytest.mark.parametrize(
    ("instruction", "code"),
    [
        (lambda: _transfer(STRANGER, 1), "destination-not-allowlisted"),
        (
            lambda: _transfer(wsol_ata(SETTLEMENT), CONFIG.max_run_sol_lamports + 1),
            "over-per-transaction-cap",
        ),
        (
            lambda: close_account_ix(wsol_ata(SETTLEMENT), STRANGER, SETTLEMENT),
            "instruction-not-allowlisted",
        ),
        (
            lambda: create_idempotent_ata_ix(
                SETTLEMENT,
                SETTLEMENT,
                USDC_MINT,
                usdc_ata(SETTLEMENT),
                TOKEN_PROGRAM_ID,
            ),
            "program-not-allowlisted",
        ),
        (
            lambda: Instruction(
                Pubkey.from_string(COMPUTE_BUDGET_PROGRAM),
                bytes([3]) + struct.pack("<Q", 10**12),
                [],
            ),
            "program-not-allowlisted",
        ),
        (
            lambda: Instruction(
                Pubkey.from_string(WHIRLPOOL_PROGRAM),
                bytes(8),
                [AccountMeta(Pubkey.from_string(SETTLEMENT), True, True)],
            ),
            "instruction-not-allowlisted",
        ),
    ],
    ids=[
        "sol-to-stranger",
        "wrap-over-ceiling",
        "close-to-stranger",
        "ata-create",
        "priority-fee",
        "other-whirlpool-ix",
    ],
)
def test_the_local_check_signs_nothing_but_the_settlement(
    instruction: Any, code: str
) -> None:
    verdict = check_settlement_policy(_tx(instruction()), POLICY)
    assert verdict is not None and verdict.code == code


def test_a_gate_with_no_settlement_policy_refuses() -> None:
    from gecko.simulate import Receipt

    receipt = Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=1,
        sol_delta=-5_000,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated",
    )
    verdict = SettlementPolicyGate().authorize(_tx(_transfer(STRANGER, 1)), receipt)
    assert not verdict.authorized and verdict.code == "no-policy"


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"lamports_in": 0}, "no SOL"),
        ({"owner": FEE_WALLET}, "treasury"),
        ({"swap_planner": fixed_planner(min_out=0)}, "no output floor"),
        ({"max_usdc_micro": FLOOR - 1}, "ceiling"),
    ],
)
def test_build_settlement_refuses(kw: dict[str, Any], match: str) -> None:
    args: dict[str, Any] = {
        "owner": SETTLEMENT,
        "fee_wallet": FEE_WALLET,
        "lamports_in": LAMPORTS_IN,
        "held_usdc_micro": 0,
        "swap_planner": fixed_planner(),
        "max_usdc_micro": CONFIG.max_settlement_usdc_micro,
    }
    with pytest.raises(BillingError, match=match):
        build_settlement(**{**args, **kw})


def test_the_forward_is_capped_but_never_below_the_floor() -> None:
    build = build_settlement(
        owner=SETTLEMENT,
        fee_wallet=FEE_WALLET,
        lamports_in=LAMPORTS_IN,
        held_usdc_micro=10**9,
        swap_planner=fixed_planner(),
        max_usdc_micro=FLOOR + 5,
    )
    assert build.transfer_usdc_micro == FLOOR + 5


# --------------------------------------------------------------------------------------
# The float: the settlement wallet keeps USDC to pay Orthogonal over x402 (founder, 9 Oct)
# --------------------------------------------------------------------------------------


def _float_build(held: int, float_micro: int, cap: int = 10**9) -> Any:
    return build_settlement(
        owner=SETTLEMENT,
        fee_wallet=FEE_WALLET,
        lamports_in=LAMPORTS_IN,
        held_usdc_micro=held,
        swap_planner=fixed_planner(),
        max_usdc_micro=cap,
        float_usdc_micro=float_micro,
    )


def test_only_what_is_above_the_float_goes_to_the_treasury() -> None:
    build = _float_build(held=1_000_000, float_micro=1_000_000)
    # It held exactly the float; the swap's floor is all that is above it.
    assert build.transfer_usdc_micro == FLOOR
    build = _float_build(held=1_500_000, float_micro=1_000_000)
    assert build.transfer_usdc_micro == FLOOR + 500_000


def test_below_the_float_nothing_is_forwarded_and_the_transfer_is_left_out() -> None:
    build = _float_build(held=300, float_micro=1_000_000)
    assert build.transfer_usdc_micro == 0
    programs = [p for p, _d in _programs(build.built.tx)]
    # Wrap, sync, swap: no TransferChecked at all, so no treasury account in the bytes.
    assert programs == [SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID, WHIRLPOOL_PROGRAM]
    assert usdc_ata(FEE_WALLET) not in decode_message(build.built.tx).writable_accounts
    assert usdc_ata(FEE_WALLET) not in build.policy.allowed_destinations


def test_the_float_is_reckoned_on_the_floor_so_a_worst_swap_still_keeps_it() -> None:
    float_micro = 1_000_000
    held = float_micro - FLOOR // 2
    build = _float_build(held=held, float_micro=float_micro)
    # Worst case the swap pays exactly its floor: the wallet then holds the float.
    assert held + FLOOR - build.transfer_usdc_micro == float_micro


def test_the_float_does_not_lift_the_settlement_ceiling() -> None:
    build = _float_build(held=10**9, float_micro=1_000_000, cap=FLOOR + 5)
    assert build.transfer_usdc_micro == FLOOR + 5


def test_a_negative_float_is_refused() -> None:
    with pytest.raises(BillingError, match="float"):
        _float_build(held=0, float_micro=-1)


def test_the_default_float_is_one_usdc_and_a_small_settlement_keeps_it_all() -> None:
    assert AnyQueryConfig().settlement_float_usdc_micro == 1_000_000
    rig = _rig(config=CONFIG, held_usdc=300)

    entry = _settled(rig)

    assert entry.status == "landed" and entry.amount_usdc_micro == 0
    assert "float" in entry.note
    [sent] = rig.chain.sent
    assert TOKEN_PROGRAM_ID in [p for p, _d in _programs(sent)]  # the sync
    assert usdc_ata(FEE_WALLET) not in decode_message(sent).writable_accounts


def test_a_settlement_above_the_float_forwards_the_excess_and_lands() -> None:
    rig = _rig(config=replace(CONFIG, settlement_float_usdc_micro=1_000), held_usdc=300)

    entry = _settled(rig)

    assert entry.status == "landed"
    assert entry.amount_usdc_micro == FLOOR + 300 - 1_000


# --------------------------------------------------------------------------------------
# The settlement wallet's Privy policy
# --------------------------------------------------------------------------------------


def _conds(rule: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    return {
        (c["field_source"], c["field"], c["operator"], str(c["value"]))
        for c in rule["conditions"]
    }


def test_the_settlement_policy_allows_the_four_steps_and_nothing_else() -> None:
    body = privy_settlement_body(POLICY, name="gecko-settlement")

    assert (body["version"], body["chain_type"]) == ("1.0", "solana")
    rules = body["rules"]
    assert [r["action"] for r in rules] == ["ALLOW"] * 4
    assert {r["method"] for r in rules} == {"signTransaction"}
    wrap, token, whirlpool, forward = rules
    assert _conds(wrap) == {
        ("solana_system_program_instruction", "instructionName", "eq", "Transfer"),
        (
            "solana_system_program_instruction",
            "Transfer.to",
            "eq",
            wsol_ata(SETTLEMENT),
        ),
        (
            "solana_system_program_instruction",
            "Transfer.lamports",
            "lte",
            str(CONFIG.max_run_sol_lamports),
        ),
    }
    # Privy's token-program vocabulary has no SyncNative (docs, 9 Oct; a live create 400'd
    # on it), so the SPL Token program is allowed program-wide, like Whirlpool, for this
    # wallet only; our own check still pins every account.
    assert _conds(token) == {
        ("solana_program_instruction", "programId", "eq", str(TOKEN_PROGRAM))
    }
    assert "SyncNative" not in json.dumps(body)
    # Program-wide, and named so: Privy cannot pin the pool; our own check does.
    assert _conds(whirlpool) == {
        ("solana_program_instruction", "programId", "eq", WHIRLPOOL_PROGRAM)
    }
    assert _conds(forward) == {
        (
            "solana_token_program_instruction",
            "instructionName",
            "eq",
            "TransferChecked",
        ),
        (
            "solana_token_program_instruction",
            "TransferChecked.destination",
            "eq",
            usdc_ata(FEE_WALLET),
        ),
        ("solana_token_program_instruction", "TransferChecked.mint", "eq", USDC_MINT),
        (
            "solana_token_program_instruction",
            "TransferChecked.amount",
            "lte",
            str(CONFIG.max_settlement_usdc_micro),
        ),
    }
    assert "DENY" not in repr(body) and "CloseAccount" not in repr(body)


def test_before_the_wallet_exists_there_is_no_wrap_rule() -> None:
    rules = privy_settlement_rules(
        treasury_usdc_account=usdc_ata(FEE_WALLET),
        max_usdc_micro=1,
        wallet=None,
        max_lamports=1,
    )
    assert len(rules) == 3
    assert "Transfer.to" not in repr(rules)


def test_every_privy_rule_name_is_under_fifty_characters() -> None:
    """Live 9 Oct: Privy answered 400 "Rule name must be fewer than 50 characters"."""
    for wallet in (None, SETTLEMENT):
        rules = privy_settlement_rules(
            treasury_usdc_account=usdc_ata(FEE_WALLET),
            max_usdc_micro=10_000_000,
            wallet=wallet,
            max_lamports=60_000_000,
        )
        for rule in rules:
            assert len(rule["name"]) < 50, rule["name"]


SCRAPE_PAY_TO = "4Bf6zpkrv4gtirAzM3kzqXrRRLXNmbKrpHLebTpAk3Wg"
FIBER_PAY_TO = "DHZJ3PpyVtaomjEiHVwzL982prM2LeENNuHiFMxJy4NC"
PINNED = {SCRAPE_PAY_TO: ("scrapecreators",), FIBER_PAY_TO: ("fiber",)}


def test_the_settlement_policy_gains_one_capped_usdc_rule_per_pinned_pay_to() -> None:
    """Founder ruling, 9 Oct: the settlement wallet pays per call over x402. Measured
    the same day, Orthogonal names a different payTo per provider, so the update the
    founder runs on testpolicy00000000000002 carries one rule per pinned payTo."""
    from dataclasses import replace as _replace

    from gecko.anyquery.privy_admin import settlement_rules
    from gecko.x402_svm import COMPUTE_BUDGET_PROGRAM, MEMO_PROGRAM

    rules = settlement_rules(CONFIG, wallet=SETTLEMENT, x402_pay_tos=PINNED)

    assert len(rules) == 8
    by_name = {r["name"]: r for r in rules}
    fiber = by_name["USDC to fiber (DHZJ), capped"]
    assert _conds(fiber) == {
        (
            "solana_token_program_instruction",
            "instructionName",
            "eq",
            "TransferChecked",
        ),
        (
            "solana_token_program_instruction",
            "TransferChecked.destination",
            "eq",
            usdc_ata(FIBER_PAY_TO),
        ),
        ("solana_token_program_instruction", "TransferChecked.mint", "eq", USDC_MINT),
        (
            "solana_token_program_instruction",
            "TransferChecked.amount",
            "lte",
            str(CONFIG.max_x402_call_usdc_micro),
        ),
    }
    assert "USDC to scrapecreators (4Bf6), capped" in by_name
    programs = {
        c["value"] for r in rules for c in r["conditions"] if c["field"] == "programId"
    }
    assert {COMPUTE_BUDGET_PROGRAM, MEMO_PROGRAM} <= programs
    assert all(len(r["name"]) < 50 for r in rules)
    # Without a pinned payTo there is no x402 rule at all: nowhere to pay.
    assert len(settlement_rules(CONFIG, wallet=SETTLEMENT)) == 4
    # The env override replaces the registry's set with its one address.
    override = _replace(CONFIG, orthogonal_x402_pay_to=STRANGER)
    only = settlement_rules(override, wallet=None, x402_pay_tos=PINNED)
    destinations = {
        c["value"]
        for r in only
        for c in r["conditions"]
        if c["field"] == "TransferChecked.destination"
    }
    assert usdc_ata(STRANGER) in destinations
    assert usdc_ata(FIBER_PAY_TO) not in destinations
    assert len(only) == 6 and "Transfer.to" not in repr(only)


def test_a_long_provider_list_still_names_its_rule_under_fifty_characters() -> None:
    from gecko.anyquery.x402_policy import x402_rule_name

    name = x402_rule_name(("a-very-long-provider-name", "another-one"), FIBER_PAY_TO)
    assert len(name) < 50 and name.endswith("(DHZJ), capped")


def test_the_x402_cap_must_be_positive_when_the_rules_are_written() -> None:
    from gecko.anyquery.config import ConfigError

    with pytest.raises(ConfigError, match="cap"):
        privy_settlement_rules(
            treasury_usdc_account=usdc_ata(FEE_WALLET),
            max_usdc_micro=1,
            wallet=None,
            max_lamports=1,
            x402_pay_tos={STRANGER: ("x",)},
            max_x402_call_usdc_micro=0,
        )
