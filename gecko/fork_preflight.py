"""A **state-advancing** fork preflight — chain a lifecycle without ever sending one.

WHY THIS MODULE EXISTS. :func:`gecko.simulate.simulate` is honest about a single
transaction and blind about a sequence: it never SENDS, so N consecutive calls all run
against the same pre-state. Run a four-instruction lifecycle through it and every step
after the first reports ``AccountNotInitialized`` (Anchor 3012) — for the account the
first step would have created. That reads as a derivation gap ("we derived the wrong
PDA") when it is a STATE gap ("the account is not there yet, and never will be, because
nothing advanced the chain"). Three of those on one probe is three false findings.

HOW IT IS CLOSED, WITHOUT SIGNING ANYTHING. Each step is simulated with its
carry-forward accounts in ``simulateTransaction``'s ``accounts`` config, so the RPC
returns the POST-execution state of exactly those accounts. That state is then written
into the LOCAL fork as an account override before the next step is simulated. No keypair
is loaded, no ``sendTransaction`` is issued, no signature exists, and the whole run is
$0 against a local surfpool fork. The override write is a fork cheatcode
(``surfnet_setAccount``), which is a local-validator state edit — not a broadcast.

FAIL-CLOSED, EVERYWHERE. The override seam has NO working default: a runner given no
:data:`OverlayApply` REFUSES the next step rather than silently re-running against the
shared pre-state, because a silent shared pre-state is the exact bug this module exists
to kill. A post-account the RPC returns in a shape we cannot fully account for is a
refusal, never a coerced zero. A step that fails stops the chain, and
:attr:`PreflightReport.exit_code` is non-zero — this is a gate, not the fail-open probe
it replaces.

RENT IS READ BEFORE THE FACT. Nothing else in Gecko calls
``getMinimumBalanceForRentExemption``, so underfunding only ever surfaced downstream as
an ``insufficient_funds`` revert with no number attached. :func:`check_funding` asks for
the number first and refuses with the SHORTFALL, so "you are 2,038,280 lamports short"
replaces "it reverted".

Control-plane invariant #1: nothing here is persisted. Reports are returned to the
caller; no payload, account data, or log line is written to disk by this module. All RPC
output is treated as untrusted and type-checked before it is believed.
"""

from __future__ import annotations

from base64 import b64decode
from binascii import Error as BinasciiError
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, Mapping, Sequence

from .networks import Network
from .rpc import RpcCall, RpcError, default_rpc_call, validate_rpc_url
from .simulate import BuildCall, Receipt, SimulateError, simulate

__all__ = [
    "AccountState",
    "FundingRequirement",
    "OverlayApply",
    "OverlayUnsupported",
    "PreflightError",
    "PreflightReport",
    "PreflightStep",
    "StepOutcome",
    "check_funding",
    "minimum_balance_for_rent_exemption",
    "run_preflight",
    "steps_from_dicts",
    "surfnet_overlay",
]

#: The default honesty label. A preflight is a fork snapshot advanced by overrides — it
#: is further from mainnet than a plain simulation, not closer, and says so.
DEFAULT_PREFLIGHT_LABEL = (
    "fork preflight (state advanced by local account overrides — unsigned, not mainnet)"
)

StepStatus = Literal["pass", "fail", "refused", "not_attempted"]


class PreflightError(Exception):
    """A runner-side failure that is NOT a program revert — an unreadable RPC answer, a
    build that produced nothing. Carries public metadata only; no request body is echoed."""


class OverlayUnsupported(PreflightError):
    """The transport cannot carry account state forward.

    Raised rather than degraded: without overrides every step re-runs against the same
    pre-state, which is the bug. A preflight that cannot advance state must say so.
    """


@dataclass(frozen=True)
class AccountState:
    """One account's state, as read back from a simulation's post-execution snapshot.

    ``data_base64`` is the account's data exactly as the RPC encoded it — never decoded,
    never inspected, never stored. This is a control-plane carrier: it exists only to be
    written straight back into the local fork as an override.
    """

    address: str
    lamports: int
    owner: str
    data_base64: str
    executable: bool


