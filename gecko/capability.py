"""The Capability Card: what a contributor writes, and the check that admits it.

A card is ONE file about ONE surface, and it carries only the two things a machine cannot
supply (``gecko/project/probes.py`` says so in its own docstring):

* ``[bindings]`` — the values a caller genuinely has to choose: their wallet, their store,
  their product, the numbers.
* ``[intents]`` — the sentence a person would actually say. *"buy a bottle of water at the
  bar"* is not in an IDL, and no derivation recovers it.

Everything else — accounts, PDA derivation, argument types, the fee payer — is derived by
code that already exists, so a contributor cannot get the derived parts wrong: they do not
write them. That is what makes a card an hour of work rather than a week.

**A raised refusal is not a failure. An UNDECLARED refusal is.** A card that derives 6 of 9
instructions and names the other 3 by their refusal class is a good card; demanding
completeness would admit almost nothing (6% of PDA accounts still need a human). What the
check will not tolerate is a card describing a surface it no longer matches — in either
direction: an undeclared refusal, or a declared gap that no longer fires.

Nothing here signs, sends, or reaches the network. The check refuses to run at all when a
live-signing environment is set, and it reads the chain never.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "CapabilityCard",
    "CapabilityCardError",
    "CapabilityReport",
    "IntentRouting",
    "InstructionOutcome",
    "REFUSAL_CLASSES",
    "check_card",
    "load_card",
]

#: The refusal classes ``probes.py`` raises, by name. A card declares a gap with one of
#: these and nothing else: a free-text reason would make the declaration unfalsifiable.
REFUSAL_CLASSES: frozenset[str] = frozenset(
    {"MissingProbeBindingError", "UnderivableAccountError", "UnprobableArgTypeError"}
)

#: Environment that means a key is reachable. The check refuses rather than run beside one.
_SIGNING_ENV: tuple[str, ...] = (
    "KORA_RELAY_KEY",
    "PAYBOX_SIGNIN_KEY",
    "PAYBOX_SIGNING_KEY",
    "PRIVY_APP_SECRET",
    "SOLANA_PRIVATE_KEY",
)

Tier = Literal["community", "verified"]
Kind = Literal["solana-program", "openapi"]


class CapabilityCardError(Exception):
    """The card cannot be read, or says something a card may not say."""


@dataclass(frozen=True)
class CapabilityCard:
    """One surface, as its contributor describes it. Paths resolve against the card's own
    directory, so a card plus its IDL is a self-contained folder a student can hand over."""

    slug: str
    kind: Kind
    source: str
    owner: str
    tier: Tier
    bindings: Mapping[str, str]
    intents: Mapping[str, str]
    paraphrases: tuple[tuple[str, str], ...]
    declared_gaps: tuple[tuple[str, str, str], ...]
    idl_path: Path | None = None
    path: Path | None = None

    @property
    def declared_by_instruction(self) -> dict[str, str]:
        return {
            instruction: refusal for instruction, refusal, _note in self.declared_gaps
        }


@dataclass(frozen=True)
class InstructionOutcome:
    """One instruction: derived, or refused by the class the engine named."""

    instruction: str
    derived: bool
    refusal: str | None = None
    declared: bool = False

    @property
    def undeclared(self) -> bool:
        return self.refusal is not None and not self.declared


@dataclass(frozen=True)
class IntentRouting:
    """One sentence, and where the router sent it. ``kind`` is find_start's own word."""

    intent: str
    expects: str
    routed: bool
    program: str | None = None
    instruction: str | None = None
    kind: str | None = None
    paraphrase: bool = False


