"""One score per endpoint, and it answers two different people's questions.

An agent asks *which of these should I call?* A provider asks *which of mine need work?*
Those are the same measurement read in opposite directions, so it is taken once.

THREE COMPONENTS, and each is offline, deterministic, and derived from artifacts we
already hold. No model, no network, no learned float:

* **callable** — can we derive every account this endpoint needs, and did a human
  establish the parts the IDL cannot state? Sourced from the packaged PDA recipes
  (:mod:`gecko.pda`), the intent's declared gaps, and ``pda_origins``.
* **findable** — handed this endpoint's own words, does the catalog return it, first?
  The same probe :func:`gecko.ingest_gate.check_discrimination` runs. That one REFUSES on
  the answer because it is a gate; this one GRADES it, because a provider cannot act on a
  refusal that names no degree.
* **distinct** — is it separable from its own siblings? `swap` beside `swap2` beside
  `swap_with_destination` is the case that motivated this: six instructions of one
  program whose names differ by a suffix and whose descriptions differ by a clause.

WHAT THIS IS NOT. It is not a percentage and it does not sum. Three named verdicts stay
three named verdicts, because "0.72" tells a provider nothing they can fix and a weighted
blend would let a strong component hide a blocked one. The worst component IS the score;
the others say what else is true.

WHAT IT WILL NOT CLAIM. `callable` means *we can derive the accounts*, never *this call
will succeed* — the chain decides that, and a simulation is a different, slower question
this module deliberately does not ask. An endpoint we cannot rank is ``unknown``, never
``weak``: not knowing is not a middling result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

__all__ = [
    "Component",
    "EndpointScore",
    "Verdict",
    "score_endpoints",
]

#: Ordered worst-first. `unknown` sits apart: it is the absence of a measurement, and
#: ordering it beside the measured ones would let "we did not look" average away.
Verdict = Literal["blocked", "weak", "ok", "unknown"]

_RANK: dict[str, int] = {"blocked": 0, "weak": 1, "ok": 2, "unknown": 3}

#: Origins where a human established what the IDL cannot state — the byte order of an
#: integer seed, a root nobody's IDL names. Mirrors `ingest_gate._ENDIANNESS_ESTABLISHED`.
_ESTABLISHED = frozenset({"manual", "recovered"})


@dataclass(frozen=True)
class Component:
    """One named question, its verdict, and the numbers behind it."""

    name: str
    verdict: Verdict
    reason: str
    measured: Mapping[str, Any]


@dataclass(frozen=True)
class EndpointScore:
    """Every component for one endpoint. The worst component is the score."""

    api_id: str
    instruction: str | None
    components: tuple[Component, ...]

    @property
    def label(self) -> str:
        return f"{self.api_id}.{self.instruction}" if self.instruction else self.api_id

    @property
    def verdict(self) -> Verdict:
        """The WORST component, never a blend. A blocked endpoint that ranks first is
        still blocked, and an average would report it as fine."""
        measured = [c.verdict for c in self.components if c.verdict != "unknown"]
        if not measured:
            return "unknown"
        return min(measured, key=lambda v: _RANK[v])  # type: ignore[arg-type]

    @property
    def rankable(self) -> bool:
        """May an agent offer this endpoint as a choice? Only if we can derive it."""
        return self.component("callable").verdict == "ok"

    def component(self, name: str) -> Component:
        for c in self.components:
            if c.name == name:
                return c
        return Component(name, "unknown", "not measured", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.label,
            "verdict": self.verdict,
            "components": {
                c.name: {
                    "verdict": c.verdict,
                    "reason": c.reason,
                    **({"measured": dict(c.measured)} if c.measured else {}),
                }
                for c in self.components
            },
        }


def _callable_component(card: Any, origins: Mapping[str, str]) -> Component:
    """Can we derive every account, and was the underivable part established by a human?"""
    all_pdas = dict(getattr(card, "pdas", {}) or {})
    spec = getattr(card, "spec", None)
    gaps = tuple(getattr(spec, "gaps", ()) or ())

    # THE DENOMINATOR IS THE INTENT'S OWN ACCOUNTS, not the program's.
    #
    # A card carries every PDA its PROGRAM declares. Scoring an instruction against all of
    # them reads `whirlpool.swap_v2` as blocked on `bundled_position` and
    # `reward_token_badge` — accounts belonging to position bundling and reward
    # distribution, which a swap never touches. That endpoint landed real mainnet swaps on
    # 2026-09-08; a scorecard calling it broken would be measuring the wrong set.
    #
    # `StartSpec.accounts` is the intent's own affirmative claim about what its plan
    # derives, so it is the honest denominator. A SURFACE card claims the whole program,
    # so it keeps the whole set — but is graded `weak` rather than `blocked`, because an
    # underivable account somewhere in a program is a gap in coverage, not a broken call.
    claimed = tuple(getattr(spec, "accounts", ()) or ()) if card.instruction else ()
    pdas = (
        {n: node for n, node in all_pdas.items() if n in set(claimed)}
        if claimed
        else all_pdas
    )
    is_surface = card.instruction is None

    unresolvable = sorted(n for n, node in pdas.items() if not node.resolvable)
    assumed = sorted(
        name
        for name, node in pdas.items()
        if node.resolvable
        and str(origins.get(name, "extracted")) not in _ESTABLISHED
        and any(getattr(s, "encoding", None) in ("le", "be") for s in node.seeds)
    )
    measured = {
        "accounts": len(pdas),
        "claimed_by_intent": len(claimed),
        "unresolvable": tuple(unresolvable),
        "assumed_byte_order": tuple(assumed),
        "declared_gaps": len(gaps),
    }
    if not pdas:
        return Component(
            "callable",
            "unknown",
            "this card declares no PDA recipes, so there is nothing to derive and "
            "nothing to judge — absence of accounts is not evidence of callability",
            measured,
        )
    if unresolvable and is_surface:
        return Component(
            "callable",
            "weak",
            f"{len(unresolvable)} of {len(pdas)} account(s) this program declares carry "
            f"a seed nothing can bind ({', '.join(unresolvable)}). That is a gap in the "
            "program's coverage, not a broken call — an instruction that needs none of "
            "them is unaffected.",
            measured,
        )
    if unresolvable:
        return Component(
            "callable",
            "blocked",
            f"{len(unresolvable)} account(s) THIS instruction declares carry a seed "
            f"nothing can bind: {', '.join(unresolvable)}. A human must recover the "
            "recipe before an agent can call it at all.",
            measured,
        )
    if assumed or gaps:
        parts = []
        if assumed:
            parts.append(
                f"{len(assumed)} integer seed(s) took a byte order the IDL cannot "
                f"state ({', '.join(assumed)})"
            )
        if gaps:
            parts.append(f"{len(gaps)} declared gap(s)")
        return Component(
            "callable",
            "weak",
            "derivable, but on an assumption: " + "; and ".join(parts) + ". "
            "Reproduce one live address before trusting it.",
            measured,
        )
    return Component(
        "callable",
        "ok",
        f"all {len(pdas)} account(s) derive from established recipes",
        measured,
    )


def _rank_own_text(cards: Sequence[Any]) -> dict[int, tuple[int | None, int, int]]:
    """For each card, probe the WHOLE catalog with that card's own text.

    Returns ``{id(card): (rank, own_score, best_other_score)}``. This is the measurement
    :func:`gecko.ingest_gate.check_discrimination` takes to decide whether to refuse an
    ingest; here the same numbers are kept as degrees instead of collapsed to a verdict.
    """
    from .catalog import Catalog

    out: dict[int, tuple[int | None, int, int]] = {}
    entries = [c.operation for c in cards]
    catalog = Catalog(entries)
    for card in cards:
        op = card.operation
        probe = f"{op.summary} {op.description}"
        scored = catalog.search_scored(probe, limit=len(entries))
        ranked = [(s.entry.operation.operation_id, s.score) for s in scored]
        position = next(
            (i for i, (name, _s) in enumerate(ranked) if name == op.operation_id), None
        )
        own = ranked[position][1] if position is not None else 0
        others = [s for name, s in ranked if name != op.operation_id]
        out[id(card)] = (
            None if position is None else position + 1,
            own,
            max(others) if others else 0,
        )
    return out


def _findable_component(card: Any, probe: tuple[int | None, int, int]) -> Component:
    rank, own, best_other = probe
    measured = {"rank": rank, "own_score": own, "best_other": best_other}
    if rank is None:
        return Component(
            "findable",
            "blocked",
            "handed this endpoint's own description, the catalog does not return it at "
            "all — a card its own words cannot find is a card no intent can find",
            measured,
        )
    margin = own - best_other
    measured["margin"] = margin
    if rank > 1:
        return Component(
            "findable",
            "weak",
            f"its own description ranks it {rank}, behind another card scoring "
            f"{best_other}. An agent handed this text is routed elsewhere.",
            measured,
        )
    if margin <= 0:
        return Component(
            "findable",
            "weak",
            f"first by the sort, not by the evidence — margin {margin} over the next "
            "card. A tie is decided by ordering, which is not a reason.",
            measured,
        )
    return Component("findable", "ok", f"first on its own words, by {margin}", measured)


def _distinct_component(card: Any, cards: Sequence[Any], probes: Any) -> Component:
    """Separable from its OWN siblings — the six-`swap`-variants case."""
    from .catalog import Catalog

    siblings = [
        c
        for c in cards
        if c.api_id == card.api_id
        and c.operation.operation_id != card.operation.operation_id
    ]
    measured: dict[str, Any] = {"siblings": len(siblings)}
    if not siblings:
        return Component(
            "distinct",
            "unknown",
            "the only card its program wires — nothing to be confused with, which is "
            "not the same as being distinct",
            measured,
        )
    op = card.operation
    catalog = Catalog([c.operation for c in [card, *siblings]])
    scored = catalog.search_scored(
        f"{op.summary} {op.description}", limit=len(siblings) + 1
    )
    ranked = [(s.entry.operation.operation_id, s.score) for s in scored]
    own = next((s for n, s in ranked if n == op.operation_id), 0)
    rival_name, rival = next(
        ((n, s) for n, s in ranked if n != op.operation_id), (None, 0)
    )
    margin = own - rival
    measured.update({"own_score": own, "nearest_sibling": rival_name, "margin": margin})
    if margin <= 0:
        return Component(
            "distinct",
            "weak",
            f"its own words do not separate it from {rival_name} (margin {margin}). "
            "Two instructions of one program that read the same are chosen by ordering.",
            measured,
        )
    return Component(
        "distinct",
        "ok",
        f"clear of its nearest sibling {rival_name} by {margin}",
        measured,
    )


def score_endpoints(
    cards: Sequence[Any] | None = None,
    *,
    origins: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[EndpointScore, ...]:
    """Score every wired endpoint. Offline and deterministic — no RPC, no model.

    ``cards`` defaults to the live wired catalog; ``origins`` maps ``api_id ->
    {account: origin}`` and defaults to the packaged configs.
    """
    from .find_start import _wired_cards

    cards = list(cards if cards is not None else _wired_cards())
    origins = dict(origins) if origins is not None else _packaged_origins()
    probes = _rank_own_text(cards)

    out: list[EndpointScore] = []
    for card in cards:
        out.append(
            EndpointScore(
                api_id=card.api_id,
                instruction=card.instruction,
                components=(
                    _callable_component(card, origins.get(card.api_id, {})),
                    _findable_component(card, probes[id(card)]),
                    _distinct_component(card, cards, probes),
                ),
            )
        )
    return tuple(out)


def _packaged_origins() -> dict[str, dict[str, str]]:
    """``api_id -> {account: origin}`` from the packaged configs. Empty when unreadable:
    a missing origins map must read as `extracted` (the weakest claim), never as
    established."""
    import json
    from pathlib import Path

    root = Path(__file__).parent / "providers" / "configs" / "orquestra"
    out: dict[str, dict[str, str]] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.json")):
        if path.name == "provider.json":
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable config is not established
            continue
        program = doc.get("program")
        if isinstance(program, Mapping):
            origins = program.get("pda_origins")
            if isinstance(origins, Mapping):
                out[path.stem] = {str(k): str(v) for k, v in origins.items()}
    return out
