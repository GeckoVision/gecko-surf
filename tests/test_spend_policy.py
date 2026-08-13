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
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from gecko.simulate import (
    Receipt,
    TokenDeltaRefusal,
    TokenDeltaReport,
    TokenMovement,
)
from gecko.spend_policy import (
    DEDUPE_SECONDS,
    AdvisorySpendLedger,
    AllowedInstruction,
    FileSpendLedger,
    InMemorySpendLedger,
    LedgerError,
    SpendPolicy,
    SpendPolicyGate,
    SpendVerdict,
    TokenCap,
    TokenCaps,
    VelocityLimits,
)
from gecko.txbind import DecodedMessage, decode_message, message_binding

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
    blockhash: Hash | None = None,
) -> str:
    """An UNSIGNED legacy transaction, base64. No key exists anywhere in this helper."""
    metas = [AccountMeta(payer, is_signer=True, is_writable=True)]
    metas += [AccountMeta(key, is_signer=False, is_writable=True) for key in writable]
    instruction = Instruction(program, data, metas)
    message = Message.new_with_blockhash(
        [instruction], payer, Hash.default() if blockhash is None else blockhash
    )
    return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()


def _distinct_tx(index: int) -> str:
    """The Nth DIFFERENT transaction. Same plan, its own blockhash, its own bytes.

    The cumulative-cap tests below mean "N transactions" and used to say it by calling
    ``_tx()`` N times. That worked only while the gate could not tell repetition from
    distinctness. Now that a reservation is idempotent on exact bytes, N identical calls
    are correctly ONE transaction submitted N times — at most one of which can settle,
    because they share a signature — so a test that wants N of them has to build N.
    """
    return _tx(blockhash=Hash.from_bytes(bytes([index + 1]) * 32))


#: A 6-decimal mint, and the raw amounts that make the fold visible: 25 USDC is
#: 25,000,000 raw, which is 0.025 SOL if anybody ever adds it to a lamport window.
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ONE_USDC = 1_000_000
TWENTY_FIVE_USDC = 25_000_000


def _measured(*movements: TokenMovement) -> TokenDeltaReport:
    """A token leg that was READ. No movements is an OBSERVED zero, never "not tracked"."""
    return TokenDeltaReport(status="measured", movements=movements, refusals=())


def _movement(
    *,
    mint: str = MINT,
    owner: str = str(PAYER),
    decimals: int = 6,
    delta_raw: int = -TWENTY_FIVE_USDC,
) -> TokenMovement:
    return TokenMovement(
        mint=mint,
        owner=owner,
        decimals=decimals,
        pre_raw=100_000_000,
        post_raw=100_000_000 + delta_raw,
        delta_raw=delta_raw,
        ui_delta=str(delta_raw),
    )


def _receipt(
    *,
    sol_delta: int | None = -1_000,
    token_delta: TokenDeltaReport | None = None,
    track_tokens: bool = True,
) -> Receipt:
    """A receipt whose TOKEN leg was read as well as its lamport one.

    ``track_tokens=False`` is the NOT-TRACKED state — ``token_delta is None``, which is
    what stock ``simulateTransaction`` produces and which this gate refuses on a
    token-capable message rather than reading as zero.
    """
    return Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=10_000,
        sol_delta=sol_delta,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
        token_delta=(
            (token_delta if token_delta is not None else _measured())
            if track_tokens
            else None
        ),
    )