#: Write carried-forward account state into the local fork. There is deliberately no
#: default implementation: see :class:`OverlayUnsupported`.
OverlayApply = Callable[[Mapping[str, AccountState]], None]


@dataclass(frozen=True)
class FundingRequirement:
    """An account this step needs to already be there, and rent-solvent, before it runs.

    ``data_len`` is the size the account is EXPECTED to be — the input to
    ``getMinimumBalanceForRentExemption``. When the account already exists the larger of
    the expected and the observed length is used, so a stated size can never make a real
    account look cheaper than it is.
    """

    address: str
    data_len: int
    role: str
    must_exist: bool = False


@dataclass(frozen=True)
class PreflightStep:
    """One instruction in the lifecycle, plus the accounts whose state must advance.

    ``carry`` is the load-bearing field: those addresses are requested in the
    simulation's ``accounts`` config and their post-execution state is written into the
    fork before the next step. An account a later step depends on and this step creates
    MUST be listed, or the later step sees the pre-state.
    """

    name: str
    plan: Mapping[str, Any]
    carry: tuple[str, ...] = ()
    fee_payer: str | None = None
    funding: tuple[FundingRequirement, ...] = ()

    def without_carry(self) -> "PreflightStep":
        """The same step with nothing carried — the degraded, pre-fix behaviour.

        Exists so a test (and an operator) can reproduce the shared-pre-state bug on
        demand instead of taking the fix on trust.
        """
        return replace(self, carry=())


@dataclass(frozen=True)
class StepOutcome:
    """What one step did. ``refused`` is distinct from ``fail`` on purpose: a revert is
    an answer about the program, a refusal is Gecko declining to produce an answer it
    cannot stand behind (unfunded account, unreadable post-state, no override seam)."""

    name: str
    status: StepStatus
    receipt: Receipt | None = None
    refusal: str | None = None
    carried: tuple[str, ...] = ()

    @property
    def units_consumed(self) -> int | None:
        return self.receipt.units_consumed if self.receipt is not None else None


@dataclass(frozen=True)
class PreflightReport:
    """The whole lifecycle's outcome. ``exit_code`` is 1 unless every step passed —
    the replaced probe exited 0 whatever happened, and that is not repeated here."""

    steps: tuple[StepOutcome, ...]
    network: Network
    network_label: str = DEFAULT_PREFLIGHT_LABEL

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.status == "pass" for step in self.steps)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


def steps_from_dicts(raw: Sequence[Mapping[str, Any]]) -> tuple[PreflightStep, ...]:
    """Parse an operator-authored lifecycle description into typed steps.

    The boundary parse, so the CLI stays transport: a lifecycle is DATA (which keeps the
    runner API-agnostic — nothing about any one program lives in this module). Fails
    CLOSED on anything it cannot type: a mistyped ``carry`` entry silently dropped would
    reintroduce the shared-pre-state bug through the front door.
    """
    steps: list[PreflightStep] = []
    for index, entry in enumerate(raw):
        name = entry.get("name")
        plan = entry.get("plan")
        if not isinstance(name, str) or not name or not isinstance(plan, Mapping):
            raise PreflightError(
                f"step {index} needs a string `name` and a `plan` object"
            )
        carry = entry.get("carry") or []
        if not isinstance(carry, list) or any(not isinstance(a, str) for a in carry):
            raise PreflightError(f"step {name!r}: `carry` must be a list of addresses")
        funding: list[FundingRequirement] = []
        for item in entry.get("funding") or []:
            if not isinstance(item, Mapping):
                raise PreflightError(
                    f"step {name!r}: each `funding` entry must be an object"
                )
            address = item.get("address")
            data_len = item.get("data_len", 0)
            role = item.get("role", "account")
            if not isinstance(address, str) or not isinstance(data_len, int):
                raise PreflightError(
                    f"step {name!r}: a funding entry needs a string `address` and an "
                    "integer `data_len`"
                )
            funding.append(
                FundingRequirement(
                    address=address,
                    data_len=data_len,
                    role=str(role),
                    must_exist=bool(item.get("must_exist", False)),
                )
            )
        fee_payer = entry.get("fee_payer")
        steps.append(
            PreflightStep(
                name=name,
                plan=dict(plan),
                carry=tuple(carry),
                fee_payer=fee_payer if isinstance(fee_payer, str) else None,
                funding=tuple(funding),
            )
        )
    return tuple(steps)


