"""The state-advancing fork preflight — falsified entirely offline, with a light fake.

The bug these tests exist for: :func:`gecko.simulate.simulate` never SENDS, so four
consecutive simulations all run against the same pre-state. A lifecycle whose second
instruction needs the account its first instruction creates therefore reports
``AccountNotInitialized`` (Anchor 3012) at every step after the first — a STATE gap
reported as a derivation gap. The runner closes it by carrying each step's
post-execution account state forward as an override for the next step.

Every test here runs against a `FakeFork` (one dict of account state, no network), so
the runner is falsifiable with no RPC, no keypair and no spend. Per Pattern B that fake
is the FIRST deliverable; the surfpool run is the last check, never the debugger.
"""

from __future__ import annotations

import ast
import base64
from pathlib import Path
from typing import Any

import pytest

from gecko.fork_preflight import (
    AccountState,
    FundingRequirement,
    OverlayUnsupported,
    PreflightStep,
    minimum_balance_for_rent_exemption,
    run_preflight,
    surfnet_overlay,
)
from gecko.simulate import BuiltTx

RPC = "http://127.0.0.1:8899"

RECEIPTS = "9Rec3ipts1111111111111111111111111111111111"
AUTHORITY = "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy"
PROGRAM = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
SYSTEM = "11111111111111111111111111111111"

#: What a 165-byte-ish account costs to keep alive. The fake answers this to
#: getMinimumBalanceForRentExemption so the rent read has something to be wrong about.
RENT_EXEMPT = 2_039_280


def _account(
    lamports: int, *, data: bytes = b"", owner: str = SYSTEM
) -> dict[str, Any]:
    """An account object shaped exactly as a Solana RPC returns one (base64 data)."""
    return {
        "lamports": lamports,
        "owner": owner,
        "data": [base64.b64encode(data).decode(), "base64"],
        "executable": False,
        "rentEpoch": 0,
    }


