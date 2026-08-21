"""The recorded lane — the $0 first green, and the attributed simulate feed.

Stage 3 of the hosted flow (architecture §0, §4). Given a prepared purchase (the
``prepare_purchase`` comprehension: translate → derive accounts → SIMULATE the
exact bytes, sigVerify off), this records the outcome as a payload-free
:class:`~gecko.corpus.SimulatedOutcome`, attributed to an API key. No signature,
no broadcast, no worker — a read-only simulation against a node, which is why it
is the instant activation green.

What this lane OWNS: comprehension + derivation + simulate + attributed
recording. What it does NOT own: the semantic *reasoning* (refuse-to-guess,
cardinality) — that is the agent's, produced when it consumes the surface, and
surfaced by the playground BFF; the engine's job is to hand the agent an honest
surface and a checkable receipt.

Pattern B: ``prepare`` is injected, so the whole lane is falsifiable offline with
a fake receipt and no RPC. In production it is wired to ``prepare_purchase``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gecko.corpus import SimulatedOutcome, simulated_outcome_from
from gecko.simulate import Receipt
from gecko.store.collections import Collection
from gecko.store.projections import record_simulated_outcome


@dataclass(frozen=True)
class PreparedSim:
    """What a prepare step hands the recorded lane — a simulate result, values-free.

    ``recipe_hash`` is the STRUCTURAL fingerprint of the derivation (values-free);
    ``slot`` powers the drift series; ``network`` is a closed label, never a URL.
    """

    receipt: Receipt
    recipe_hash: str
    slot: int | None
    network: str


PrepareFn = Callable[[], PreparedSim]


@dataclass(frozen=True)
class RecordedRun:
    """The gradable result of one recorded run — a receipt summary, recorded once.

    ``first_call_correct`` here is the recorded green: the exact bytes the surface
    derived SIMULATED clean on the first try. It is reported to the caller, and
    the SimulatedOutcome it came from is what the score's simulate/land rate reads
    (at the ``simulated`` evidence tier — a fork/recorded pass is strictly weaker
    than a mainnet-observed one, and the score carries that tier).
    """

    surface_id: str
    instruction: str
    status: str  # "pass" | "fail" | "unknown"
    units_consumed: int | None
    revert_class: str
    network: str
    first_call_correct: bool
    recorded: bool


def run_recorded(
    prepare: PrepareFn,
    outcomes: Collection,
    *,
    surface_id: str,
    program_id: str,
    instruction: str,
    api_key_id: str,
    run_id: str,
    ts: int,
) -> RecordedRun:
    """Run one recorded purchase: simulate, then record the outcome attributed.

    ``api_key_id`` must be a key HASH (the writer refuses a plaintext). The
    recorded row is ``source="simulated"`` (fixed on the outcome), so it feeds the
    simulate/land rate and never the observed FCC rate — a playground green can
    never inflate a provider's published number.
    """
    prepared = prepare()
    outcome: SimulatedOutcome = simulated_outcome_from(
        prepared.receipt,
        program_id=program_id,
        instruction=instruction,
        recipe_hash=prepared.recipe_hash,
        slot=prepared.slot,
        network=prepared.network,
        ts=ts,
        surface_id=surface_id,
    )
    record_simulated_outcome(outcomes, outcome, run_id=run_id, api_key_id=api_key_id)
    return RecordedRun(
        surface_id=surface_id,
        instruction=instruction,
        status=outcome.status,
        units_consumed=outcome.units_consumed,
        revert_class=outcome.revert_class,
        network=outcome.network,
        first_call_correct=outcome.status == "pass",
        recorded=True,
    )