def _token_caps() -> TokenCaps:
    return TokenCaps.of(
        (
            TokenCap(
                mint=MINT,
                decimals=6,
                per_transaction_raw=ONE_USDC,
                hourly_raw=2 * ONE_USDC,
                daily_raw=4 * ONE_USDC,
            ),
        )
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
        "token_caps": _token_caps(),
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
        verdict = gate.authorize(
            _distinct_tx(index), _receipt(sol_delta=-_CAP), now=1_000.0 + index
        )
        assert verdict.authorized is True, f"call {index} should have passed"
    fourth = gate.authorize(_distinct_tx(3), _receipt(sol_delta=-_CAP), now=1_003.0)
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
            _distinct_tx(index), _receipt(sol_delta=-1), now=1_000.0 + index
        ).authorized
    third = gate.authorize(_distinct_tx(2), _receipt(sol_delta=-1), now=1_002.0)
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
    """C6: it ships labelled ADVISORY until its storage owner is settled.

    The word alone is not enough. "advisory" also appears in the overview near the top of
    the module docstring, so asserting only on it let the entire RESIDUALS section be
    deleted with the suite still green (measured: 51 passed) — the label survived while
    the reason it is only a label went away. A reader would then find a counter labelled
    advisory and no statement of what makes it inexact.

    So the claims that make the label mean something are pinned by name. Whitespace is
    folded because they are prose and wrap wherever the paragraph reflows.

    THIS TEST WENT RED ON PURPOSE and was rewritten here. It used to pin "the retry
    double-reserves" and "over-counts on retries", with a note saying that when the
    counter was actually made idempotent it SHOULD fail. B3 made it idempotent on exact
    bytes, so those two sentences are no longer true and pinning them would now force the
    docstring to lie. What replaces them is the residual that is STILL true and the reason
    the obvious closure of it was refused — because that reason is the part a future reader
    is most likely to "fix" by reaching for a structural binding.
    """
    source = _SPEND_POLICY_SOURCE.read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(source)) or ""
    assert "advisory" in docstring.lower()
    assert "ADVISORY" in source

    folded = " ".join(docstring.split()).lower()
    for claim in ("still double-reserves", "errs toward a cap bypass"):
        assert claim in folded, (
            f"the residual that makes ADVISORY meaningful is gone: {claim}"
        )


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
# CAP 5 — PER-MINT TOKEN CAPS, IN THE MINT'S OWN RAW BASE UNITS
# --------------------------------------------------------------------------------------


def test_a_token_drain_that_moves_almost_no_lamports_is_refused() -> None:
    """THE BUG, at this level. 25 USDC out; the lamport leg is a 1,000-lamport fee.

    Every lamport-denominated cap in this policy authorises this transaction. Only a cap
    written in the mint's own units can see it.
    """
    verdict = _gate().authorize(
        _tx(), _receipt(token_delta=_measured(_movement())), now=1_000.0
    )
    assert verdict.authorized is False
    assert verdict.code == "over-per-transaction-token-cap"
    assert verdict.outflow_lamports == 1_000, (
        "the lamport figure must stay a LAMPORT figure — the moment a raw token amount "
        "is folded in here, 25 USDC starts reading as 0.025 SOL"
    )


def test_a_raw_token_amount_never_enters_the_lamport_windows() -> None:
    """A fold would spend the hourly LAMPORT budget on a token transfer.

    Four half-USDC spends are authorised; the lamport windows must show only the four
    1,000-lamport fees, not 2,000,000 raw units of somebody else's denomination.
    """
    gate = _gate(_policy(hourly_cap_lamports=10_000))
    for index in range(4):
        verdict = gate.authorize(
            _tx(),
            _receipt(token_delta=_measured(_movement(delta_raw=-500_000))),
            now=1_000.0 + index,
        )
        assert verdict.authorized is True, f"call {index}: {verdict.reason}"
        assert verdict.outflow_lamports == 1_000
    assert [spend.mint for spend in verdict.outflow_tokens] == [MINT]
    assert [spend.raw for spend in verdict.outflow_tokens] == [500_000]


def test_a_mint_the_policy_never_named_refuses() -> None:
    other = "So11111111111111111111111111111111111111112"
    verdict = _gate().authorize(
        _tx(),
        _receipt(token_delta=_measured(_movement(mint=other, delta_raw=-1))),
        now=1_000.0,
    )
    assert verdict.authorized is False
    assert verdict.code == "mint-not-allowlisted"


def test_a_decimals_disagreement_refuses_rather_than_rescaling() -> None:
    """A raw cap means nothing without the scale it was written at."""
    verdict = _gate().authorize(
        _tx(),
        _receipt(token_delta=_measured(_movement(decimals=2, delta_raw=-100))),
        now=1_000.0,
    )
    assert verdict.authorized is False
    assert verdict.code == "token-decimals-mismatch"


