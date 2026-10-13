"""The user's Privy wallet is restricted, and our own pre-sign check says the same.

Offline: Privy is an injected transport, the transaction bytes are real (solders). The
policy words were confirmed by Privy's echo of the first live policy (9 Oct); these tests
pin what is SENT, word for word. Option B (9 Oct): SOL may go only to the settlement
wallet, USDC only to the treasury; no program rule, no swap.
"""

from __future__ import annotations

import inspect
import struct
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

import pytest

pytest.importorskip("solders")

from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from gecko.anyquery.config import AnyQueryConfig, ConfigError, load_config  # noqa: E402
from gecko.anyquery.enrol import (  # noqa: E402
    EnrolmentError,
    InMemoryWalletBinder,
    WalletEnrolment,
    privy_wallet_creator,
)
from gecko.anyquery.payment_tx import build_payment, usdc_ata  # noqa: E402
from gecko.anyquery.settlement_tx import swap_v2_instruction  # noqa: E402
from gecko.anyquery.wallet_policy import (  # noqa: E402
    WalletPolicy,
    WalletPolicyGate,
    check_wallet_policy,
    privy_policy_body,
    wallet_policy_from_config,
)
from gecko.autonomous_purchase import (  # noqa: E402
    COMPUTE_BUDGET_PROGRAM,
    USDC_MINT,
    WHIRLPOOL_PROGRAM,
)
from gecko.landing import (  # noqa: E402
    NATIVE_SOL_MINT,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    assemble_unsigned_tx,
    close_account_ix,
)
from gecko.privy_backend import (  # noqa: E402
    POLICY_CREATE_PATH,
    PRIVY_API_BASE,
    UNCONFIRMED_WALLET_POLICY_IDS_FIELD,
    PrivyRequest,
)
from gecko.simulate import Receipt  # noqa: E402
from gecko.store_accounts import derive_ata  # noqa: E402
from gecko.wallet_binding import WalletBinding  # noqa: E402
from tests.test_anyquery_solana_billing import (  # noqa: E402
    AMOUNT,
    CONFIG,
    FEE_WALLET,
    PAYER,
    SETTLEMENT,
    SOL_IN,
    STRANGER,
    SWAP_V2_IDL,
    _swap_values,
    fixed_pricer,
)

SECRET = "privy-secret-SENTINEL-DO-NOT-LEAK"
ADDRESS = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
POLICY = wallet_policy_from_config(CONFIG)


def _tx(*instructions: Any) -> str:
    return str(assemble_unsigned_tx(list(instructions), PAYER).tx)


def _system_transfer(to: str, lamports: int, *, source: str = PAYER) -> Instruction:
    return Instruction(
        Pubkey.from_string(SYSTEM_PROGRAM_ID),
        struct.pack("<IQ", 2, lamports),
        [
            AccountMeta(Pubkey.from_string(source), True, True),
            AccountMeta(Pubkey.from_string(to), False, True),
        ],
    )


def _receipt() -> Receipt:
    return Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=10_000,
        sol_delta=-5_000,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
    )


# --------------------------------------------------------------------------------------
# The policy, as data, from config
# --------------------------------------------------------------------------------------


def test_the_policy_comes_from_config_and_names_the_treasury_and_both_ceilings() -> (
    None
):
    assert POLICY == WalletPolicy(
        treasury=FEE_WALLET,
        treasury_usdc_account=usdc_ata(FEE_WALLET),
        settlement=SETTLEMENT,
        max_usdc_micro=CONFIG.max_run_usdc_micro,
        max_lamports=CONFIG.max_run_sol_lamports,
    )


def test_no_treasury_means_no_policy_and_so_no_wallet() -> None:
    with pytest.raises(ConfigError):
        wallet_policy_from_config(AnyQueryConfig())


def test_the_settlement_wallet_is_never_the_cold_treasury() -> None:
    with pytest.raises(ConfigError, match="treasury"):
        wallet_policy_from_config(replace(CONFIG, settlement_wallet=FEE_WALLET))


def test_the_sol_ceiling_is_config_and_refuses_a_non_integer() -> None:
    assert load_config({}).max_run_sol_lamports > 0
    assert (
        load_config({"GECKO_ANYQUERY_MAX_RUN_LAMPORTS": "123"}).max_run_sol_lamports
        == 123
    )
    with pytest.raises(ConfigError):
        load_config({"GECKO_ANYQUERY_MAX_RUN_LAMPORTS": "lots"})


