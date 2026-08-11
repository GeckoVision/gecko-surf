"""Opt-in corpus recording for the landing orchestrators — the D2 wiring.

The gap this closes (Solana architecture review): ``simulated_outcome_from`` /
``record_simulated`` / ``recipe_hash`` existed but were called only from their own
tests — the "outcome → corpus → drift" loop the architecture labelled *pending* was
never connected to :func:`gecko.providers.pumpfun_landing.simulate_buy_landing` or
:func:`gecko.providers.meteora_landing.simulate_swap_landing`. This module is the ONE
shared bridge both orchestrators (and the Path-A ``simulate`` tool) call when — and
only when — the caller explicitly opts in with ``record_to``. Persistence is never
automatic (invariant #1 posture: the default run stores nothing, byte-identical to
before).

What crosses the bridge is CATEGORICAL only: the Receipt's status / revert family +
public code / compute units, a best-effort public slot, the closed network category,
and a ``recipe_hash`` built from the plan's STRUCTURAL fingerprint — account NAMES,
arg NAMES, seed-KIND tokens projected mechanically from the packaged PDA graph
(:func:`gecko.corpus.seed_recipes_of`). Never a resolved pubkey, amount, log line, or
RPC URL; the ``recipe_hash`` input guard rejects value-shaped input (fail closed).

Failure posture mirrors ``gecko.corpus.record``: a control-plane violation
(:class:`~gecko.corpus.CorpusError`) SURFACES — a bad kind must break the build, not
be swallowed — while any other recording failure never breaks the simulate call.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast

from ..corpus import (
    NETWORKS,
    CorpusError,
    network_category,
    recipe_hash,
    record_simulated,
    seed_recipes_of,
    simulated_outcome_from,
)
from ..networks import UNKNOWN_NETWORK, Network
from ..pda import PdaNode
from ..rpc import RpcCall, default_rpc_call
from ..simulate import Receipt

__all__ = ["record_landing_outcome"]


def _best_effort_slot(rpc_url: str, rpc_call: RpcCall | None) -> int | None:
    """The slot the sim ran against — a public integer that anchors the drift SERIES.

    Best-effort by design: an offline/injected transport that doesn't answer
    ``getSlot`` must not lose the row (the row is still valid with ``slot=None``; it
    just cannot anchor ordering — see ``gecko.drift._slot_key``)."""
    call = rpc_call or default_rpc_call
    try:
        result = call(rpc_url, "getSlot", []).get("result")
    except Exception:  # noqa: BLE001 — slot is an enrichment, never a gate
        return None
    return result if isinstance(result, int) else None


def _network_of(receipt: Receipt, network_label: str | None) -> Network:
    """The network to RECORD: the Receipt's asserted field, else the prose collapse.

    Order matters and only in this direction. A Receipt that ASSERTED its network states
    a fact; the label is one operator's sentence about it, and an operator who mislabels a
    devnet run would otherwise poison the ledger with ``fork``. The prose fallback exists
    for receipts that asserted nothing — older rows, third-party Receipt-shaped objects,
    and every provider orchestrator, which is handed an injected RPC and told nothing
    about it.

    This is the CORPUS, so reading prose is allowed: a categorical row one bucket off is a
    slightly-wrong observation. The signing gate does the opposite and reads the field
    only — see :func:`gecko.txbind.evaluate_tx`, which never imports the collapse.
    """
    asserted = getattr(receipt, "network", None)
    if (
        isinstance(asserted, str)
        and asserted in NETWORKS
        and asserted != UNKNOWN_NETWORK
    ):
        return cast("Network", asserted)
    return network_category(network_label)


def record_landing_outcome(
    receipt: Receipt,
    *,
    program_id: str,
    instruction: str,
    account_names: Iterable[str],
    arg_names: Iterable[str],
    pdas: Mapping[str, PdaNode],
    network_label: str | None,
    rpc_url: str,
    rpc_call: RpcCall | None,
    record_to: str | Path,
) -> None:
    """Append ONE categorical ``SimulatedOutcome`` row for a landing-orchestrator run
    to ``record_to``'s segregated ``simulated.jsonl`` sibling.

    Reads the Receipt's categorical fields only (``simulated_outcome_from`` never
    touches ``sol_delta``/``tokens_received``/``logs_tail``/``err``) and fingerprints
    the plan by NAMES + seed KINDS, so two runs of the same intent with different
    resolved pubkeys/amounts share one ``recipe_hash`` — the drift key.

    Raises :class:`CorpusError` on a control-plane violation (fail closed); swallows
    everything else with a redacted note (recording must never break the simulate).
    """
    try:
        slot = _best_effort_slot(rpc_url, rpc_call)
        fingerprint = recipe_hash(
            program_id=program_id,
            instruction=instruction,
            account_names=list(account_names),
            arg_names=list(arg_names),
            seed_recipes=seed_recipes_of(pdas),
        )
        outcome = simulated_outcome_from(
            receipt,
            program_id=program_id,
            instruction=instruction,
            recipe_hash=fingerprint,
            slot=slot,
            network=_network_of(receipt, network_label),
            ts=int(time.time() * 1000),
            surface_id=f"orquestra:{program_id}",
        )
        record_simulated(outcome, record_to)
    except CorpusError:
        raise  # a control-plane violation must surface, not be swallowed
    except Exception:  # noqa: BLE001 — best-effort; never break the simulate call
        import logging

        logging.getLogger("gecko.corpus").warning(
            "landing corpus record failed (redacted)"
        )