def test_an_unauthored_token_cap_map_refuses_as_incomplete() -> None:
    """ "No cap that is unlimited because nobody thought about it", extended to cap 5."""
    verdict = _gate(_policy(token_caps=None)).authorize(_tx(), _receipt(), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "policy-incomplete"
    assert "token_caps" in verdict.reason


def test_an_authored_empty_map_is_a_sentence_and_refuses_any_token_movement() -> None:
    """``TokenCaps.none()`` is authored; an absent map is not. They are different answers.

    With ``none()`` the policy is COMPLETE — a lamport-only transaction is authorised —
    and any token outflow at all is refused as an unlisted mint.
    """
    gate = _gate(_policy(token_caps=TokenCaps.none()))
    assert gate.authorize(_tx(), _receipt(), now=1_000.0).authorized is True

    moving = gate.authorize(
        _tx(), _receipt(token_delta=_measured(_movement(delta_raw=-1))), now=1_001.0
    )
    assert moving.authorized is False
    assert moving.code == "mint-not-allowlisted"


def test_an_unmeasurable_token_leg_refuses_rather_than_reading_zero() -> None:
    """S2's third state. ``outflows()`` raises here, and a raise is not a zero."""
    unmeasurable = TokenDeltaReport(
        status="unmeasurable",
        movements=(),
        refusals=(
            TokenDeltaRefusal(
                reason="transfer-hook",
                mint=MINT,
                detail="an extra program runs on transfer; the balances cannot see it",
            ),
        ),
    )
    verdict = _gate().authorize(_tx(), _receipt(token_delta=unmeasurable), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "amount-unresolvable"


def test_an_untracked_token_leg_refuses_on_a_token_capable_message() -> None:
    """``None`` is NOT TRACKED, and stock ``simulateTransaction`` returns exactly that."""
    verdict = _gate().authorize(_tx(), _receipt(track_tokens=False), now=1_000.0)
    assert verdict.authorized is False
    assert verdict.code == "token-leg-not-measured"


def test_an_untracked_token_leg_is_a_FACT_when_no_program_could_move_a_token() -> None:
    """The one derivation, and it is a derivation rather than an exemption.

    A message built only from the System program has no token leg to measure: that
    program cannot invoke another. Anything else — Memo included, deliberately — is
    treated as token-capable.
    """
    system = Pubkey.from_string("11111111111111111111111111111111")
    policy = _policy(
        allowed_instructions=frozenset(
            {
                AllowedInstruction(
                    program_id=str(system), discriminator=b"\x02\x00\x00\x00"
                )
            }
        )
    )
    verdict = _gate(policy).authorize(
        _tx(program=system, data=b"\x02\x00\x00\x00" + b"\x10" * 8),
        _receipt(track_tokens=False),
        now=1_000.0,
    )
    assert verdict.authorized is True, verdict.reason


def test_the_per_mint_velocity_binds_in_the_mints_own_units() -> None:
    """The `singleTxLimit` lesson again: a per-tx token cap of X is defeated by N of X."""
    gate = _gate()
    for index in range(4):
        verdict = gate.authorize(
            _distinct_tx(index),
            _receipt(token_delta=_measured(_movement(delta_raw=-500_000))),
            now=1_000.0 + index,
        )
        assert verdict.authorized is True, f"call {index}: {verdict.reason}"
    fifth = gate.authorize(
        _distinct_tx(4),
        _receipt(token_delta=_measured(_movement(delta_raw=-500_000))),
        now=1_004.0,
    )
    assert fifth.authorized is False
    assert fifth.code == "over-hourly-token-cap"


def test_a_token_row_carries_a_mint_and_never_an_owner_or_a_binding(
    tmp_path: Path,
) -> None:
    """Invariant #1 on the ledger schema, as narrowed by D-B and no further.

    A mint is a public program-surface identifier. An owner, a token-account address or a
    payload is not, and none may appear. The one-way dedupe digest MAY appear, on the
    lamport row only — and the point of this test after D-B is that the concession stops
    exactly there: it did not become a licence to record everything about a transaction.
    """
    path = tmp_path / "spend.jsonl"
    verdict = _gate(ledger=FileSpendLedger(str(path))).authorize(
        _tx(),
        _receipt(token_delta=_measured(_movement(delta_raw=-500_000))),
        now=1_000.0,
    )
    assert verdict.authorized is True

    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {frozenset(row) for row in rows} == {
        frozenset({"at", "lamports", "d"}),
        frozenset({"at", "mint", "raw"}),
    }
    token_row = next(row for row in rows if "mint" in row)
    assert token_row["mint"] == MINT
    assert token_row["raw"] == 500_000
    # The digest rides the lamport row alone. A token row carrying it too would be the
    # same fact stored twice, free to disagree with itself, and would additionally make
    # the per-mint history the correlatable part.
    assert "d" not in token_row
    for row in rows:
        assert not (set(row) & {"owner", "account", "binding", "tx", "payload"})

    # It is a HASH, not the transaction. The bytes must not be recoverable from the row,
    # and the cheapest way that could go wrong is somebody storing the base64 itself.
    digest = next(row for row in rows if "mint" not in row)["d"]
    assert digest != _tx()
    assert _tx() not in json.dumps(rows)
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")


# --------------------------------------------------------------------------------------
# B3 — THE RESERVATION IS IDEMPOTENT ON EXACT BYTES
# --------------------------------------------------------------------------------------


def test_retrying_the_identical_transaction_reserves_budget_once() -> None:
    """B3, the defect itself: a retried backend failure must not spend budget twice.

    The cap here admits exactly one transaction. Before B3 the second call took a second
    reservation and the third was refused for a budget that had never been spent, because
    only one of three identical messages can ever settle — they share a signature.
    """
    gate = _gate(_policy(hourly_cap_lamports=_CAP, max_transactions_per_day=1))
    same = _tx()
    for attempt in range(3):
        verdict = gate.authorize(same, _receipt(sol_delta=-_CAP), now=1_000.0 + attempt)
        assert verdict.authorized is True, f"attempt {attempt}: {verdict.reason}"

    # And the budget really is still only spent once: a DIFFERENT transaction is now
    # refused, which is what proves the three above consumed one slot rather than none.
    other = gate.authorize(_distinct_tx(9), _receipt(sol_delta=-_CAP), now=1_003.0)
    assert other.authorized is False
    assert other.code in {"over-hourly-cap", "over-daily-transaction-count"}


def test_the_replay_says_it_did_not_reserve_again() -> None:
    """The verdict distinguishes "allowed, and charged" from "allowed, already charged".

    A replay that reported the ordinary success sentence would be indistinguishable from a
    fresh reservation in a log, which is where somebody debugging a budget discrepancy
    looks first.
    """
    gate = _gate(_policy(hourly_cap_lamports=2 * _CAP))
    same = _tx()
    first = gate.authorize(same, _receipt(sol_delta=-_CAP), now=1_000.0)
    replay = gate.authorize(same, _receipt(sol_delta=-_CAP), now=1_001.0)
    assert first.authorized is replay.authorized is True
    assert "idempotent" in replay.reason
    assert "idempotent" not in first.reason


def test_two_distinct_transactions_are_never_collapsed() -> None:
    """The other direction, and the one that would be a cap bypass if it broke.

    Deduplication that matched too widely would let an agent spend N times on one
    reservation. Same plan, same amount, same everything a policy reads — different bytes,
    so two reservations.
    """
    gate = _gate(_policy(hourly_cap_lamports=_CAP, max_transactions_per_day=5))
    assert gate.authorize(
        _distinct_tx(0), _receipt(sol_delta=-_CAP), now=1_000.0
    ).authorized
    second = gate.authorize(_distinct_tx(1), _receipt(sol_delta=-_CAP), now=1_001.0)
    assert second.authorized is False
    assert second.code == "over-hourly-cap"


def test_the_key_is_the_exact_binding_and_not_the_structural_one() -> None:
    """WHY ``exact``: a structural binding would collapse two deliberate transfers.

    ``structural`` normalises the blockhash to zero, so these two re-quotes of one plan
    share a structural binding and differ in their exact one. If the ledger keyed on the
    former, a human who sent the same amount twice on purpose would be charged once — a
    cap bypass reachable by repetition. This pins the choice, not just its effect.
    """
    first, second = _distinct_tx(0), _distinct_tx(1)
    assert message_binding(first, strength="structural") == message_binding(
        second, strength="structural"
    )
    assert message_binding(first, strength="exact") != message_binding(
        second, strength="exact"
    )

    # And the value actually written is the exact one.
    ledger = InMemorySpendLedger()
    _gate(_policy(hourly_cap_lamports=_CAP), ledger=ledger).authorize(
        first, _receipt(sol_delta=-1), now=1_000.0
    )
    stored = [entry.digest for entry in ledger._entries if entry.digest is not None]
    assert stored == [message_binding(first, strength="exact")]


def test_a_retry_that_re_quotes_still_double_reserves_the_named_residual() -> None:
    """The residual, pinned so nobody closes it the wrong way and calls that progress.

    A retry handled past blockhash validity carries new bytes, so it is not recognised and
    reserves again. That is NOT fixed here. It is the safe direction — over-counting errs
    toward refusing — and the only way to close it is a blockhash-insensitive key, which
    the test above shows would open a cap bypass. If this test ever goes red, read that
    one before deciding it is an improvement.
    """
    gate = _gate(_policy(hourly_cap_lamports=_CAP, max_transactions_per_day=5))
    assert gate.authorize(
        _distinct_tx(0), _receipt(sol_delta=-_CAP), now=1_000.0
    ).authorized
    requoted = gate.authorize(_distinct_tx(1), _receipt(sol_delta=-_CAP), now=1_060.0)
    assert requoted.authorized is False, (
        "the re-quoted retry is a NAMED residual: it still reserves a second time"
    )


def test_the_dedupe_window_expires_and_the_amount_outlives_the_digest() -> None:
    """After ``DEDUPE_SECONDS`` the row stops saying WHICH transaction it was.

    Both halves matter. The digest going away is the data-governance bound that keeps D-B's
    concession narrow; the amount staying is what keeps the daily cap a daily cap.
    """
    ledger = InMemorySpendLedger()
    gate = _gate(_policy(daily_cap_lamports=2 * _CAP), ledger=ledger)
    same = _tx()
    assert gate.authorize(same, _receipt(sol_delta=-_CAP), now=1_000.0).authorized

    later = 1_000.0 + DEDUPE_SECONDS + 1
    assert gate.authorize(same, _receipt(sol_delta=-_CAP), now=later).authorized, (
        "past the window these are two transactions, and both fit the daily cap"
    )
    # The first row survives with its amount and without its digest.
    assert [entry.amount for entry in ledger._entries] == [_CAP, _CAP]
    assert [entry.digest is None for entry in ledger._entries] == [True, False]

    # The daily cap therefore still binds on the amounts that outlived their digests.
    third = gate.authorize(_distinct_tx(7), _receipt(sol_delta=-_CAP), now=later + 1)
    assert third.authorized is False
    assert third.code == "over-daily-cap"


def test_two_expired_digests_do_not_match_each_other() -> None:
    """A ``None`` digest is never a replay — the hazard of comparing absence to absence.

    Written because the natural implementation (``entry.digest == digest``) makes every
    row whose digest expired a free pass for every future call that also has none. That is
    a total bypass of the cumulative cap, reachable by waiting fifteen minutes.
    """
    ledger = InMemorySpendLedger()
    limits = VelocityLimits(
        hourly_lamports=10 * _CAP, daily_lamports=10 * _CAP, max_transactions_per_day=2
    )
    assert ledger.reserve(at=1_000.0, lamports=1, limits=limits, digest=None).within
    assert ledger.reserve(at=1_001.0, lamports=1, limits=limits, digest=None).within
    third = ledger.reserve(at=1_002.0, lamports=1, limits=limits, digest=None)
    assert third.within is False, (
        "two unkeyed reservations must both count; matching None to None would have "
        "made the second a replay and left the count cap unreachable"
    )


def test_there_is_no_parameter_through_which_a_caller_could_choose_the_key() -> None:
    """The PR-1-shaped hazard is absent by CONSTRUCTION, not by validation.

    A caller-chosen idempotency key is a request to spend for free: reuse one value across
    different transactions and everything after the first reads as already paid. No
    validation can catch that, because the key would be well-formed. So ``authorize``
    exposes no such parameter and the key is derived from the subject.
    """
    import inspect

    parameters = set(inspect.signature(SpendPolicyGate.authorize).parameters)
    assert parameters == {
        "self",
        "transaction_base64",
        "receipt",
        "now",
        "agent_supplied_policy",
    }
    for forbidden in ("digest", "idempotency_key", "key", "nonce", "dedupe"):
        assert forbidden not in parameters


def test_the_digest_is_derived_from_the_bytes_and_not_from_the_receipt() -> None:
    """G4 stays intact: the gate reads two AMOUNT fields off the receipt and no others.

    The obvious way to build this would have been ``receipt.message_binding``, which is
    already computed and sitting right there. It is also a VERIFICATION field, and letting
    verification stand in for authorization is the substitution the module exists to
    prevent — so the digest comes from the transaction argument the gate already decodes.
    """
    source = _SPEND_POLICY_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    read = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "receipt"
    }
    assert read & {field.name for field in fields(Receipt)} == {
        "sol_delta",
        "token_delta",
    }
    assert "message_binding" not in read


def test_a_ledger_written_before_dedupe_existed_is_still_readable(
    tmp_path: Path,
) -> None:
    """Upgrading must not read as corruption. ``d`` is optional in both directions.

    ``_read_entries`` refuses a row it cannot fully account for, and that refusal is a
    total one — an unreadable ledger refuses every transaction. So adding a REQUIRED field
    would have turned the first run after an upgrade into an outage whose message said the
    budget file was corrupt.
    """
    path = tmp_path / "spend.jsonl"
    path.write_text(
        json.dumps({"at": 1_000.0, "lamports": _CAP}) + "\n"
        f'{{"at": 1000.0, "mint": "{MINT}", "raw": 500000}}\n',
        encoding="utf-8",
    )
    verdict = _gate(
        _policy(hourly_cap_lamports=_CAP), ledger=FileSpendLedger(str(path))
    ).authorize(_tx(), _receipt(sol_delta=-1), now=1_001.0)
    assert verdict.authorized is False, "the legacy row still counts against the cap"
    assert verdict.code == "over-hourly-cap"


def test_a_row_whose_digest_is_the_wrong_shape_refuses_rather_than_reading_past(
    tmp_path: Path,
) -> None:
    """Half-identifying a transaction is refused, not read as unidentified.

    The permissive reading — treat an unusable ``d`` as absent — would make a truncated
    write look like a row that never had a digest, so a retry of that transaction would
    reserve a second time. Small, but it is the failure this whole change exists to remove.
    """
    path = tmp_path / "spend.jsonl"
    path.write_text(
        json.dumps({"at": 1_000.0, "lamports": 1, "d": ""}) + "\n", encoding="utf-8"
    )
    with pytest.raises(LedgerError):
        FileSpendLedger(str(path)).reserve(
            at=1_001.0,
            lamports=1,
            limits=VelocityLimits(
                hourly_lamports=_CAP,
                daily_lamports=_CAP,
                max_transactions_per_day=5,
            ),
        )


def test_idempotency_holds_ACROSS_PROCESSES(tmp_path: Path) -> None:
    """The retry that matters is the one after the process died and was restarted.

    An in-process memo would cover a loop and miss exactly the case B3 is for: a backend
    fault, an operator re-running the command. So the key lives in the shared file, and
    this proves it by spending from three separate interpreters.
    """
    ledger_path = tmp_path / "spend.jsonl"
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(_REPO)!r})
        from gecko.spend_policy import FileSpendLedger, VelocityLimits
        ledger = FileSpendLedger({str(ledger_path)!r})
        limits = VelocityLimits(
            hourly_lamports={_CAP},
            daily_lamports={_CAP},
            max_transactions_per_day=1,
        )
        decision = ledger.reserve(
            at=1000.0, lamports={_CAP}, limits=limits, digest="deadbeef" * 8
        )
        print(decision.within)
        """
    )
    outcomes = []
    for _ in range(3):
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        )
        outcomes.append(completed.stdout.strip())
    assert outcomes == ["True", "True", "True"], outcomes

    rows = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1, f"three identical reserves wrote {len(rows)} rows: {rows}"


def test_a_ledger_row_with_a_field_this_module_cannot_account_for_refuses(
    tmp_path: Path,
) -> None:
    """A malformed line stays a LedgerError — never a skipped line, never a smaller total.

    Written with an OWNER, because that is the field most likely to be added by somebody
    who wants better attribution and is exactly the one invariant #1 forbids.
    """
    path = tmp_path / "spend.jsonl"
    path.write_text(
        json.dumps({"at": 1_000.0, "mint": MINT, "raw": 1, "owner": str(PAYER)}) + "\n",
        encoding="utf-8",
    )
    verdict = _gate(ledger=FileSpendLedger(str(path))).authorize(
        _tx(), _receipt(), now=1_001.0
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
    # The AMOUNT fields, and ONLY those. ``token_delta`` joined ``sol_delta`` when cap 5
    # landed: it is what the transaction MOVES, not a claim about whether it will land.
    amount_fields = {"sol_delta", "token_delta"}
    assert amount_fields <= read_attributes, "the gate must still read what it charges"

    # Closed against the dataclass, not against a hand-kept list of six names. The list
    # could only forbid what someone had thought to add to it, so a SECOND amount source
    # — ``receipt.tokens_received``, a real Receipt field it did not mention — was read
    # straight past it. Deriving the set from ``fields(Receipt)`` forbids every field the
    # gate has no business reading, including fields that do not exist yet: a new one
    # arrives forbidden and has to be argued for here.
    receipt_fields = {field.name for field in fields(Receipt)}
    read_off_the_receipt = receipt_fields & read_attributes
    assert read_off_the_receipt == amount_fields, (
        "the gate reads a Receipt field that is not an amount: "
        f"{sorted(read_off_the_receipt - amount_fields)}"
    )


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
