"""The spend policy — authorization, the other half of the signing decision.

Every test here runs offline. Nothing signs, nothing broadcasts, no keypair is
constructed, and no RPC is reached: the whole point of the gate under test is that it
decides from bytes the caller already holds.
"""

from __future__ import annotations

import ast
import base64
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from gecko.simulate import Receipt
from gecko.spend_policy import (
    AdvisorySpendLedger,
    AllowedInstruction,
    FileSpendLedger,
    InMemorySpendLedger,
    SpendPolicy,
    SpendPolicyGate,
    SpendVerdict,
)
from gecko.txbind import DecodedMessage, decode_message

_REPO = Path(__file__).resolve().parent.parent
_SPEND_POLICY_SOURCE = _REPO / "gecko" / "spend_policy.py"

# One lamport shy of a tenth of a SOL — a number small enough to read in a diff.
_CAP = 100_000_000

PAYER = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")
DEST = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
OTHER = Pubkey.from_string("SysvarS1otHashes111111111111111111111111111")
PROGRAM = Pubkey.from_string("Vote111111111111111111111111111111111111111")

#: An Anchor-shaped 8-byte discriminator for the one call the human authorised.
TRANSFER_DISC = bytes([0xA3, 0x34, 0xC8, 0xE7, 0x8C, 0x03, 0x45, 0xBA])
#: A DIFFERENT instruction of the SAME program — the `setAuthority`-shaped one.
SET_AUTHORITY_DISC = bytes([0x8E, 0xE7, 0x4D, 0xE1, 0x0E, 0x0E, 0x3E, 0x1D])


def _tx(
    *,
    data: bytes = TRANSFER_DISC + b"\x10" * 8,
    program: Pubkey = PROGRAM,
    writable: tuple[Pubkey, ...] = (DEST,),
    payer: Pubkey = PAYER,
) -> str:
    """An UNSIGNED legacy transaction, base64. No key exists anywhere in this helper."""
    metas = [AccountMeta(payer, is_signer=True, is_writable=True)]
    metas += [AccountMeta(key, is_signer=False, is_writable=True) for key in writable]
    instruction = Instruction(program, data, metas)
    message = Message.new_with_blockhash([instruction], payer, Hash.default())
    return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()


def _receipt(*, sol_delta: int | None = -1_000) -> Receipt:
    return Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=10_000,
        sol_delta=sol_delta,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
    )


def _policy(**overrides: Any) -> SpendPolicy:
    base: dict[str, Any] = {
        "authorized": True,
        "per_transaction_cap_lamports": _CAP,
        "hourly_cap_lamports": 10 * _CAP,
        "daily_cap_lamports": 20 * _CAP,
        "max_transactions_per_day": 5,
        "allowed_instructions": frozenset(
            {AllowedInstruction(program_id=str(PROGRAM), discriminator=TRANSFER_DISC)}
        ),
        "allowed_destinations": frozenset({str(DEST)}),
    }
    base.update(overrides)
    return SpendPolicy(**base)


def _gate(
    policy: SpendPolicy | None = None, ledger: AdvisorySpendLedger | None = None
) -> SpendPolicyGate:
    return SpendPolicyGate(
        policy=policy if policy is not None else _policy(),
        ledger=ledger if ledger is not None else InMemorySpendLedger(),
    )


# --------------------------------------------------------------------------------------
# (b) ABSENT POLICY = REFUSE — a distinct type, not AgentPolicy's meaning changed
# --------------------------------------------------------------------------------------


