"""``gecko prove`` — from a sentence to a receipt, in one command.

The pieces already existed and had no front door. ``find_start`` routes an intent to a
start point but only over MCP; the landing orchestrators simulate but only from Python;
the receipt was a return value nobody could see. A person could not *type* anything and
watch the whole thing happen — which meant the product had no moment.

This is that moment, and it is three questions in order:

1. **What should I call?** — the intent is routed to a start point, ranked, with the
   reason it won.
2. **What does the call need?** — the account set, each one carrying where it came from:
   ``extracted`` from the surface, ``recovered`` from program source, ``cross_surface``
   from an API the program has never heard of, ``flagged`` when we genuinely don't know.
3. **Will it work?** — simulated on a fork before anything is signed or spent. A receipt,
   not a promise.

Every stage can answer "no" honestly. An intent below the retrieval floor is a
``no_start``, not a guess dressed as an answer; a plan with a flagged account says so; a
simulation that reverts prints the class it reverted with. The command is only useful if
it is trusted, and it is only trusted if it is willing to disappoint.

Nothing here signs or broadcasts. The simulated transaction is unsigned, on a fork, $0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .find_start import FindStartResult, StartPoint, find_start
from .rpc import LOCAL_RPC, RpcCall
from .simulate import Receipt

__all__ = ["ProofError", "ProveResult", "format_proof", "prove"]


class ProofError(Exception):
    """The intent could not be proven end to end. Never carries a secret."""


@dataclass(frozen=True)
class ProveResult:
    """What the command found, in the order it found it.

    ``receipt is None`` with ``start`` set means we knew what to call and could not run
    it — a different, and more useful, answer than not knowing what to call.
    """

    intent: str
    routing: FindStartResult
    start: StartPoint | None
    accounts: tuple[tuple[str, str], ...]  # (label, provenance)
    receipt: Receipt | None
    reason: str = ""

    @property
    def provenance_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _label, provenance in self.accounts:
            counts[provenance] = counts.get(provenance, 0) + 1
        return counts


Simulator = Callable[[StartPoint, Mapping[str, Any], str, RpcCall | None], Any]


def _default_simulator(
    start: StartPoint,
    bindings: Mapping[str, Any],
    rpc_url: str,
    rpc_call: RpcCall | None,
) -> Any:
    """Dispatch a routed start point to the orchestrator that owns it."""
    from .providers import (
        jupiter_landing,
        metadao_landing,
        meteora_landing,
        ore_landing,
        pumpfun_landing,
    )

    common: dict[str, Any] = {
        "rpc_url": rpc_url,
        "rpc_call": rpc_call,
        "include_derive_only": False,
    }
    key = (start.program.lower(), (start.instruction or "").lower())
    table = {
        ("pumpfun", "buy"): pumpfun_landing.simulate_buy_landing,
        ("pumpfun", "sell"): pumpfun_landing.simulate_sell_landing,
        ("meteora", "swap"): meteora_landing.simulate_swap_landing,
        ("ore", "claim"): ore_landing.simulate_claim_landing,
        ("metadao", "fund"): metadao_landing.simulate_fund_landing,
        ("metadao_ico", "fund"): metadao_landing.simulate_fund_landing,
        ("jupiter", "route"): jupiter_landing.simulate_route_landing,
    }
    runner: Any = table.get(key)
    if runner is None:
        raise ProofError(
            f"no orchestrator wired for {start.program}/{start.instruction}"
        )
    return runner(bindings, **common)


def _accounts_of(result: Any, start: StartPoint) -> tuple[tuple[str, str], ...]:
    """Read the provenance-tagged account set off whatever the orchestrator returned.

    Jupiter's result carries `PlannedAccount` objects (it is the flow where provenance is
    the finding); the others expose their derive plan on the start point. Both are read
    here rather than normalised upstream — the orchestrators predate this command and
    changing their return types to suit a CLI would be the tail wagging the dog.
    """
    planned = getattr(result, "accounts", None)
    if planned:
        return tuple(
            (getattr(a, "pubkey", "?")[:8] + "…", getattr(a, "provenance", "extracted"))
            for a in planned
        )
    return tuple((step.account, step.provenance) for step in start.derive_plan)


def _run_failure(exc: BaseException) -> str:
    """Why a run did not produce a receipt, without echoing a payload.

    An orchestrator's own "needs X" message names the missing bindings and is the most
    useful thing we can say, so it is surfaced verbatim when it is short and shaped like a
    requirement. Anything else degrades to the exception class — never the message, which
    could carry a value.
    """
    message = str(exc)
    if len(message) <= 120 and ("needs" in message or "missing" in message):
        return message
    return f"could not run ({type(exc).__name__})"


def prove(
    intent: str,
    *,
    bindings: Mapping[str, Any] | None = None,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
    simulator: Simulator | None = None,
    router: Callable[..., FindStartResult] | None = None,
) -> ProveResult:
    """Route ``intent``, assemble the call, simulate it, and return all three answers.

    Both the router and the simulator are injected seams, so the whole command is
    falsifiable offline with no catalog, no network and no RPC.
    """
    route = (router or find_start)(intent)

    if route.no_start or not route.starts:
        return ProveResult(
            intent=intent,
            routing=route,
            start=None,
            accounts=(),
            receipt=None,
            reason=route.note or "nothing cleared the retrieval floor",
        )

    start = route.starts[0]
    if start.kind != "start":
        # A `surface` or `guess` is a real answer to "where would I look" and NOT a
        # runnable plan. Simulating it anyway would turn an honest hedge into a fake
        # certainty, which is the one thing this command cannot afford to do.
        return ProveResult(
            intent=intent,
            routing=route,
            start=start,
            accounts=tuple((s.account, s.provenance) for s in start.derive_plan),
            receipt=None,
            reason=f"closest match is a {start.kind}, not a runnable plan",
        )

    # Deliberately NO pre-check against ``start.inputs``. The declared input list is a
    # superset of what a caller must supply — pump's ``max_sol_cost`` is quoted from the
    # bonding curve, not passed in — so gating on it would refuse a call that runs
    # perfectly. The orchestrator is the authority on its own required bindings, and its
    # error is the honest one to report.
    run = simulator or _default_simulator
    try:
        result = run(start, dict(bindings or {}), rpc_url, rpc_call)
    except Exception as exc:  # noqa: BLE001 - redact: the class, never the payload
        return ProveResult(
            intent=intent,
            routing=route,
            start=start,
            accounts=tuple((s.account, s.provenance) for s in start.derive_plan),
            receipt=None,
            reason=_run_failure(exc),
        )

    return ProveResult(
        intent=intent,
        routing=route,
        start=start,
        accounts=_accounts_of(result, start),
        receipt=getattr(result, "landing_receipt", None),
    )


def format_proof(result: ProveResult, *, show_accounts: int = 0) -> Iterable[str]:
    """The three answers as human lines. All rendering lives here, none in the CLI."""
    yield f'intent  "{result.intent}"'
    yield ""

    if result.start is None:
        yield "  no start — " + (result.reason or "nothing matched")
        for candidate in result.routing.catalog[:3]:
            yield f"    catalog: {candidate.name} (unwired — comprehend it first)"
        return

    start = result.start
    considered = result.routing.starts

    # Show the FIELD, not just the winner. "It picked route" is a claim; "it picked
    # route over these six, and here is the score and the reason for each" is the
    # mechanism. A reader who cannot see what was rejected cannot tell selection from
    # luck — and the runners-up are also where a wrong answer would be visible.
    if len(considered) > 1:
        yield f"  considered {len(considered)} candidates across the wired programs:"
        for point in considered[:6]:
            mark = "→" if point is start else " "
            name = f"{point.program}/{point.instruction or '—'}"
            tag = "" if point.kind == "start" else f"  ({point.kind})"
            terms = ", ".join(point.why[:3])
            yield f"    {mark} {name:<24} {point.score:>2}  {terms}{tag}"
        yield ""

    yield f"  → {start.program}/{start.instruction or '—'}   score {start.score}"
    for why in start.why[:2]:
        yield f'    matched on "{why}"'
    yield ""

    counts = result.provenance_counts
    if counts:
        total = sum(counts.values())
        yield f"  the call needs {total} accounts:"
        for provenance in ("extracted", "recovered", "cross_surface", "flagged"):
            if counts.get(provenance):
                label = provenance.replace("_", "-")
                yield f"    {counts[provenance]:>3}  {label}"
        if show_accounts:
            for label, provenance in result.accounts[:show_accounts]:
                yield f"      {label:<24} [{provenance}]"
        yield ""

    if start.gaps:
        for gap in start.gaps:
            yield f"  flagged: {gap.name} — {gap.note}"
        yield ""

    if result.receipt is None:
        yield f"  no receipt — {result.reason}"
        return

    receipt = result.receipt
    if receipt.status == "pass":
        yield f"  PASS   {receipt.units_consumed:,} compute units"
    else:
        yield f"  FAIL   {receipt.revert_class}"
    yield f"  {receipt.network_label} · unsigned · $0"