@dataclass(frozen=True)
class CapabilityReport:
    """What a run of the check found. The number a contributor puts on their portfolio,
    and the evidence we keep — produced by a run, never asserted by the author."""

    slug: str
    owner: str
    tier: Tier
    outcomes: tuple[InstructionOutcome, ...]
    routings: tuple[IntentRouting, ...]
    phantom_gaps: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def derived(self) -> int:
        return sum(1 for one in self.outcomes if one.derived)

    @property
    def declared_gaps(self) -> int:
        return sum(
            1 for one in self.outcomes if one.refusal is not None and one.declared
        )

    @property
    def undeclared(self) -> tuple[InstructionOutcome, ...]:
        return tuple(one for one in self.outcomes if one.undeclared)

    @property
    def routed(self) -> int:
        return sum(1 for one in self.routings if one.routed)

    @property
    def admitted(self) -> bool:
        """Admitted means: the graph built, every refusal is declared, every declared gap
        fired, and nothing errored. Routing is REPORTED, never a rejection: a sentence the
        ranker misses is a demand signal about our ranker, not a fault in the card."""
        return not self.errors and not self.undeclared and not self.phantom_gaps

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "owner": self.owner,
            "tier": self.tier,
            "admitted": self.admitted,
            "instructions": {
                "total": len(self.outcomes),
                "derived": self.derived,
                "declared_gaps": self.declared_gaps,
                "undeclared": [one.instruction for one in self.undeclared],
            },
            "intents": {
                "total": len(self.routings),
                "routed": self.routed,
                "unrouted": [one.intent for one in self.routings if not one.routed],
            },
            "phantom_gaps": list(self.phantom_gaps),
            "errors": list(self.errors),
            "notes": list(self.notes),
        }

    def to_markdown(self) -> str:
        lines = [
            f"# {self.slug} — capability card report",
            "",
            f"**{'admitted' if self.admitted else 'not admitted'}** · owner {self.owner} · tier `{self.tier}`",
            "",
            f"- {self.derived} of {len(self.outcomes)} instructions derive",
            f"- {self.declared_gaps} declared gap(s), {len(self.undeclared)} undeclared",
            f"- {self.routed} of {len(self.routings)} sentences route to this surface",
        ]
        if self.undeclared:
            lines += ["", "## Undeclared refusals (these are what keep the card out)"]
            lines += [
                f"- `{one.instruction}` raised `{one.refusal}` and the card does not declare it"
                for one in self.undeclared
            ]
        if self.phantom_gaps:
            lines += ["", "## Declared gaps that did not fire"]
            lines += [f"- `{name}`" for name in self.phantom_gaps]
        unrouted = [one for one in self.routings if not one.routed]
        if unrouted:
            lines += [
                "",
                "## Sentences the router missed (our backlog, not your fault)",
            ]
            lines += [
                f"- {'paraphrase: ' if one.paraphrase else ''}{one.intent!r} → expected `{one.expects}`"
                for one in unrouted
            ]
        if self.errors:
            lines += ["", "## Errors"] + [f"- {message}" for message in self.errors]
        return "\n".join(lines) + "\n"