class FakeFork:
    """A three-method fake validator: getAccountInfo, simulateTransaction, setAccount.

    It models the ONE thing the bug is about — that state does not advance unless
    somebody advances it. ``simulateTransaction`` reads the CURRENT state, computes the
    step's declared effect, and returns it as the post-account set. It never writes:
    only ``surfnet_setAccount`` writes, which is what makes "did the runner carry state
    forward" observable instead of assumed.
    """

    def __init__(self, state: dict[str, dict[str, Any]] | None = None) -> None:
        self.state: dict[str, dict[str, Any]] = dict(state or {})
        self.methods: list[str] = []
        #: tx-id -> (required_initialized_accounts, {address: post_account})
        self.effects: dict[str, tuple[tuple[str, ...], dict[str, dict[str, Any]]]] = {}
        self.rent_minimum = RENT_EXEMPT
        self.malformed_post = False

    def call(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.methods.append(method)
        if method == "getAccountInfo":
            return {
                "result": {"context": {"slot": 42}, "value": self.state.get(params[0])}
            }
        if method == "getMinimumBalanceForRentExemption":
            return {"result": self.rent_minimum}
        if method == "surfnet_setAccount":
            pubkey, update = params[0], params[1]
            current = dict(self.state.get(pubkey) or _account(0))
            current.update(
                {
                    "lamports": update["lamports"],
                    "owner": update["owner"],
                    "data": [update["data"], "base64"],
                }
            )
            self.state[pubkey] = current
            return {"result": {"value": None}}
        if method == "simulateTransaction":
            return self._simulate(params)
        raise AssertionError(f"FakeFork got an unexpected RPC method {method!r}")

    def _simulate(self, params: list[Any]) -> dict[str, Any]:
        tx = params[0]
        config = params[1]
        addresses = list((config.get("accounts") or {}).get("addresses") or [])
        needs, post = self.effects[tx]
        for address in needs:
            existing = self.state.get(address)
            if existing is None or not base64.b64decode(existing["data"][0]):
                return {
                    "result": {
                        "context": {"slot": 42},
                        "value": {
                            "err": {"InstructionError": [0, {"Custom": 3012}]},
                            "logs": [
                                "Program log: AnchorError caused by account: receipts. "
                                "Error Code: AccountNotInitialized. Error Number: 3012."
                            ],
                            "unitsConsumed": 4_101,
                            "accounts": None,
                        },
                    }
                }
        merged = {**self.state, **post}
        accounts: list[Any] = []
        for address in addresses:
            value = merged.get(address)
            if value is not None and self.malformed_post:
                value = {**value, "lamports": "2039280"}  # untrusted: a numeric STRING
            accounts.append(value)
        return {
            "result": {
                "context": {"slot": 43},
                "value": {
                    "err": None,
                    "logs": ["Program log: ok"],
                    "unitsConsumed": 21_000,
                    "accounts": accounts,
                },
            }
        }


def _fork_with_lifecycle() -> FakeFork:
    """A fork where `make_purchase` only lands once `initialize` has created `receipts`.

    This is the shape of the real finding: the second instruction is not derivable-wrong,
    it is state-dependent, and four independent simulations cannot see that.
    """
    fork = FakeFork({AUTHORITY: _account(10 * RENT_EXEMPT)})
    fork.effects["tx-initialize"] = (
        (),
        {RECEIPTS: _account(RENT_EXEMPT, data=b"store", owner=PROGRAM)},
    )
    fork.effects["tx-make-purchase"] = ((RECEIPTS,), {})
    return fork


def _steps() -> list[PreflightStep]:
    return [
        PreflightStep(
            name="initialize",
            plan={"tx": "tx-initialize"},
            carry=(RECEIPTS,),
            fee_payer=AUTHORITY,
        ),
        PreflightStep(
            name="make_purchase",
            plan={"tx": "tx-make-purchase"},
            carry=(RECEIPTS,),
            fee_payer=AUTHORITY,
        ),
    ]


def _build(plan: Any) -> BuiltTx:
    return BuiltTx(tx=str(plan["tx"]), encoding="base64")


# --- the bug: state must advance between steps -------------------------------


def test_the_second_step_sees_the_state_the_first_step_created() -> None:
    # Without carry-forward BOTH steps run against the same pre-state and the second
    # reports AccountNotInitialized (3012). That is the exact probe finding.
    fork = _fork_with_lifecycle()
    report = run_preflight(
        _steps(),
        rpc_url=RPC,
        network="fork",
        rpc_call=fork.call,
        build_call=_build,
        overlay_apply=surfnet_overlay(RPC, fork.call),
    )
    assert [outcome.status for outcome in report.steps] == ["pass", "pass"]
    assert report.ok is True
    assert report.exit_code == 0
    second = report.steps[1]
    assert second.receipt is not None
    assert second.receipt.revert_class is None
    assert second.receipt.units_consumed == 21_000


def test_without_carrying_state_the_second_step_reverts_3012() -> None:
    # The control that proves the fake reproduces the bug rather than hiding it: declare
    # nothing to carry and the run degrades to four independent simulations again.
    fork = _fork_with_lifecycle()
    steps = [step.without_carry() for step in _steps()]
    report = run_preflight(
        steps,
        rpc_url=RPC,
        network="fork",
        rpc_call=fork.call,
        build_call=_build,
        overlay_apply=surfnet_overlay(RPC, fork.call),
    )
    assert report.steps[0].status == "pass"
    assert report.steps[1].status == "fail"
    assert report.steps[1].receipt is not None
    assert report.steps[1].receipt.revert_class == "account_error"
    assert report.ok is False


# --- fail-closed: no overlay support is a REFUSAL, never a shared pre-state ---


def test_an_absent_overlay_seam_refuses_instead_of_sharing_pre_state() -> None:
    fork = _fork_with_lifecycle()
    report = run_preflight(
        _steps(),
        rpc_url=RPC,
        network="fork",
        rpc_call=fork.call,
        build_call=_build,
        # overlay_apply omitted on purpose: absence is a refusal, never a silent no-op.
    )
    assert report.steps[0].status == "pass"
    assert report.steps[1].status == "refused"
    assert "overlay" in (report.steps[1].refusal or "").lower()
    assert report.ok is False
    assert report.exit_code != 0


def test_surfnet_overlay_raises_overlay_unsupported_when_the_rpc_rejects_it() -> None:
    def rejecting(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        from gecko.rpc import RpcError

        raise RpcError("JSON-RPC surfnet_setAccount failed: code=-32601")

    apply_overlay = surfnet_overlay(RPC, rejecting)
    with pytest.raises(OverlayUnsupported):
        apply_overlay({RECEIPTS: AccountState(RECEIPTS, 1, SYSTEM, "", False)})


# --- fail-closed: a failed step does not let the run report success ----------


def test_a_failing_step_stops_the_chain_and_the_exit_code_is_not_zero() -> None:
    fork = _fork_with_lifecycle()
    fork.effects["tx-initialize"] = ((RECEIPTS,), {})  # initialize itself now reverts
    report = run_preflight(
        _steps(),
        rpc_url=RPC,
        network="fork",
        rpc_call=fork.call,
        build_call=_build,
        overlay_apply=surfnet_overlay(RPC, fork.call),
    )
    assert report.steps[0].status == "fail"
    assert report.steps[1].status == "not_attempted"
    assert report.ok is False
    assert report.exit_code == 1


# --- the rent/existence read: underfunding refuses BEFORE it is a revert -----


def test_an_underfunded_fee_payer_is_refused_before_any_simulation() -> None:
    # Nothing in gecko called getMinimumBalanceForRentExemption, so underfunding only ever
    # surfaced downstream as insufficient_funds. Here it is a refusal with a number.
    fork = _fork_with_lifecycle()
    fork.state[AUTHORITY] = _account(1_000)
    steps = [
        PreflightStep(
            name="initialize",
            plan={"tx": "tx-initialize"},
            carry=(RECEIPTS,),
            fee_payer=AUTHORITY,
            funding=(
                FundingRequirement(address=AUTHORITY, data_len=0, role="fee payer"),
            ),
        )
    ]
    report = run_preflight(
        steps,
        rpc_url=RPC,
        network="fork",
        rpc_call=fork.call,
        build_call=_build,
        overlay_apply=surfnet_overlay(RPC, fork.call),
    )
    assert report.steps[0].status == "refused"
    refusal = report.steps[0].refusal or ""
    assert "rent" in refusal.lower()
    assert str(RENT_EXEMPT - 1_000) in refusal  # the SHORTFALL, not just a verdict
    assert "simulateTransaction" not in fork.methods
    assert report.ok is False


def test_an_absent_required_account_is_named_not_simulated_into_a_revert() -> None:
    fork = _fork_with_lifecycle()
    steps = [
        PreflightStep(
            name="initialize",
            plan={"tx": "tx-initialize"},
            carry=(RECEIPTS,),
            fee_payer=AUTHORITY,
            funding=(
                FundingRequirement(
                    address=RECEIPTS, data_len=0, role="receipts", must_exist=True
                ),
            ),
        )
    ]
    report = run_preflight(
        steps,
        rpc_url=RPC,
        network="fork",
        rpc_call=fork.call,
        build_call=_build,
        overlay_apply=surfnet_overlay(RPC, fork.call),
    )
    assert report.steps[0].status == "refused"
    assert "does not exist" in (report.steps[0].refusal or "")
    assert "simulateTransaction" not in fork.methods


def test_minimum_balance_for_rent_exemption_refuses_a_non_integer_answer() -> None:
    # Untrusted transport: a numeric string is not a lamport count. Never coerced.
    def lying(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        return {"result": "2039280"}

    from gecko.fork_preflight import PreflightError

    with pytest.raises(PreflightError):
        minimum_balance_for_rent_exemption(0, rpc_url=RPC, rpc_call=lying)


# --- untrusted RPC output: a post-account we cannot read is not carried ------


def test_a_malformed_post_account_refuses_rather_than_carrying_a_coerced_zero() -> None:
    fork = _fork_with_lifecycle()
    fork.malformed_post = True
    report = run_preflight(
        _steps(),
        rpc_url=RPC,
        network="fork",
        rpc_call=fork.call,
        build_call=_build,
        overlay_apply=surfnet_overlay(RPC, fork.call),
    )
    assert report.steps[0].status == "refused"
    assert "lamports" in (report.steps[0].refusal or "")
    assert report.ok is False


# --- control plane + no-broadcast boundary ----------------------------------


def test_the_runner_issues_only_read_and_local_overlay_methods() -> None:
    fork = _fork_with_lifecycle()
    run_preflight(
        _steps(),
        rpc_url=RPC,
        network="fork",
        rpc_call=fork.call,
        build_call=_build,
        overlay_apply=surfnet_overlay(RPC, fork.call),
    )
    assert set(fork.methods) <= {
        "getAccountInfo",
        "getMinimumBalanceForRentExemption",
        "simulateTransaction",
        "surfnet_setAccount",
    }
    assert "sendTransaction" not in fork.methods


def test_the_report_carries_the_asserted_network_and_never_infers_it() -> None:
    fork = _fork_with_lifecycle()
    report = run_preflight(
        _steps(),
        rpc_url=RPC,
        network="fork",
        rpc_call=fork.call,
        build_call=_build,
        overlay_apply=surfnet_overlay(RPC, fork.call),
    )
    assert report.network == "fork"
    assert all(o.receipt is None or o.receipt.network == "fork" for o in report.steps)


# --- the boundary parse: a lifecycle is data, and it fails closed ------------


def test_steps_from_dicts_types_the_lifecycle_and_refuses_a_mistyped_carry() -> None:
    from gecko.fork_preflight import PreflightError, steps_from_dicts

    steps = steps_from_dicts(
        [
            {
                "name": "initialize",
                "plan": {"build_url": "https://example.test/build"},
                "carry": [RECEIPTS],
                "fee_payer": AUTHORITY,
                "funding": [
                    {"address": AUTHORITY, "data_len": 0, "role": "fee payer"},
                    {"address": RECEIPTS, "data_len": 96, "must_exist": True},
                ],
            }
        ]
    )
    assert steps[0].carry == (RECEIPTS,)
    assert steps[0].funding[1].must_exist is True
    assert steps[0].funding[0].role == "fee payer"

    # A carry entry that is not an address would otherwise be dropped silently, and a
    # dropped carry entry IS the shared-pre-state bug.
    with pytest.raises(PreflightError):
        steps_from_dicts([{"name": "x", "plan": {}, "carry": [{"address": RECEIPTS}]}])
    with pytest.raises(PreflightError):
        steps_from_dicts([{"name": "", "plan": {}}])


def _code_identifiers(path: Path) -> set[str]:
    """Every identifier the module's CODE uses. Docstrings and comments are ignored —
    they legitimately say "never sendTransaction"; only executable references count."""
    tree = ast.parse(path.read_text())
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.alias):
            identifiers.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                identifiers.add(node.asname)
    return identifiers


def test_no_sign_or_broadcast_path_in_the_preflight_runner() -> None:
    # S5: the banned-token guard, extended to cover this module and its thin script.
    # "State-advancing" means simulateTransaction plus carried-forward account overrides
    # against a LOCAL fork. No keypair is loaded, nothing is sent.
    root = Path(__file__).resolve().parent.parent
    sources = [
        root / "gecko" / "fork_preflight.py",
        root / "scripts" / "fork_preflight.py",
    ]
    forbidden = {
        "sendTransaction",
        "send_transaction",
        "broadcast",
        "Keypair",
        "sign_transaction",
        "sign",
        "sign_message",
        "partial_sign",
        "new_signed",
        "secret_key",
        "from_seed",
        "X402_MODE",
    }
    for path in sources:
        leaked = forbidden & _code_identifiers(path)
        assert not leaked, f"{path.name} must not reference {sorted(leaked)} in code"


def test_the_runner_persists_nothing() -> None:
    # Invariant #1: a preflight is a control-plane read. No write seam may exist here.
    root = Path(__file__).resolve().parent.parent / "gecko" / "fork_preflight.py"
    used = _code_identifiers(root)
    assert not ({"open", "write_text", "write_bytes", "mkdir", "dump"} & used)