# --- the rent / existence read ----------------------------------------------


def minimum_balance_for_rent_exemption(
    data_len: int, *, rpc_url: str, rpc_call: RpcCall | None = None
) -> int:
    """``getMinimumBalanceForRentExemption(data_len)`` → lamports, type-checked.

    A numeric STRING is not a lamport count and is not coerced (untrusted transport);
    neither is a bool, which ``isinstance(True, int)`` would otherwise wave through.
    """
    validate_rpc_url(rpc_url)
    call = rpc_call or default_rpc_call
    response = call(rpc_url, "getMinimumBalanceForRentExemption", [data_len])
    value = response.get("result")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PreflightError(
            "getMinimumBalanceForRentExemption returned a non-integer lamport value "
            f"for data_len={data_len} — refusing to coerce it"
        )
    return value


def _account_info(
    address: str, *, rpc_url: str, rpc_call: RpcCall | None
) -> Mapping[str, Any] | None:
    call = rpc_call or default_rpc_call
    response = call(rpc_url, "getAccountInfo", [address, {"encoding": "base64"}])
    result = response.get("result")
    value = result.get("value") if isinstance(result, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _data_length(value: Mapping[str, Any]) -> int:
    """The account's data length from a base64 ``[data, encoding]`` pair, or 0.

    Length is computed from the ENCODED string rather than decoding it: we never need
    the bytes, and not decoding is one less thing done to untrusted input.
    """
    data = value.get("data")
    if isinstance(data, list) and data and isinstance(data[0], str):
        return (len(data[0]) * 3) // 4
    return 0


def check_funding(
    requirement: FundingRequirement, *, rpc_url: str, rpc_call: RpcCall | None = None
) -> str | None:
    """``None`` if the account is present and rent-solvent, else the refusal sentence.

    This is the read that did not exist: without it an underfunded account produces an
    ``insufficient_funds`` revert AFTER the simulation, with no number and no name.
    """
    value = _account_info(requirement.address, rpc_url=rpc_url, rpc_call=rpc_call)
    if value is None:
        if requirement.must_exist:
            return (
                f"{requirement.role} account {requirement.address} does not exist on "
                "this fork — the step needs it to already be there"
            )
        lamports = 0
        data_len = requirement.data_len
    else:
        raw = value.get("lamports")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return (
                f"{requirement.role} account {requirement.address} reported a "
                "non-integer lamports value — refusing to coerce it"
            )
        lamports = raw
        data_len = max(requirement.data_len, _data_length(value))

    minimum = minimum_balance_for_rent_exemption(
        data_len, rpc_url=rpc_url, rpc_call=rpc_call
    )
    if lamports < minimum:
        return (
            f"{requirement.role} account {requirement.address} holds {lamports} lamports "
            f"but rent exemption for {data_len} bytes costs {minimum} — short by "
            f"{minimum - lamports} lamports"
        )
    return None


# --- the override seam ------------------------------------------------------


def surfnet_overlay(rpc_url: str, rpc_call: RpcCall | None = None) -> OverlayApply:
    """An :data:`OverlayApply` backed by surfpool's ``surfnet_setAccount`` cheatcode.

    A cheatcode is a LOCAL validator state edit, not a transaction: no keypair, no
    signature, no broadcast, and it exists only on the fork. An RPC that does not know
    the method raises :class:`OverlayUnsupported` — the caller must not degrade to
    "carry on without state", which is the shared-pre-state bug wearing a shrug.

    THE ONE TRANSCODE IN THE PATH, AND IT IS NOT OPTIONAL. ``simulateTransaction``
    returns account data as base64; ``surfnet_setAccount`` decodes ``AccountUpdate.data``
    as HEX. Measured against surfpool 1.1.1 — sending base64 answers
    ``code=-32602 Invalid hex data provided: Invalid character 'v' at position 1``, and
    every chained step then refuses. (Gecko's own comprehended surfpool spec at
    ``examples/surfpool/spec`` calls this field "base-58 / byte string", which is wrong
    on both counts; that spec is a docs-derived draft, and this is the measurement.)
    The encoding is therefore fixed here rather than exposed as a parameter — a knob
    whose only correct setting is one value is a way to get it wrong.
    """
    validate_rpc_url(rpc_url)
    call = rpc_call or default_rpc_call

    def apply_overlay(states: Mapping[str, AccountState]) -> None:
        for address, state in states.items():
            try:
                data_hex = b64decode(state.data_base64, validate=True).hex()
            except (BinasciiError, ValueError) as exc:
                # Writing an empty account instead would be a silent state edit to
                # something the chain never produced — worse than not advancing at all.
                raise OverlayUnsupported(
                    f"post-execution data for {address} is not decodable base64, so it "
                    f"cannot be transcoded to the hex surfnet_setAccount wants ({exc})"
                ) from exc
            update = {
                "lamports": state.lamports,
                "owner": state.owner,
                "data": data_hex,
                "executable": state.executable,
            }
            try:
                call(rpc_url, "surfnet_setAccount", [address, update])
            except RpcError as exc:
                raise OverlayUnsupported(
                    "the RPC rejected surfnet_setAccount, so state cannot be carried "
                    f"forward for {address} — refusing to re-simulate against the "
                    f"unadvanced pre-state ({exc})"
                ) from exc

    return apply_overlay


# --- reading the post-state off the one simulation we already made ----------


@dataclass
class _AccountTap:
    """Wrap an :data:`RpcCall` to capture the post-execution accounts.

    Deliberately a TAP rather than a second RPC call: ``simulate`` stays the only path
    that simulates anything (one code path, recorded and live), and this reads the
    answer it already got. Records only the addresses that were asked for and the
    account objects that came back — held in memory for the length of one step.
    """

    inner: RpcCall
    addresses: list[str] = field(default_factory=list)
    accounts: Any = None

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        response = self.inner(url, method, params)
        if method != "simulateTransaction":
            return response
        config = params[1] if len(params) > 1 and isinstance(params[1], Mapping) else {}
        requested = (config.get("accounts") or {}).get("addresses")
        self.addresses = [a for a in requested] if isinstance(requested, list) else []
        result = response.get("result")
        value = result.get("value") if isinstance(result, Mapping) else None
        self.accounts = value.get("accounts") if isinstance(value, Mapping) else None
        return response


def _carried_states(tap: _AccountTap) -> dict[str, AccountState]:
    """The tapped post-accounts as :class:`AccountState`, or a :class:`PreflightError`.

    Every field is type-checked because this is untrusted transport output that is about
    to be written back into the fork. A missing or mistyped field is a REFUSAL: carrying
    a coerced zero would advance the chain to a state that never existed, which is worse
    than not advancing it at all.
    """
    if not tap.addresses:
        return {}
    if not isinstance(tap.accounts, list) or len(tap.accounts) != len(tap.addresses):
        raise PreflightError(
            "simulateTransaction returned no post-execution accounts for the "
            f"{len(tap.addresses)} address(es) asked to carry — cannot advance state"
        )
    states: dict[str, AccountState] = {}
    for address, value in zip(tap.addresses, tap.accounts):
        if value is None:
            raise PreflightError(
                f"post-execution state for {address} came back null — the account this "
                "step was asked to carry does not exist after it ran"
            )
        if not isinstance(value, Mapping):
            raise PreflightError(f"post-execution state for {address} is not an object")
        lamports = value.get("lamports")
        if isinstance(lamports, bool) or not isinstance(lamports, int):
            raise PreflightError(
                f"post-execution lamports for {address} is not an integer — refusing to "
                "coerce it into a carried state"
            )
        owner = value.get("owner")
        data = value.get("data")
        encoded = data[0] if isinstance(data, list) and data else data
        if not isinstance(owner, str) or not isinstance(encoded, str):
            raise PreflightError(
                f"post-execution owner/data for {address} is not readable — refusing to "
                "carry a partial account"
            )
        states[address] = AccountState(
            address=address,
            lamports=lamports,
            owner=owner,
            data_base64=encoded,
            executable=bool(value.get("executable")),
        )
    return states


# --- the runner -------------------------------------------------------------


def run_preflight(
    steps: Sequence[PreflightStep],
    *,
    rpc_url: str,
    network: Network,
    rpc_call: RpcCall | None = None,
    build_call: BuildCall | None = None,
    overlay_apply: OverlayApply | None = None,
    network_label: str = DEFAULT_PREFLIGHT_LABEL,
) -> PreflightReport:
    """Simulate ``steps`` in order, advancing fork state between them.

    ``network`` is ASSERTED by the caller and never inferred — same rule as
    :func:`gecko.simulate.simulate`, for the same reason (a fork proxy answers at any
    hostname). ``rpc_call``, ``build_call`` and ``overlay_apply`` are all injectable, so
    the whole runner is falsifiable with no network at all.

    THE CHAIN STOPS at the first step that does not pass. Downstream steps are reported
    ``not_attempted`` rather than run: once the state diverged from the lifecycle, every
    later result would be about a state nobody asked for.
    """
    validate_rpc_url(rpc_url)
    call = rpc_call or default_rpc_call

    outcomes: list[StepOutcome] = []
    pending: dict[str, AccountState] = {}
    halted = False

    for step in steps:
        if halted:
            outcomes.append(StepOutcome(name=step.name, status="not_attempted"))
            continue

        # 1. Carry the previous step's state in FIRST — a step simulated before its
        #    precondition is applied is the bug, not a preflight.
        if pending:
            if overlay_apply is None:
                outcomes.append(
                    StepOutcome(
                        name=step.name,
                        status="refused",
                        refusal=(
                            f"{len(pending)} account(s) must be carried forward from the "
                            "previous step but no overlay seam was configured — refusing "
                            "to re-simulate against the unadvanced pre-state"
                        ),
                    )
                )
                halted = True
                continue
            try:
                overlay_apply(pending)
            except (OverlayUnsupported, RpcError) as exc:
                outcomes.append(
                    StepOutcome(name=step.name, status="refused", refusal=str(exc))
                )
                halted = True
                continue
            pending = {}

        # 2. Rent / existence, BEFORE simulating: a refusal with a shortfall beats an
        #    insufficient_funds revert with no number.
        refusal = None
        try:
            for requirement in step.funding:
                refusal = check_funding(requirement, rpc_url=rpc_url, rpc_call=call)
                if refusal is not None:
                    break
        except (PreflightError, RpcError) as exc:
            refusal = str(exc)
        if refusal is not None:
            outcomes.append(
                StepOutcome(name=step.name, status="refused", refusal=refusal)
            )
            halted = True
            continue

        # 3. The one simulation, through the one simulator, tapped for its post-state.
        tap = _AccountTap(inner=call)
        try:
            receipt = simulate(
                step.plan,
                rpc_url=rpc_url,
                rpc_call=tap,
                build_call=build_call,
                track=step.carry,
                network=network,
                network_label=network_label,
                replace_blockhash=True,
            )
        except (SimulateError, RpcError) as exc:
            outcomes.append(
                StepOutcome(name=step.name, status="refused", refusal=str(exc))
            )
            halted = True
            continue

        if receipt.status != "pass":
            outcomes.append(StepOutcome(name=step.name, status="fail", receipt=receipt))
            halted = True
            continue

        # 4. Read back what it did. Unreadable is a refusal about THIS step: we cannot
        #    account for what it changed, so we will not claim it passed.
        try:
            pending = _carried_states(tap)
        except PreflightError as exc:
            outcomes.append(
                StepOutcome(
                    name=step.name, status="refused", receipt=receipt, refusal=str(exc)
                )
            )
            halted = True
            continue

        outcomes.append(
            StepOutcome(
                name=step.name,
                status="pass",
                receipt=receipt,
                carried=tuple(pending),
            )
        )

    return PreflightReport(
        steps=tuple(outcomes), network=network, network_label=network_label
    )