def test_a_gate_with_no_policy_refuses() -> None:
    verdict = SpendPolicyGate().authorize(_tx(), _receipt(), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "no-policy"


def test_a_default_constructed_policy_is_a_refusal_not_a_no_op() -> None:
    """The deliberate inversion of ``policy.py:28-29``'s "an unset field is a no-op"."""
    verdict = _gate(SpendPolicy()).authorize(_tx(), _receipt(), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "policy-not-authorized"


def test_agent_policy_still_means_what_its_current_consumers_documented() -> None:
    """We may not change ``AgentPolicy`` under the governed-session callers that use it."""
    from gecko.policy import AgentPolicy

    plain = AgentPolicy()
    assert plain.spend_cap is None
    assert plain.recipient_allowlist == frozenset()
    assert not hasattr(plain, "authorized")


def test_an_incomplete_policy_refuses_and_names_the_missing_field() -> None:
    verdict = _gate(_policy(daily_cap_lamports=None)).authorize(
        _tx(), _receipt(), now=1_000.0
    )
    assert verdict.authorized is False
    assert verdict.code == "policy-incomplete"
    assert "daily_cap_lamports" in verdict.reason


# --------------------------------------------------------------------------------------
# (c) THE AGENT CANNOT WIDEN THE POLICY
# --------------------------------------------------------------------------------------


def test_an_agent_supplied_policy_is_rejected_not_merged() -> None:
    wider = _policy(per_transaction_cap_lamports=_CAP * 1_000)
    verdict = _gate().authorize(
        _tx(), _receipt(), now=1_000.0, agent_supplied_policy=wider
    )
    assert verdict.authorized is False
    assert verdict.code == "agent-supplied-policy"


def test_even_a_NARROWER_agent_supplied_policy_is_rejected() -> None:
    """Rejected, never merged. A policy that can be replaced can be replaced upward."""
    narrower = _policy(per_transaction_cap_lamports=1)
    verdict = _gate().authorize(
        _tx(), _receipt(), now=1_000.0, agent_supplied_policy=narrower
    )
    assert verdict.authorized is False
    assert verdict.code == "agent-supplied-policy"


# --------------------------------------------------------------------------------------
# (a) EVALUATED OVER THE DECODED MESSAGE, NEVER OVER STATED INTENT
# --------------------------------------------------------------------------------------


def test_authorize_takes_no_intent_shaped_parameter() -> None:
    import inspect

    params = set(inspect.signature(SpendPolicyGate.authorize).parameters)
    forbidden = {
        "intent",
        "stated_intent",
        "description",
        "purpose",
        "memo",
        "tool_name",
        "prompt",
        "summary",
    }
    assert not (params & forbidden), f"intent-shaped parameter on authorize: {params}"


def test_the_decision_follows_the_bytes_not_a_benign_description() -> None:
    """Same 'intent' either way; only the decoded discriminator differs."""
    good = _gate().authorize(_tx(data=TRANSFER_DISC + b"\x01"), _receipt(), now=1_000.0)
    bad = _gate().authorize(
        _tx(data=SET_AUTHORITY_DISC + b"\x01"), _receipt(), now=1_000.0
    )
    assert good.authorized is True
    assert bad.authorized is False


def test_no_rpc_may_become_an_input_to_the_gate() -> None:
    """``txbind`` already ruled on this (txbind.py:68-73). The gate inherits the rule."""
    tree = ast.parse(_SPEND_POLICY_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"urllib", "http", "requests", "httpx", "socket", "rpc"}), (
        f"the gate reached for the network: {sorted(imported)}"
    )


# --------------------------------------------------------------------------------------
# C4 — NOTHING FROM risk.py's FAIL-OPEN SIGNAL LAYER
# --------------------------------------------------------------------------------------


def test_the_spend_policy_imports_nothing_from_risk() -> None:
    source = _SPEND_POLICY_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "risk" not in (node.module or ""), f"imports risk: {node.module}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "risk" not in alias.name
    for banned in (
        "_cap_signal",
        "_recipient_signal",
        "_extract_amount",
        "_run_signal",
    ):
        assert banned not in source, f"lifted a fail-open predicate: {banned}"


def test_an_amount_that_cannot_be_resolved_refuses() -> None:
    """risk.py:622-623 returns no-reason here. On the signing path that is an ALLOW."""
    verdict = _gate().authorize(_tx(), _receipt(sol_delta=None), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "amount-unresolvable"


def test_a_predicate_that_raises_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """risk.py:723-725 crash-contains a raising predicate to no-reason. Not here."""
    import gecko.spend_policy as sp

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("predicate exploded")

    monkeypatch.setattr(sp, "_check_destinations", _boom)
    verdict = _gate().authorize(_tx(), _receipt(), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "predicate-raised"


# --------------------------------------------------------------------------------------
# C5 — AN UNDECODABLE ANYTHING REFUSES, NAMING WHICH ONE FIRED
# --------------------------------------------------------------------------------


def test_an_undecodable_transaction_refuses() -> None:
    verdict = _gate().authorize("not-base64-at-all!!", _receipt(), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "undecodable-transaction"


def test_an_instruction_with_no_discriminator_cannot_be_allowlisted_and_refuses() -> (
    None
):
    """Empty instruction data names no instruction. It is not 'the default one'."""
    verdict = _gate().authorize(_tx(data=b""), _receipt(), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "undecodable-instruction"


def test_an_instruction_shorter_than_its_allowlisted_discriminator_refuses() -> None:
    verdict = _gate().authorize(_tx(data=TRANSFER_DISC[:3]), _receipt(), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "instruction-not-allowlisted"


def test_a_message_that_loads_from_a_lookup_table_refuses() -> None:
    """No binding exists for it, and resolving it would need the RPC the gate refuses."""
    import gecko.spend_policy as sp
    from gecko.txbind import UnresolvedLookupError

    def _raise(*_args: Any, **_kwargs: Any) -> DecodedMessage:
        raise UnresolvedLookupError(
            "message loads accounts from an address lookup table"
        )

    import gecko.spend_policy as module

    monkey = pytest.MonkeyPatch()
    monkey.setattr(module, "decode_message", _raise)
    try:
        verdict = _gate().authorize(_tx(), _receipt(), now=1_000.0)
    finally:
        monkey.undo()
    assert sp is module
    assert verdict.authorized is False
    assert verdict.code == "undecodable-transaction"
    assert "lookup" in verdict.reason.lower()


def test_every_refusal_code_is_distinct_from_no_violation_found() -> None:
    """No path may return "authorized" because it could not read the input."""
    for tx, receipt in (
        ("!!!", _receipt()),
        (_tx(data=b""), _receipt()),
        (_tx(), _receipt(sol_delta=None)),
        (_tx(writable=(OTHER,)), _receipt()),
    ):
        verdict = _gate().authorize(tx, receipt, now=1_000.0)
        assert verdict.authorized is False
        assert verdict.code is not None


# --------------------------------------------------------------------------------------
# CAP 3 — PROGRAMS *AND* INSTRUCTIONS
# --------------------------------------------------------------------------------------


def test_a_program_allowlist_does_not_permit_every_instruction_of_that_program() -> (
    None
):
    """The whole reason the allowlist is (program, instruction) and not (program)."""
    verdict = _gate().authorize(
        _tx(data=SET_AUTHORITY_DISC + b"\x00" * 4), _receipt(), now=1_000.0
    )
    assert verdict.authorized is False
    assert verdict.code == "instruction-not-allowlisted"


def test_an_unallowlisted_program_refuses() -> None:
    verdict = _gate().authorize(
        _tx(program=Pubkey.from_string("Stake11111111111111111111111111111111111111")),
        _receipt(),
        now=1_000.0,
    )
    assert verdict.authorized is False
    assert verdict.code == "program-not-allowlisted"


def test_an_allowlist_entry_with_an_empty_discriminator_is_refused_at_construction() -> (
    None
):
    with pytest.raises(ValueError):
        AllowedInstruction(program_id=str(PROGRAM), discriminator=b"")


def test_an_allowlist_entry_longer_than_the_ceiling_is_refused() -> None:
    with pytest.raises(ValueError):
        AllowedInstruction(program_id=str(PROGRAM), discriminator=b"\x00" * 9)


# --------------------------------------------------------------------------------------
# CAP 4 — DESTINATIONS
# --------------------------------------------------------------------------------------


def test_a_writable_account_off_the_destination_allowlist_refuses() -> None:
    verdict = _gate().authorize(_tx(writable=(OTHER,)), _receipt(), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "destination-not-allowlisted"
    assert str(OTHER) in verdict.reason


def test_the_fee_payer_is_the_only_writable_account_exempt_from_the_allowlist() -> None:
    """It is checked as the signer's own account inside the signer, not here."""
    decoded = decode_message(_tx())
    assert decoded.fee_payer == str(PAYER)
    assert str(PAYER) in decoded.writable_accounts
    assert _gate().authorize(_tx(), _receipt(), now=1_000.0).authorized is True


def test_a_readonly_program_account_is_not_treated_as_a_destination() -> None:
    decoded = decode_message(_tx())
    assert str(PROGRAM) not in decoded.writable_accounts


# --------------------------------------------------------------------------------------
# CAP 1 — PER TRANSACTION
# --------------------------------------------------------------------------------------


def test_over_the_per_transaction_cap_refuses() -> None:
    verdict = _gate().authorize(_tx(), _receipt(sol_delta=-(_CAP + 1)), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "over-per-transaction-cap"


def test_an_inflow_costs_nothing_against_the_cap() -> None:
    verdict = _gate().authorize(_tx(), _receipt(sol_delta=+5_000), now=1_000.0)
    assert verdict.authorized is True
    assert verdict.outflow_lamports == 0


# --------------------------------------------------------------------------------------
# CAP 2 — CUMULATIVE / VELOCITY (ADVISORY, per C6)
# --------------------------------------------------------------------------------------


def test_n_transactions_each_under_the_per_tx_cap_still_hit_the_hourly_cap() -> None:
    """The `singleTxLimit` lesson: a per-tx cap of X is defeated by N transactions of X."""
    gate = _gate(_policy(hourly_cap_lamports=3 * _CAP))
    for index in range(3):
        verdict = gate.authorize(_tx(), _receipt(sol_delta=-_CAP), now=1_000.0 + index)
        assert verdict.authorized is True, f"call {index} should have passed"
    fourth = gate.authorize(_tx(), _receipt(sol_delta=-_CAP), now=1_003.0)
    assert fourth.authorized is False
    assert fourth.code == "over-hourly-cap"


def test_the_hourly_window_rolls() -> None:
    gate = _gate(_policy(hourly_cap_lamports=_CAP))
    assert gate.authorize(_tx(), _receipt(sol_delta=-_CAP), now=1_000.0).authorized
    assert not gate.authorize(_tx(), _receipt(sol_delta=-_CAP), now=2_000.0).authorized
    later = gate.authorize(_tx(), _receipt(sol_delta=-_CAP), now=1_000.0 + 3_601)
    assert later.authorized is True


def test_the_daily_cap_binds_independently_of_the_hourly_one() -> None:
    gate = _gate(
        _policy(
            hourly_cap_lamports=_CAP,
            daily_cap_lamports=2 * _CAP,
            max_transactions_per_day=100,
        )
    )
    start = 1_000.0
    for hour in range(2):
        verdict = gate.authorize(
            _tx(), _receipt(sol_delta=-_CAP), now=start + hour * 3_700
        )
        assert verdict.authorized is True
    third = gate.authorize(_tx(), _receipt(sol_delta=-_CAP), now=start + 2 * 3_700)
    assert third.authorized is False
    assert third.code == "over-daily-cap"


def test_the_transaction_count_cap_binds_even_when_every_amount_is_tiny() -> None:
    gate = _gate(_policy(max_transactions_per_day=2))
    for index in range(2):
        assert gate.authorize(
            _tx(), _receipt(sol_delta=-1), now=1_000.0 + index
        ).authorized
    third = gate.authorize(_tx(), _receipt(sol_delta=-1), now=1_002.0)
    assert third.authorized is False
    assert third.code == "over-daily-transaction-count"


def test_a_refusal_from_another_predicate_spends_no_velocity_budget() -> None:
    gate = _gate(_policy(max_transactions_per_day=1))
    refused = gate.authorize(_tx(writable=(OTHER,)), _receipt(), now=1_000.0)
    assert refused.authorized is False
    assert gate.authorize(_tx(), _receipt(), now=1_001.0).authorized is True


def test_a_gate_with_no_ledger_refuses() -> None:
    verdict = SpendPolicyGate(policy=_policy()).authorize(
        _tx(), _receipt(), now=1_000.0
    )
    assert verdict.authorized is False
    assert verdict.code == "velocity-ledger-unavailable"


def test_the_velocity_counter_is_labelled_advisory(tmp_path: Path) -> None:
    """C6: it ships labelled ADVISORY until its storage owner is settled."""
    source = _SPEND_POLICY_SOURCE.read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(source)) or ""
    assert "advisory" in docstring.lower()
    assert "ADVISORY" in source


def test_the_counter_is_cumulative_across_PROCESSES_though_not_beyond_the_agent(
    tmp_path: Path,
) -> None:
    """Cross-process, which the current ``--count`` loop counter is not.

    This proves the counter is SHARED. It does NOT prove the agent cannot reset it —
    the file is writable by the process it bounds, which is why C6 ships it advisory.
    """
    ledger_path = tmp_path / "spend.jsonl"
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(_REPO)!r})
        from gecko.spend_policy import FileSpendLedger, VelocityLimits
        ledger = FileSpendLedger({str(ledger_path)!r})
        limits = VelocityLimits(
            hourly_lamports={2 * _CAP},
            daily_lamports={10 * _CAP},
            max_transactions_per_day=10,
        )
        decision = ledger.reserve(at=1000.0, lamports={_CAP}, limits=limits)
        print(decision.within)
        """
    )
    outcomes = []
    for _ in range(3):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        outcomes.append(completed.stdout.strip())
    assert outcomes == ["True", "True", "False"], outcomes
    lines = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 2
    assert all(set(entry) == {"at", "lamports"} for entry in lines)


def test_the_file_ledger_prunes_entries_older_than_a_day(tmp_path: Path) -> None:
    from gecko.spend_policy import VelocityLimits

    ledger = FileSpendLedger(str(tmp_path / "spend.jsonl"))
    limits = VelocityLimits(
        hourly_lamports=_CAP, daily_lamports=_CAP, max_transactions_per_day=10
    )
    assert ledger.reserve(at=0.0, lamports=_CAP, limits=limits).within is True
    assert ledger.reserve(at=100.0, lamports=_CAP, limits=limits).within is False
    fresh = ledger.reserve(at=90_000.0, lamports=_CAP, limits=limits)
    assert fresh.within is True


def test_an_unreadable_ledger_refuses_rather_than_counting_zero(tmp_path: Path) -> None:
    path = tmp_path / "spend.jsonl"
    path.write_text("{not json at all\n", encoding="utf-8")
    verdict = _gate(ledger=FileSpendLedger(str(path))).authorize(
        _tx(), _receipt(), now=1_000.0
    )
    assert verdict.authorized is False
    assert verdict.code == "ledger-unreadable"


# --------------------------------------------------------------------------------------
# VERIFICATION AND AUTHORIZATION ARE DIFFERENT PREDICATES
# --------------------------------------------------------------------------------------


def test_the_gate_reads_no_verification_field_off_the_receipt() -> None:
    """Reading ``status``/``message_binding``/``network`` here would let one predicate
    stand in for the other. The gate reads ``sol_delta`` and nothing else."""
    source = _SPEND_POLICY_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    read_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    }
    for verification_field in (
        "message_binding",
        "binding_strength",
        "network",
        "status",
        "lookup_resolution",
        "observed_slot",
    ):
        assert verification_field not in read_attributes, verification_field
    assert "sol_delta" in read_attributes


def test_an_authorized_verdict_carries_no_bytes() -> None:
    verdict = _gate().authorize(_tx(), _receipt(), now=1_000.0)
    assert verdict.authorized is True
    assert not hasattr(verdict, "transaction_base64")
    assert isinstance(verdict, SpendVerdict)


# --------------------------------------------------------------------------------------
# C13 — NOTHING HERE SIGNS OR BROADCASTS
# --------------------------------------------------------------------------------------


#: Assembled from fragments so that THIS file does not contain the very tokens it bans —
#: otherwise the check below would fail on its own source and teach the next reader to
#: exclude the test file, which is where a real one would then be free to appear.
_BANNED_TOKENS = ("send" + "Transaction", "Key" + "pair", "sign_" + "transaction")


def test_no_new_file_can_sign_or_broadcast() -> None:
    for path in (_SPEND_POLICY_SOURCE, Path(__file__)):
        source = path.read_text(encoding="utf-8")
        for token in _BANNED_TOKENS:
            assert token not in source, f"{path.name} names {token}"
