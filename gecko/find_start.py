"""``find_start`` — route a plain intent to the right starting point across programs.

The hardest thing for an agent acting on-chain is not calling an instruction — it
is knowing WHERE TO START among N programs × dozens of instructions. This module
is the catalog router: the agent says an intent ("buy this token on pump and hold
it") and Gecko returns the exact starting point — (program, instruction), the
dependency-ordered derive plan with a provenance tag on every account (the
source-recovered roots and IDL-hidden accounts included), the DECLARED landing
preludes (idempotent ATA w/ the Token-2022 note, compute budget), the honest
flagged gaps, and the execute pointer at Orquestra's builder.

Design constraints (do not weaken):

- **Lexical, no vectors.** Scoring reuses :mod:`gecko.catalog`'s overlap engine
  (the same engine that routes API intents). The semantic gate stays closed;
  misses are *instrumented* (categorical only — never the intent text) so the
  lexical-vs-semantic evidence gate gets data instead of a guess.
- **No fabrication.** Below the retrieval floor the answer is "no start found",
  with the closest candidates labeled GUESSES — never dressed up as starts.
- **Honesty on every account.** Each derive-plan step carries a closed
  provenance tag (:data:`gecko.provenance.AccountProvenance`); FLAGGED entries
  are never dropped.
- **Control plane only.** find_start returns plans and pointers; it never
  derives against a live chain here, never signs, never broadcasts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from importlib import resources
from typing import Any, Callable, Literal, Mapping, Sequence

from .catalog import Catalog, _tokens
from .ingest import Operation
from .orquestra_client import ProjectCatalogPage
from .pda import PdaNode
from .program_graph import derivation_order_for
from .provenance import AccountProvenance

__all__ = [
    "GapSpec",
    "PreludeSpec",
    "StartSpec",
    "DeriveStep",
    "StartPoint",
    "CatalogCandidate",
    "MissRecord",
    "MissLogger",
    "FindStartResult",
    "find_start",
    "format_result",
    "jsonl_miss_logger",
]

# Defensive cap on the caller-supplied intent before tokenizing (untrusted input).
MAX_INTENT_CHARS = 500
# Cap on catalog-entry text (name+description) folded into scoring — the catalog
# is UNTRUSTED input (orquestra_client validates shape; we still cap text here).
MAX_CATALOG_TEXT_CHARS = 800
# How many catalog candidates ride along with a result.
CATALOG_CANDIDATE_LIMIT = 3

# A small CLOSED stopword set applied to the QUERY only (catalog.py's scorer is
# untouched): without it, "the"/"and" overlap every program's prose and a nonsense
# intent would clear the floor on stopwords alone.
_STOPWORDS = frozenset(
    "a an and are as at be by can do for from how i in into is it me my of on or "
    "our please some that the then this to want we what which with you your".split()
)

StartKind = Literal["start", "surface", "guess"]

# rank for deterministic ordering at equal score: an executable intent beats a
# program surface, which beats a below-floor guess.
_KIND_RANK = {"start": 0, "surface": 1, "guess": 2}


# --- declarative start metadata (supplied by provider modules) -------------------


@dataclass(frozen=True)
class GapSpec:
    """An honest, FLAGGED gap of a start point — declared, never fabricated."""

    name: str
    note: str


@dataclass(frozen=True)
class PreludeSpec:
    """One DECLARED landing prelude (structured data — a builder assembles it)."""

    kind: str
    note: str
    program: str = ""


@dataclass(frozen=True)
class StartSpec:
    """What a wired plan intent declares about itself for routing: which config
    PDAs its plan derives, extra recovered-knowledge notes (source-recovered
    facts the packaged overlay does not carry), its honest gaps, and its
    DECLARED preludes. Pure data, authored next to the intent it describes."""

    accounts: tuple[str, ...]
    recovered: Mapping[str, str] = field(default_factory=dict)
    gaps: tuple[GapSpec, ...] = ()
    preludes: tuple[PreludeSpec, ...] = ()


# --- results ---------------------------------------------------------------------


@dataclass(frozen=True)
class DeriveStep:
    """One account of the dependency-ordered derive plan, provenance-tagged."""

    account: str
    provenance: AccountProvenance
    note: str = ""
    resolver: str | None = None  # the declared read recipe/reason, when present


@dataclass(frozen=True)
class StartPoint:
    """One ranked starting point. ``kind`` is honest: ``start`` = an executable
    plan intent; ``surface`` = a wired program without a plan intent for this ask
    (start from its derive/graph tools); ``guess`` = below the retrieval floor —
    a closest-candidate, NOT a start."""

    kind: StartKind
    program: str
    program_id: str
    instruction: str | None
    next_tool: str | None
    score: int
    why: tuple[str, ...]
    inputs: tuple[str, ...]
    derive_plan: tuple[DeriveStep, ...]
    preludes: tuple[PreludeSpec, ...]
    gaps: tuple[GapSpec, ...]
    execute: dict[str, str] | None
    serve: str
    note: str = ""


@dataclass(frozen=True)
class CatalogCandidate:
    """An UNWIRED catalog program matching the intent — a comprehend-on-pick
    pointer (the D-A path), never presented as a runnable start."""

    slug: str
    name: str
    program_id: str
    score: int
    why: tuple[str, ...]
    comprehend_first: dict[str, str]


@dataclass(frozen=True)
class MissRecord:
    """A CATEGORICAL retrieval-miss record — counts only, NEVER the intent text
    (it could carry user data; control-plane rules). Feeds the lexical-vs-semantic
    evidence gate."""

    intent_term_count: int
    matched_score: int
    wired_program_count: int


MissLogger = Callable[[MissRecord], None]


@dataclass(frozen=True)
class FindStartResult:
    """The router's answer: ranked start points, catalog pointers, and an honest
    ``no_start`` verdict when nothing clears the floor."""

    starts: tuple[StartPoint, ...]
    catalog: tuple[CatalogCandidate, ...]
    no_start: bool
    note: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# --- the wired-program index -----------------------------------------------------


@dataclass(frozen=True)
class _Card:
    """One scorable candidate: an intent start point or a program surface."""

    kind: StartKind
    api_id: str
    program_id: str
    instruction: str | None
    intent_name: str | None
    inputs: tuple[str, ...]
    accounts: tuple[str, ...]
    pdas: dict[str, PdaNode]
    spec: StartSpec | None
    notes: str
    execute_url: str | None
    operation: Operation
    # the program's wired plan-intent names (surface cards only — honesty: a
    # surface card must not claim "no intent wired" when start points exist)
    wired_intents: tuple[str, ...] = ()


def _first_sentence(text: str) -> str:
    head = (text or "").split(". ", 1)[0]
    return head[:200]


def _operation(
    operation_id: str, path: str, summary: str, description: str, tags: list[str]
) -> Operation:
    return Operation(
        method="GET",
        path=path,
        operation_id=operation_id,
        summary=summary,
        description=description,
        tags=tags,
        parameters=[],
        request_body=None,
        responses={},
    )


def _packaged_overlay(api_id: str) -> dict[str, Any]:
    """The packaged manual overlay for a program ({} when absent) — the explicit
    beyond-the-surface knowledge whose ``why`` map powers ``recovered`` notes."""
    try:
        anchor = resources.files("gecko.providers.configs").joinpath(
            "orquestra", "overlays", f"{api_id}.json"
        )
        data = json.loads(anchor.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _wired_cards() -> list[_Card]:
    """Build the scorable index from the packaged configs + intent registries.
    Lazy provider imports keep module import light and cycle-free."""
    from .provider_config import (
        load_packaged_provider,
        load_packaged_provider_base_url,
    )
    from .providers.cli import intent_registries, start_specs

    _, apis = load_packaged_provider("orquestra")
    base = load_packaged_provider_base_url("orquestra").rstrip("/")
    intents = intent_registries()
    specs = start_specs()

    cards: list[_Card] = []
    for api_id, api in apis.items():
        program = api.program
        if program is None:
            continue
        project_base = (
            f"{base}/{program.orquestra_project}" if program.orquestra_project else None
        )
        notes = program.notes
        # The program's recovered-knowledge notes are PROGRAM-level truths (a
        # source-recovered root is recovered no matter which tool derives it), so
        # the surface card carries the union of its intents' recovered maps —
        # otherwise the same account would honestly read "recovered" on the intent
        # card and misleadingly "extracted" on the surface card.
        program_recovered: dict[str, str] = {}
        for spec in specs.get(api_id, {}).values():
            program_recovered.update(spec.recovered)
        # the program-surface card: derive_pda/get_program_graph is always a start
        cards.append(
            _Card(
                kind="surface",
                api_id=api_id,
                program_id=program.program_id,
                instruction=None,
                intent_name=None,
                inputs=(),
                accounts=tuple(program.pdas),
                pdas=dict(program.pdas),
                spec=StartSpec(
                    accounts=tuple(program.pdas), recovered=program_recovered
                ),
                notes=notes,
                execute_url=project_base,
                operation=_operation(
                    operation_id=api_id,
                    path=f"/{api_id}",
                    summary=_first_sentence(notes),
                    description=f"{notes} accounts: {' '.join(program.pdas)}",
                    tags=[api_id],
                ),
                wired_intents=tuple(program.intents),
            )
        )
        # one card per wired plan intent
        for name in program.intents:
            intent = intents.get(api_id, {}).get(name)
            if intent is None:
                continue
            start_spec = specs.get(api_id, {}).get(name)
            accounts = start_spec.accounts if start_spec else tuple(program.pdas)
            execute_url = (
                f"{project_base}/instructions/{intent.instruction}/build"
                if project_base
                else None
            )
            cards.append(
                _Card(
                    kind="start",
                    api_id=api_id,
                    program_id=program.program_id,
                    instruction=intent.instruction,
                    intent_name=intent.name,
                    inputs=tuple(intent.inputs),
                    accounts=accounts,
                    pdas=dict(program.pdas),
                    spec=start_spec,
                    notes=notes,
                    execute_url=execute_url,
                    operation=_operation(
                        operation_id=intent.name,
                        path=f"/{api_id}/{intent.instruction}",
                        summary=_first_sentence(intent.description),
                        description=(
                            f"{intent.description} inputs: {', '.join(intent.inputs)}"
                        ),
                        tags=[api_id, intent.instruction],
                    ),
                )
            )
    return cards


# --- provenance tagging (mechanical + packaged-data-driven) ----------------------


def _account_step(
    name: str,
    node: PdaNode | None,
    *,
    recovered: Mapping[str, str],
    overlay_pdas: frozenset[str],
    overlay_why: Mapping[str, str],
) -> DeriveStep:
    """Tag one account. FLAGGED wins (a resolver seed with no declared read recipe
    is an unresolved gap regardless of how the recipe was obtained); ``recovered``
    marks source/overlay-rescued knowledge or a declared read recipe; everything
    straight off the surface stays ``extracted``."""
    resolver_seeds = node.unresolved_seeds if node is not None else ()
    resolver_note = (
        "; ".join(s.reason for s in resolver_seeds) if resolver_seeds else None
    )
    has_recipe = bool(resolver_seeds) and all(
        s.resolve is not None for s in resolver_seeds
    )
    if resolver_seeds and not has_recipe:
        return DeriveStep(
            account=name,
            provenance="flagged",
            note=overlay_why.get(name, ""),
            resolver=resolver_note,
        )
    if name in recovered:
        return DeriveStep(
            account=name,
            provenance="recovered",
            note=recovered[name],
            resolver=resolver_note,
        )
    if name in overlay_pdas:
        return DeriveStep(
            account=name,
            provenance="recovered",
            note=overlay_why.get(name, "beyond-surface recipe (manual overlay)"),
            resolver=resolver_note,
        )
    if has_recipe:
        return DeriveStep(
            account=name,
            provenance="recovered",
            note="a declared control-plane read recipe resolves this seed at plan time",
            resolver=resolver_note,
        )
    return DeriveStep(account=name, provenance="extracted")


def _derive_plan(card: _Card) -> tuple[DeriveStep, ...]:
    overlay = _packaged_overlay(card.api_id)
    overlay_pdas = frozenset((overlay.get("pdas") or {}).keys())
    overlay_why_raw = overlay.get("why") or {}
    overlay_why = {str(k): str(v) for k, v in overlay_why_raw.items()}
    recovered = card.spec.recovered if card.spec else {}
    ordered = derivation_order_for(card.pdas, card.accounts)
    steps = [
        _account_step(
            name,
            card.pdas.get(name),
            recovered=recovered,
            overlay_pdas=overlay_pdas,
            overlay_why=overlay_why,
        )
        for name in ordered
    ]
    # declared gaps join the plan as FLAGGED steps — never dropped
    if card.spec:
        present = {s.account for s in steps}
        for gap in card.spec.gaps:
            if gap.name not in present:
                steps.append(
                    DeriveStep(account=gap.name, provenance="flagged", note=gap.note)
                )
    return tuple(steps)


def _gaps_of(card: _Card, plan: tuple[DeriveStep, ...]) -> tuple[GapSpec, ...]:
    declared = {g.name: g for g in (card.spec.gaps if card.spec else ())}
    out: list[GapSpec] = []
    for step in plan:
        if step.provenance != "flagged":
            continue
        if step.account in declared:
            out.append(declared[step.account])
        else:
            out.append(
                GapSpec(step.account, step.note or step.resolver or "unresolved")
            )
    return tuple(out)


# --- scoring + assembly ----------------------------------------------------------


def _query_tokens(intent: str) -> set[str]:
    return _tokens(intent[:MAX_INTENT_CHARS]) - _STOPWORDS


def _card_terms(card: _Card) -> set[str]:
    op = card.operation
    return _tokens(
        " ".join(
            [op.summary, op.description, op.path, " ".join(op.tags), op.operation_id]
        )
    )


def _start_point(
    card: _Card, score: int, why: tuple[str, ...], kind: StartKind
) -> StartPoint:
    plan = _derive_plan(card)
    execute: dict[str, str] | None = None
    if card.execute_url:
        execute = (
            {"builder": "orquestra", "method": "POST", "url": card.execute_url}
            if card.kind == "start"
            else {"builder": "orquestra", "url": card.execute_url}
        )
    if kind == "guess":
        note = (
            "GUESS — no lexical overlap with the intent cleared the floor; this is "
            "a closest candidate, not a start"
        )
    elif card.kind == "surface":
        if card.wired_intents:
            note = (
                f"this program's plan intents ({', '.join(card.wired_intents)}) are "
                "separate start points; this entry is its raw surface "
                "(get_program_graph / derive_pda). " + _first_sentence(card.notes)
            )
        else:
            note = (
                "no executable plan intent is wired for this program yet — start "
                "from its surface tools (get_program_graph / derive_pda). "
                + _first_sentence(card.notes)
            )
    else:
        note = _first_sentence(card.notes)
    return StartPoint(
        kind=kind,
        program=card.api_id,
        program_id=card.program_id,
        instruction=card.instruction,
        next_tool=card.intent_name,
        score=score,
        why=why,
        inputs=card.inputs,
        derive_plan=plan,
        preludes=card.spec.preludes if card.spec else (),
        gaps=_gaps_of(card, plan),
        execute=execute,
        serve=f"gecko-orquestra --program {card.api_id} --stdio",
        note=note,
    )


def _catalog_candidates(
    q_tokens: set[str],
    catalog_pages: Sequence[ProjectCatalogPage],
    wired_program_ids: set[str],
    *,
    program_hint: str | None = None,
) -> tuple[CatalogCandidate, ...]:
    scored: list[tuple[int, CatalogCandidate]] = []
    for page in catalog_pages:
        for project in page.projects:
            if project.program_id in wired_program_ids:
                continue
            text = f"{project.name} {project.description}"[:MAX_CATALOG_TEXT_CHARS]
            overlap = q_tokens & _tokens(text)
            hint_match = bool(program_hint) and (
                program_hint == project.id
                or (program_hint or "").lower() in project.name.lower()
            )
            if not overlap and not hint_match:
                continue
            candidate = CatalogCandidate(
                slug=project.id,
                name=project.name[:120],
                program_id=project.program_id,
                score=len(overlap),
                why=tuple(sorted(overlap)),
                comprehend_first={
                    "step": (
                        "this program is in the catalog but NOT yet comprehended — "
                        "no derive plan exists; comprehend it first (a pointer, not "
                        "a start)"
                    ),
                    "cli": f"gecko-orquestra comprehend --project {project.id}",
                    "tool": "comprehend_program",
                },
            )
            scored.append((len(overlap) + (100 if hint_match else 0), candidate))
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return tuple(c for _, c in scored[:CATALOG_CANDIDATE_LIMIT])


def find_start(
    intent: str,
    *,
    program: str | None = None,
    catalog_pages: Sequence[ProjectCatalogPage] = (),
    limit: int = 5,
    on_miss: MissLogger | None = None,
) -> FindStartResult:
    """Route ``intent`` to ranked start points across the wired programs.

    ``program`` is an optional hint (a wired api_id narrows the search; an
    unwired name is looked up in ``catalog_pages``). ``catalog_pages`` are
    already-fetched, already-validated catalog pages (the caller controls the
    fetch policy; this function is pure and offline). ``on_miss`` is the opt-in
    instrumentation seam — called with a CATEGORICAL :class:`MissRecord` when
    nothing clears the floor. Off by default; never receives the intent text.
    """
    cards = _wired_cards()
    wired_programs = {c.api_id for c in cards}
    wired_ids = {c.program_id for c in cards}

    hint_unwired = program is not None and program not in wired_programs
    if program is not None and not hint_unwired:
        cards = [c for c in cards if c.api_id == program]

    q_tokens = _query_tokens(intent)
    candidates = _catalog_candidates(
        q_tokens,
        catalog_pages,
        wired_ids,
        program_hint=program if hint_unwired else None,
    )

    def _miss(top_score: int) -> None:
        if on_miss is not None:
            on_miss(
                MissRecord(
                    intent_term_count=len(q_tokens),
                    matched_score=top_score,
                    wired_program_count=len(wired_programs),
                )
            )

    if not q_tokens:
        _miss(0)
        return FindStartResult(
            starts=(),
            catalog=candidates,
            no_start=True,
            note="the intent carried no content-bearing terms — nothing to route",
        )
    if hint_unwired:
        _miss(0)
        note = f"program {program!r} is not wired; " + (
            "closest catalog matches below — comprehend first (the D-A path)"
            if candidates
            else "no catalog match either (searched the supplied pages)"
        )
        return FindStartResult(starts=(), catalog=candidates, no_start=True, note=note)

    by_op = {id(c.operation): c for c in cards}
    scored = Catalog([c.operation for c in cards]).search_scored(
        " ".join(sorted(q_tokens)), limit=limit
    )
    genuine = [se for se in scored if not se.is_fallback]
    if genuine:
        points = []
        for se in genuine:
            card = by_op[id(se.entry.operation)]
            why = tuple(sorted(q_tokens & _card_terms(card)))
            points.append(_start_point(card, se.score, why, kind=card.kind))
        points.sort(key=lambda p: (-p.score, _KIND_RANK[p.kind], p.program))
        return FindStartResult(
            starts=tuple(points),
            catalog=candidates,
            no_start=False,
            note=(
                "ranked start points — provenance-tagged derive plans; FLAGGED gaps "
                "are honest, never dropped. Gecko plans and points; Orquestra builds; "
                "nothing here signs or broadcasts."
            ),
        )

    # below the floor: honest no-start; the fallback candidates are labeled GUESSES
    _miss(0)
    guesses = tuple(
        _start_point(by_op[id(se.entry.operation)], 0, (), kind="guess")
        for se in scored
    )
    return FindStartResult(
        starts=guesses,
        catalog=candidates,
        no_start=True,
        note=(
            "no start found — the intent matched no wired program above the floor. "
            "The entries below are closest candidates (GUESSES), not starts."
        ),
    )


# --- the opt-in miss-log seam ----------------------------------------------------


def jsonl_miss_logger(path: str) -> MissLogger:
    """A :data:`MissLogger` appending one JSON line per miss to ``path``.

    Categorical only — the record holds counts, never text (control-plane rules);
    opt-in via the CLI flag, never on by default."""

    def _log(record: MissRecord) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")

    return _log


# --- legible rendering (the CLI prints this verbatim) ----------------------------


def _render_step(index: int, step: DeriveStep) -> list[str]:
    tag = "FLAGGED" if step.provenance == "flagged" else step.provenance
    line = f"    {index:>2}. {step.account:<28} [{tag}]"
    lines = [line]
    if step.note:
        lines.append(f"        {step.note}")
    if step.resolver:
        lines.append(f"        resolver: {step.resolver}")
    return lines


def format_result(result: FindStartResult) -> str:
    lines: list[str] = []
    if result.no_start:
        lines.append("NO START FOUND")
    lines.append(result.note)
    lines.append("")
    for rank, point in enumerate(result.starts, 1):
        head = (
            point.program
            if point.instruction is None
            else f"{point.program}/{point.instruction}"
        )
        label = {"start": "START", "surface": "SURFACE", "guess": "GUESS"}[point.kind]
        why = f" (matched: {', '.join(point.why)})" if point.why else ""
        lines.append(f"{label} {rank}. {head} — score {point.score}{why}")
        lines.append(f"    program_id: {point.program_id}")
        if point.next_tool:
            lines.append(f"    next tool:  {point.next_tool}")
        lines.append(f"    serve:      {point.serve}")
        if point.inputs:
            lines.append(f"    inputs:     {', '.join(point.inputs)}")
        if point.note:
            lines.append(f"    note:       {point.note}")
        if point.derive_plan:
            lines.append("    derive plan (dependency-ordered):")
            for index, step in enumerate(point.derive_plan, 1):
                lines.extend(_render_step(index, step))
        if point.preludes:
            lines.append("    preludes (DECLARED):")
            for prelude in point.preludes:
                lines.append(f"      - {prelude.kind}: {prelude.note}")
        if point.gaps:
            lines.append("    flagged gaps (honest — resolve before building):")
            for gap in point.gaps:
                lines.append(f"      ! {gap.name}: {gap.note}")
        if point.execute:
            method = point.execute.get("method", "GET")
            lines.append(
                f"    execute:    {method} {point.execute['url']} "
                "(Orquestra builds; Gecko never signs or broadcasts)"
            )
        lines.append("")
    if result.catalog:
        lines.append("catalog candidates (NOT wired — comprehend first, the D-A path):")
        for candidate in result.catalog:
            lines.append(
                f"  ? {candidate.name} — program_id {candidate.program_id} "
                f"(slug {candidate.slug})"
            )
            lines.append(f"    {candidate.comprehend_first['cli']}")
    return "\n".join(lines).rstrip() + "\n"