def load_card(path: str | Path) -> CapabilityCard:
    """Read and validate one card. Every refusal here names the field it is about."""
    card_path = Path(path)
    try:
        raw = tomllib.loads(card_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CapabilityCardError(f"the card could not be read: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise CapabilityCardError(f"the card is not valid TOML: {exc}") from None

    surface = raw.get("surface")
    if not isinstance(surface, dict):
        raise CapabilityCardError("the card has no [surface] table")
    slug = _text(surface, "slug")
    kind = _text(surface, "kind")
    if kind not in ("solana-program", "openapi"):
        raise CapabilityCardError(
            f"surface.kind {kind!r} is not solana-program or openapi"
        )
    owner = _text(surface, "owner")
    if not owner.startswith("@"):
        raise CapabilityCardError("surface.owner is a @handle — a card names a human")
    tier = surface.get("tier", "community")
    if tier != "community":
        # Terraform's tiering, and the one rule that makes it mean anything: the label a
        # contributor may write is the lowest one. `verified` is granted by a human who
        # read the intents, never claimed by the file that wants it.
        raise CapabilityCardError(
            f"surface.tier is {tier!r}; a card is submitted as 'community' and promoted by review"
        )

    intents = raw.get("intents") or {}
    if not isinstance(intents, dict) or not intents:
        raise CapabilityCardError(
            "[intents] is empty — the sentence IS the contribution"
        )
    for instruction, sentence in intents.items():
        if not isinstance(sentence, str) or not sentence.strip():
            raise CapabilityCardError(f"intents.{instruction} is empty")

    bindings = raw.get("bindings") or {}
    if not isinstance(bindings, dict):
        raise CapabilityCardError("[bindings] must be a table")

    paraphrases: list[tuple[str, str]] = []
    for entry in raw.get("paraphrase") or []:
        if not isinstance(entry, dict):
            raise CapabilityCardError("a [[paraphrase]] entry must be a table")
        sentence, expects = _text(entry, "intent"), _text(entry, "expects")
        if expects not in intents:
            raise CapabilityCardError(
                f"paraphrase expects {expects!r}, which is not in [intents]"
            )
        shared = _tokens(sentence) & _tokens(expects)
        if shared:
            raise CapabilityCardError(
                f"the paraphrase for {expects!r} shares {sorted(shared)} with the instruction "
                "name; a paraphrase that echoes the name measures nothing"
            )
        paraphrases.append((sentence, expects))
    if not paraphrases:
        raise CapabilityCardError(
            "a card carries at least one [[paraphrase]] — it is the only part of the "
            "routing that cannot pass by keyword echo"
        )

    gaps: list[tuple[str, str, str]] = []
    for entry in raw.get("declared_gap") or []:
        if not isinstance(entry, dict):
            raise CapabilityCardError("a [[declared_gap]] entry must be a table")
        instruction, refusal = _text(entry, "instruction"), _text(entry, "refusal")
        if refusal not in REFUSAL_CLASSES:
            raise CapabilityCardError(
                f"declared_gap.refusal {refusal!r} is not one of {sorted(REFUSAL_CLASSES)}"
            )
        gaps.append((instruction, refusal, str(entry.get("note", ""))))

    idl = surface.get("idl")
    return CapabilityCard(
        slug=slug,
        kind=kind,  # type: ignore[arg-type]
        source=_text(surface, "source"),
        owner=owner,
        tier="community",
        bindings={str(k): str(v) for k, v in bindings.items()},
        intents={str(k): str(v) for k, v in intents.items()},
        paraphrases=tuple(paraphrases),
        declared_gaps=tuple(gaps),
        idl_path=(card_path.parent / str(idl)).resolve() if idl else None,
        path=card_path,
    )


def check_card(
    card: CapabilityCard, *, environ: Mapping[str, str] | None = None
) -> CapabilityReport:
    """Run the four gates over one card. Offline, $0, and it never signs."""
    env = os.environ if environ is None else environ
    live = [name for name in _SIGNING_ENV if str(env.get(name, "")).strip()]
    if live:
        raise CapabilityCardError(
            f"{', '.join(live)} is set; this check runs beside no key. Unset it and run again"
        )

    errors: list[str] = []
    notes: list[str] = []
    outcomes: list[InstructionOutcome] = []

    graph = None
    if card.kind == "solana-program":
        try:
            graph = _graph_of(card)
        except CapabilityCardError as exc:
            errors.append(str(exc))
    else:
        notes.append(
            "openapi cards are described, not probed, until the ingest gate runs here"
        )

    if graph is not None:
        declared = card.declared_by_instruction
        from .project.probes import (  # noqa: PLC0415 - kept off the import path's hot edge
            MissingProbeBindingError,
            UnderivableAccountError,
            UnprobableArgTypeError,
            probe_case,
        )

        refusals = (
            MissingProbeBindingError,
            UnderivableAccountError,
            UnprobableArgTypeError,
        )
        for instruction in [one.name for one in graph.instructions]:
            try:
                probe_case(graph, instruction, bindings=card.bindings)
            except refusals as refusal:
                name = type(refusal).__name__
                outcomes.append(
                    InstructionOutcome(
                        instruction=instruction,
                        derived=False,
                        refusal=name,
                        declared=declared.get(instruction) == name,
                    )
                )
            else:
                outcomes.append(
                    InstructionOutcome(instruction=instruction, derived=True)
                )

        fired = {one.instruction for one in outcomes if one.refusal is not None}
        phantom = tuple(sorted(name for name in declared if name not in fired))
    else:
        phantom = ()

    routings = _route(card)
    return CapabilityReport(
        slug=card.slug,
        owner=card.owner,
        tier=card.tier,
        outcomes=tuple(outcomes),
        routings=routings,
        phantom_gaps=phantom,
        errors=tuple(errors),
        notes=tuple(notes),
    )


def _route(card: CapabilityCard) -> tuple[IntentRouting, ...]:
    """Gate 3: does the router send each sentence to this surface? Offline, no catalog."""
    from .find_start import find_start  # noqa: PLC0415

    asked: list[tuple[str, str, bool]] = [
        (sentence, instruction, False) for instruction, sentence in card.intents.items()
    ]
    asked += [(sentence, expects, True) for sentence, expects in card.paraphrases]

    out: list[IntentRouting] = []
    for sentence, expects, is_paraphrase in asked:
        result = find_start(sentence)
        top = result.starts[0] if result.starts else None
        routed = bool(
            not result.no_start
            and top is not None
            and card.source in (top.program, top.program_id)
        )
        out.append(
            IntentRouting(
                intent=sentence,
                expects=expects,
                routed=routed,
                program=None if top is None else top.program,
                instruction=None if top is None else top.instruction,
                kind=None if top is None else top.kind,
                paraphrase=is_paraphrase,
            )
        )
    return tuple(out)


def _graph_of(card: CapabilityCard):  # type: ignore[no-untyped-def]
    """Gate 1: the graph builds, from the card's own IDL file. No network, ever."""
    from .program_graph import build_program_graph  # noqa: PLC0415

    if card.idl_path is None:
        raise CapabilityCardError(
            "a solana-program card names its IDL (surface.idl, a path beside the card); "
            "this check reads no chain and fetches no catalog"
        )
    try:
        idl = json.loads(card.idl_path.read_text(encoding="utf-8"))
    except OSError:
        raise CapabilityCardError(
            f"the IDL at {card.idl_path} could not be read"
        ) from None
    except json.JSONDecodeError as exc:
        raise CapabilityCardError(f"the IDL is not valid JSON: {exc}") from None
    try:
        return build_program_graph(idl, program_id=card.source)
    except Exception as exc:  # noqa: BLE001 - the builder's refusal IS the gate's answer
        raise CapabilityCardError(
            f"the graph did not build: {type(exc).__name__}: {exc}"
        ) from None


def _text(table: Mapping[str, Any], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityCardError(f"{key} is required and must be a non-empty string")
    return value.strip()


def _tokens(text: str) -> set[str]:
    return {
        word
        for word in "".join(c if c.isalnum() else " " for c in text.lower()).split()
        if len(word) > 2
    }


def report_for(
    path: str | Path, *, environ: Mapping[str, str] | None = None
) -> CapabilityReport:
    """Load and check in one call — what the CLI and the course's bonus both want."""
    return check_card(load_card(path), environ=environ)


def render(report: CapabilityReport, *, as_json: bool = False) -> str:
    return json.dumps(report.to_dict(), indent=2) if as_json else report.to_markdown()