def test_the_privy_policy_allows_exactly_two_transfers_and_nothing_else() -> None:
    """Every word below was echoed back by Privy on 9 Oct; pinned verbatim."""
    body = privy_policy_body(POLICY, name="gecko-run-payments")

    assert body["chain_type"] == "solana"
    rules = body["rules"]
    assert [r["action"] for r in rules] == ["ALLOW", "ALLOW"]
    # signTransaction only: Gecko never asks Privy to send.
    assert {r["method"] for r in rules} == {"signTransaction"}
    usdc, sol = rules

    def conds(rule: Mapping[str, Any]) -> set[tuple[str, str, str, str]]:
        return {
            (c["field_source"], c["field"], c["operator"], str(c["value"]))
            for c in rule["conditions"]
        }

    assert conds(usdc) == {
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
            str(CONFIG.max_run_usdc_micro),
        ),
    }
    assert conds(sol) == {
        ("solana_system_program_instruction", "instructionName", "eq", "Transfer"),
        ("solana_system_program_instruction", "Transfer.to", "eq", SETTLEMENT),
        (
            "solana_system_program_instruction",
            "Transfer.lamports",
            "lte",
            str(CONFIG.max_run_sol_lamports),
        ),
    }
    # SOL never goes to the treasury directly any more (option B): nothing settles it.
    assert FEE_WALLET not in {c["value"] for c in sol["conditions"]}
    # No program-wide ALLOW: a rule on programId alone would let the app secret sign a
    # swap of the user's whole USDC balance into any pool, to any output account.
    assert WHIRLPOOL_PROGRAM not in repr(body)
    assert "programId" not in repr(body)
    # No DENY rule is needed and none is sent: an unmatched instruction is denied.
    assert "DENY" not in repr(body)
    assert (body["version"], body["chain_type"]) == ("1.0", "solana")


def test_without_a_settlement_wallet_the_policy_allows_no_sol_at_all() -> None:
    policy = wallet_policy_from_config(
        replace(CONFIG, settlement_wallet=None, settlement_wallet_id=None)
    )
    [usdc] = privy_policy_body(policy, name="p")["rules"]
    assert "TransferChecked" in repr(usdc)
    verdict = check_wallet_policy(
        _tx(_system_transfer(SETTLEMENT, 1)), policy, payer=PAYER
    )
    assert verdict is not None and verdict.code == "destination-not-allowlisted"


# --------------------------------------------------------------------------------------
# The create request carries the policy
# --------------------------------------------------------------------------------------


@dataclass
class Transport:
    policy_answer: dict[str, Any] = field(
        default_factory=lambda: {"id": "pol_1", "chain_type": "solana"}
    )
    seen: list[PrivyRequest] = field(default_factory=list)

    def __call__(
        self, request: PrivyRequest, headers: Mapping[str, str]
    ) -> dict[str, Any]:
        self.seen.append(request)
        if request.url.endswith(POLICY_CREATE_PATH):
            return self.policy_answer
        return {"id": "wlt_1", "address": ADDRESS, "chain_type": "solana"}


def _creator(transport: Transport) -> Any:
    return privy_wallet_creator(
        policy=POLICY,
        env={"PRIVY_APP_ID": "app", "PRIVY_APP_SECRET": SECRET},
        transport=transport,
    )


def test_the_wallet_create_request_carries_the_policy_made_just_before_it() -> None:
    transport = Transport()
    enrolment = WalletEnrolment(
        replace(CONFIG, wallet_create_enabled=True),
        InMemoryWalletBinder(),
        _creator(transport),
    )

    binding = enrolment.enrol("acct_1")

    assert binding == WalletBinding("acct_1", "wlt_1", ADDRESS)
    policy_call, wallet_call = transport.seen
    assert policy_call.method == "POST"
    assert policy_call.url == f"{PRIVY_API_BASE}{POLICY_CREATE_PATH}"
    assert policy_call.body == privy_policy_body(
        POLICY, name=policy_call.body["name"] if policy_call.body else ""
    )
    assert policy_call.privy_headers["privy-idempotency-key"]
    assert wallet_call.url == f"{PRIVY_API_BASE}/v1/wallets"
    assert wallet_call.body == {
        "chain_type": "solana",
        UNCONFIRMED_WALLET_POLICY_IDS_FIELD: ["pol_1"],
    }
    # Two different creates, two different idempotency keys.
    assert (
        policy_call.privy_headers["privy-idempotency-key"]
        != wallet_call.privy_headers["privy-idempotency-key"]
    )
    assert SECRET not in repr(transport.seen)


def test_a_policy_answer_without_an_id_never_creates_a_wallet() -> None:
    transport = Transport(policy_answer={"chain_type": "solana"})
    enrolment = WalletEnrolment(
        replace(CONFIG, wallet_create_enabled=True),
        InMemoryWalletBinder(),
        _creator(transport),
    )
    with pytest.raises(EnrolmentError):
        enrolment.enrol("acct_1")
    assert [r.url for r in transport.seen] == [f"{PRIVY_API_BASE}{POLICY_CREATE_PATH}"]


def test_the_live_creator_cannot_be_built_without_a_policy() -> None:
    parameter = inspect.signature(privy_wallet_creator).parameters["policy"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_creation_still_ships_disabled_with_a_policy_configured() -> None:
    transport = Transport()
    enrolment = WalletEnrolment(CONFIG, InMemoryWalletBinder(), _creator(transport))
    with pytest.raises(EnrolmentError):
        enrolment.enrol("acct_1")
    assert transport.seen == []


# --------------------------------------------------------------------------------------
# Our own pre-sign check: the same rules, over the exact bytes
# --------------------------------------------------------------------------------------


def test_the_usdc_payment_gecko_builds_passes() -> None:
    build = build_payment(
        payer=PAYER, fee_wallet=FEE_WALLET, amount_usdc_micro=AMOUNT, pay_with="USDC"
    )
    assert check_wallet_policy(build.built.tx, POLICY, payer=PAYER) is None


def test_the_sol_payment_gecko_builds_passes() -> None:
    build = build_payment(
        payer=PAYER,
        fee_wallet=FEE_WALLET,
        amount_usdc_micro=AMOUNT,
        pay_with="SOL",
        settlement_wallet=SETTLEMENT,
        sol_pricer=fixed_pricer(),
    )
    assert check_wallet_policy(build.built.tx, POLICY, payer=PAYER) is None


def test_a_usdc_transfer_to_any_other_address_is_refused() -> None:
    build = build_payment(
        payer=PAYER, fee_wallet=STRANGER, amount_usdc_micro=AMOUNT, pay_with="USDC"
    )
    verdict = check_wallet_policy(build.built.tx, POLICY, payer=PAYER)
    assert verdict is not None and not verdict.authorized
    assert verdict.code == "destination-not-allowlisted"


def test_a_usdc_transfer_above_the_ceiling_is_refused() -> None:
    build = build_payment(
        payer=PAYER,
        fee_wallet=FEE_WALLET,
        amount_usdc_micro=CONFIG.max_run_usdc_micro + 1,
        pay_with="USDC",
    )
    verdict = check_wallet_policy(build.built.tx, POLICY, payer=PAYER)
    assert verdict is not None and verdict.code == "over-per-transaction-cap"


@pytest.mark.parametrize(
    ("to", "lamports", "code"),
    [
        (STRANGER, 1, "destination-not-allowlisted"),
        # The old rule: SOL straight to the treasury. Nothing settles it now.
        (FEE_WALLET, 1, "destination-not-allowlisted"),
        (SETTLEMENT, CONFIG.max_run_sol_lamports + 1, "over-per-transaction-cap"),
    ],
)
def test_a_sol_transfer_elsewhere_or_above_the_ceiling_is_refused(
    to: str, lamports: int, code: str
) -> None:
    verdict = check_wallet_policy(
        _tx(_system_transfer(to, lamports)), POLICY, payer=PAYER
    )
    assert verdict is not None and verdict.code == code


def test_a_sol_transfer_to_the_settlement_wallet_within_the_ceiling_passes() -> None:
    tx = _tx(_system_transfer(SETTLEMENT, CONFIG.max_run_sol_lamports))
    assert check_wallet_policy(tx, POLICY, payer=PAYER) is None


def test_two_transfers_that_together_pass_the_ceiling_are_refused() -> None:
    half = CONFIG.max_run_sol_lamports // 2 + 1
    tx = _tx(_system_transfer(SETTLEMENT, half), _system_transfer(SETTLEMENT, half))
    verdict = check_wallet_policy(tx, POLICY, payer=PAYER)
    assert verdict is not None and verdict.code == "over-per-transaction-cap"


@pytest.mark.parametrize(
    "instruction",
    [
        # SOL out of an account that is not the payer's own.
        lambda: _system_transfer(SETTLEMENT, 1, source=STRANGER),
        lambda: close_account_ix(usdc_ata(PAYER), PAYER, PAYER),
        lambda: Instruction(
            Pubkey.from_string(COMPUTE_BUDGET_PROGRAM),
            bytes([3]) + struct.pack("<Q", 10**9),
            [],
        ),
        lambda: _swap(),
    ],
    ids=[
        "sol-from-another-account",
        "close-account",
        "compute-budget",
        "whirlpool-swap",
    ],
)
def test_nothing_but_the_two_transfers_is_signed(instruction: Any) -> None:
    verdict = check_wallet_policy(_tx(instruction()), POLICY, payer=PAYER)
    assert verdict is not None and not verdict.authorized


def test_a_token_approve_is_refused() -> None:
    approve = Instruction(
        Pubkey.from_string(TOKEN_PROGRAM_ID),
        bytes([4]) + struct.pack("<Q", 10**12),
        [
            AccountMeta(Pubkey.from_string(usdc_ata(PAYER)), False, True),
            AccountMeta(Pubkey.from_string(STRANGER), False, False),
            AccountMeta(Pubkey.from_string(PAYER), True, False),
        ],
    )
    verdict = check_wallet_policy(_tx(approve), POLICY, payer=PAYER)
    assert verdict is not None and verdict.code == "instruction-not-allowlisted"


def _swap() -> Instruction:
    return swap_v2_instruction(
        SWAP_V2_IDL, _swap_values(PAYER, SOL_IN, AMOUNT), payer=PAYER
    )


def test_a_whirlpool_swap_is_not_the_user_wallets_to_sign_any_more() -> None:
    verdict = check_wallet_policy(_tx(_swap()), POLICY, payer=PAYER)
    assert verdict is not None and verdict.code == "program-not-allowlisted"


def test_a_wsol_wrap_is_refused() -> None:
    wsol = derive_ata(PAYER, NATIVE_SOL_MINT, token_program=TOKEN_PROGRAM_ID)
    verdict = check_wallet_policy(
        _tx(_system_transfer(wsol, 1_000)), POLICY, payer=PAYER
    )
    assert verdict is not None and verdict.code == "destination-not-allowlisted"


def test_a_transfer_of_someone_elses_usdc_is_refused() -> None:
    from gecko.anyquery.payment_tx import transfer_checked_ix

    stolen = transfer_checked_ix(
        source=usdc_ata(STRANGER),
        mint=USDC_MINT,
        destination=usdc_ata(FEE_WALLET),
        owner=PAYER,
        amount=1,
        decimals=6,
    )
    verdict = check_wallet_policy(_tx(stolen), POLICY, payer=PAYER)
    assert verdict is not None and verdict.code == "instruction-not-allowlisted"


def test_any_other_program_is_refused() -> None:
    stray = Instruction(
        Pubkey.from_string(STRANGER),
        b"\x01",
        [AccountMeta(Pubkey.from_string(PAYER), True, True)],
    )
    verdict = check_wallet_policy(_tx(stray), POLICY, payer=PAYER)
    assert verdict is not None and verdict.code == "program-not-allowlisted"


def test_the_gate_refuses_on_the_wallet_policy_before_the_spend_policy() -> None:
    build = build_payment(
        payer=PAYER, fee_wallet=STRANGER, amount_usdc_micro=AMOUNT, pay_with="USDC"
    )
    gate = WalletPolicyGate(wallet_policy=POLICY, payer=PAYER)
    verdict = gate.authorize(build.built.tx, _receipt())
    assert verdict.code == "destination-not-allowlisted"


def test_a_gate_with_no_wallet_policy_refuses() -> None:
    build = build_payment(
        payer=PAYER, fee_wallet=FEE_WALLET, amount_usdc_micro=AMOUNT, pay_with="USDC"
    )
    verdict = WalletPolicyGate(payer=PAYER).authorize(build.built.tx, _receipt())
    assert not verdict.authorized
