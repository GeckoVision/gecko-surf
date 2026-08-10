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
from dataclasses import asdict, dataclass, field, replace
from importlib import resources
from typing import Any, Callable, Literal, Mapping, Sequence

from .catalog import Catalog, _tokens
from .ingest import Operation
from .lexnorm import STOPWORDS, fold_tokens, normalize_query
from .orquestra_client import ProjectCatalogPage
from .pda import PdaNode
from .program_graph import derivation_order_with_cycle
from .provenance import AccountProvenance, ProgramProvenanceTier

__all__ = [
    "GapSpec",
    "PreludeSpec",
    "StartSpec",
    "DeriveStep",
    "StartPoint",
    "CatalogCandidate",
    "CandidateRecord",
    "FloorKind",
    "MissCause",
    "MissRecord",
    "MissLogger",
    "FindStartResult",
    "candidate_name",
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

# The stopword set, applied to the QUERY: without it, "the"/"and" overlap every
# program's prose and a nonsense intent would clear the floor on stopwords alone.
# SINGLE SOURCE OF TRUTH — this module used to declare its own copy while the catalog
# scorer had none, and the two diverging is precisely how function words came to decide
# an API ranking (the measured Pegana `delete_sub` miss). Both read it from lexnorm now.
_STOPWORDS = STOPWORDS

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
    #: Accounts the PROGRAM SURFACE names but declares no PDA recipe for — the plain
    #: caller-supplied slots (a token program id, the signer, a mint, the program id
    #: itself). They carry no node in the packaged config, so without this they would be
    #: indistinguishable from a name nothing has ever heard of.
    #:
    #: This exists because ``extracted`` must be EARNED (R3). ``_account_step`` used to
    #: fall through to ``extracted`` for any unknown string, so a typo in ``accounts``
    #: shipped as a confident "the surface stated this". Listing an account here is an
    #: affirmative, reviewed claim that the program artifact names it — the same trust
    #: class as ``recovered`` above, and asserted against the landing orchestrator in
    #: ``tests/test_derive_plan_provenance.py``. Absence means FLAGGED, never ``extracted``.
    surface_named: tuple[str, ...] = ()


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


# What the caller was actually served: genuine ``start``/``surface`` points, or
# only below-floor GUESSES. A caller proceeding on a "guess" record is the failure
# mode recall metrics never see — this makes it countable.
FloorKind = Literal["start", "guess"]

# The CLOSED miss-cause vocabulary (single source of truth — the eval runner and
# every consumer import it from here, never redeclare):
#   hit              — the gold start ranked within top-k
#   no_content_tokens — the intent carried no content-bearing terms
#   coverage_gap     — the gold program/instruction is NOT wired (argues for
#                      wiring more programs, NOT for a semantic retriever)
#   vocabulary_gap   — gold IS wired but shares zero terms with its card
#   misrank          — gold IS wired, overlap > 0, yet ranked below top-k
#   false_accept     — a deliberately out-of-scope intent cleared the floor
#                      (precision/floor evidence, not ranking evidence)
# Only vocabulary_gap + misrank count as evidence FOR the semantic flip.
MissCause = Literal[
    "hit",
    "no_content_tokens",
    "coverage_gap",
    "vocabulary_gap",
    "misrank",
    "false_accept",
]


def candidate_name(program: str, instruction: str | None) -> str:
    """The control-plane-safe display name of a candidate — program/instruction
    names only, never intent text."""
    return f"{program}/{instruction}" if instruction else program


@dataclass(frozen=True)
class CandidateRecord:
    """One served candidate, reduced to control-plane-safe fields: the program/
    instruction NAME and its lexical score — never the intent text."""

    name: str
    score: int
    kind: StartKind


@dataclass(frozen=True)
class MissRecord:
    """A CATEGORICAL retrieval record — names, scores, and counts only, NEVER the
    intent text (it could carry user data; control-plane rules). Feeds the
    lexical-vs-semantic evidence gate.

    v2 adds the gold-free ranking fields (``top_candidates``/``margin``/``floor``
    — program/instruction names are control-plane-safe) plus two eval-only fields
    (``gold_rank``/``miss_cause``) populated exclusively by
    :mod:`gecko.retrieval_eval` against the committed golden set; in production
    they stay ``None``."""

    intent_term_count: int
    matched_score: int
    wired_program_count: int
    top_candidates: tuple[CandidateRecord, ...] = ()
    margin: int = 0  # top1 − top2 score; 0 when fewer than two candidates
    floor: FloorKind = "guess"
    gold_rank: int | None = None  # eval-only
    miss_cause: MissCause | None = None  # eval-only


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
    # R7: the packaged config's ``program.pda_origins`` — the per-account tier
    # comprehension computed, read back as a one-way DEMOTION cap on the mechanical
    # tag (see :func:`_apply_origin_cap`). ``None`` = present-but-invalid entry,
    # capped at ``flagged``. Empty = the config asserts nothing (today's behaviour).
    origins: Mapping[str, ProgramProvenanceTier | None] = field(default_factory=dict)


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
        # One segment per call — see the note in provider_config._read_json. Inside a
        # PyInstaller binary this resolves to a MultiplexedPath, whose joinpath takes
        # exactly ONE segment; the multi-arg form raises only once frozen.
        anchor = (
            resources.files("gecko.providers.configs")
            .joinpath("orquestra")
            .joinpath("overlays")
            .joinpath(f"{api_id}.json")
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
                origins=dict(program.pda_origins),
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
                    origins=dict(program.pda_origins),
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
    cyclic: frozenset[str] = frozenset(),
    surface_named: frozenset[str] = frozenset(),
) -> DeriveStep:
    """Tag one account. FLAGGED wins (a resolver seed with no declared read recipe
    is an unresolved gap regardless of how the recipe was obtained); ``recovered``
    marks source/overlay-rescued knowledge or a declared read recipe.

    ``extracted`` is EARNED, never a fallthrough (R3). It requires positive evidence that
    the surface actually named this account, in one of exactly two forms: the packaged
    config holds a PDA node for it (the IDL states the recipe), or the intent's
    ``StartSpec`` lists it in ``surface_named`` (a plain non-PDA slot the artifact names).
    Anything else is an honest FLAGGED gap. Before R3 the terminal branch returned
    ``extracted`` for any string whatsoever, so "the surface stated this" and "we hold
    nothing for this name" were the same answer, and a typo in a hand-authored
    ``StartSpec.accounts`` shipped as a confident spec-stated step.

    A ``cyclic`` account outranks all of that: however good its recipe, it cannot be
    placed in a derivation order, so the plan reports a gap rather than a position."""
    if name in cyclic:
        members = ", ".join(sorted(cyclic))
        return DeriveStep(
            account=name,
            provenance="flagged",
            note=(
                "unorderable: seed-dependency cycle among "
                f"{members} — this account's derivation depends on itself, "
                "directly or through another account of this instruction"
            ),
        )
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
    if node is not None or name in surface_named:
        return DeriveStep(account=name, provenance="extracted")
    return DeriveStep(
        account=name,
        provenance="flagged",
        note=(
            "no recipe and not named by the surface — nothing in the packaged program "
            "config or this intent's declared account set accounts for this name, so "
            "the plan reports a gap rather than claiming the surface stated it"
        ),
    )


# --- the R7 origin cap -------------------------------------------------------------

# Claim strength of an account tag: how much it asserts about where the recipe came
# from. The config-asserted origin is a one-way DEMOTION switch over this order —
# final = min(mechanical, asserted) — so a poisoned or over-confident config can only
# ever LOWER a claim, never mint one.
_CLAIM_STRENGTH: dict[str, int] = {"extracted": 2, "recovered": 1, "flagged": 0}

# The explicit, total, CLOSED tier→tag mapping. ``manual`` reads as ``recovered``:
# hand-supplied through the reviewed overlay is the same trust class as
# source-recovered — derived from caller-supplied input, not the program artifact's
# own word. ``cross_surface`` and ``flagged`` are deliberately unmapped: cross_surface
# is a request-time fact from ANOTHER surface (never assertable from a program
# artifact — see gecko.provenance), and flagged is COMPUTED (an unresolved seed, a
# cycle, an unknown name), never asserted; the loader refuses both into the invalid
# (``None``) marker.
_ORIGIN_TIER_TO_TAG: dict[ProgramProvenanceTier, AccountProvenance] = {
    "extracted": "extracted",
    "recovered": "recovered",
    "manual": "recovered",
}

# HONESTY: ``recovered`` means derived from caller-supplied source at comprehension
# time — NOT verified against chain. The only refutation of a wrong address remains
# the simulate→Receipt.
_RECOVERED_CAP_NOTE = (
    "origin (packaged config): this recipe was derived from caller-supplied source "
    "at comprehension time — it is not the program artifact's own word and not "
    "verified against chain; the simulate→Receipt is the check"
)
_INVALID_ORIGIN_NOTE = (
    "the packaged config asserts an origin outside the closed tier ladder for this "
    "account — failing closed: reported as a gap, never coerced into a confident tag"
)


def _apply_origin_cap(
    step: DeriveStep, origins: Mapping[str, ProgramProvenanceTier | None]
) -> DeriveStep:
    """Cap one mechanical step with the config-carried origin (R7).

    :func:`_account_step` stays the sole computer of the mechanical tag — flagged
    first: an unresolved seed or a seed cycle is flagged no matter what any config
    says. This function applies ``program.pda_origins`` to that RESULT as
    ``final = min(mechanical, asserted)`` over claim strength. Absent entry → the
    step is returned untouched (exactly the pre-R7 behaviour; absence never defaults
    to any tier). A present-but-invalid entry (``None`` from the loader) caps that
    ONE account at ``flagged`` — contained, loud in the note, never coerced.
    """
    if step.account not in origins:
        return step
    tier = origins[step.account]
    if tier is None:
        if step.provenance == "flagged":
            return step  # already the floor; keep the mechanical explanation
        return replace(step, provenance="flagged", note=_INVALID_ORIGIN_NOTE)
    asserted = _ORIGIN_TIER_TO_TAG[tier]
    if _CLAIM_STRENGTH[asserted] >= _CLAIM_STRENGTH[step.provenance]:
        return step  # the mechanical claim is already at or below the assertion
    return replace(step, provenance=asserted, note=step.note or _RECOVERED_CAP_NOTE)


def _derive_plan(card: _Card) -> tuple[DeriveStep, ...]:
    overlay = _packaged_overlay(card.api_id)
    overlay_pdas = frozenset((overlay.get("pdas") or {}).keys())
    overlay_why_raw = overlay.get("why") or {}
    overlay_why = {str(k): str(v) for k, v in overlay_why_raw.items()}
    recovered = card.spec.recovered if card.spec else {}
    surface_named = frozenset(card.spec.surface_named if card.spec else ())
    ordered, cyclic = derivation_order_with_cycle(card.pdas, card.accounts)
    steps = [
        # R7: the config-asserted origin caps the MECHANICAL result (min, demotion
        # only) — never a branch inside _account_step, which stays the sole computer
        # of the mechanical tag.
        _apply_origin_cap(
            _account_step(
                name,
                card.pdas.get(name),
                recovered=recovered,
                overlay_pdas=overlay_pdas,
                overlay_why=overlay_why,
                cyclic=cyclic,
                surface_named=surface_named,
            ),
            card.origins,
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
    """Query terms on the SAME vocabulary the catalog scorer ranks with — folded and
    floored. If these two ever drift apart, the identity/corroboration gates below stop
    agreeing with the ranking they are gating (a card promoted on `depegs`~`depeg` could
    be demoted for "not naming the program")."""
    return normalize_query(_tokens(intent[:MAX_INTENT_CHARS]))


def _card_terms(card: _Card) -> set[str]:
    op = card.operation
    return fold_tokens(
        _tokens(
            " ".join(
                [
                    op.summary,
                    op.description,
                    op.path,
                    " ".join(op.tags),
                    op.operation_id,
                ]
            )
        )
    )


def _distinguishing_terms(q_tokens: set[str], cards: Sequence["_Card"]) -> set[str]:
    """Query terms that actually narrow the field.

    A term present in most cards cannot select between them. "usdc" appears in nearly
    every program's prose, so matching on it alone is not evidence that THIS program is
    the right one — and a single such overlap was enough to promote a card to a runnable
    start (observed live: "get me out of hyUSD into USDC" routed to metadao/fund on a
    score of 2, matching "usdc" and nothing else).

    Document frequency settles it deterministically, with no vectors and no model: a term
    carried by more than half the cards is incidental; the rest are distinguishing. The
    threshold is a property of the corpus, not a tuned constant — as more programs are
    wired, terms that used to distinguish stop doing so on their own, which is correct.
    """
    if not cards:
        return set()
    frequency: dict[str, int] = {}
    for card in cards:
        for term in _card_terms(card) & q_tokens:
            frequency[term] = frequency.get(term, 0) + 1
    ceiling = len(cards) / 2
    return {term for term, count in frequency.items() if count <= ceiling}


def _identity_terms(card: "_Card") -> set[str]:
    """The terms that NAME this card — its program and its instruction.

    Matching one of these is direct evidence the caller meant this program or this
    action. Matching only surrounding prose is not: every DeFi program's description
    mentions tokens, swaps and USDC.
    """
    parts = [card.api_id, card.instruction or "", card.intent_name or ""]
    return fold_tokens(_tokens(" ".join(parts)))


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
            overlap = q_tokens & fold_tokens(_tokens(text))
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

    def _miss(top_score: int, served: Sequence[StartPoint]) -> None:
        if on_miss is not None:
            candidates = tuple(
                CandidateRecord(
                    name=candidate_name(p.program, p.instruction),
                    score=p.score,
                    kind=p.kind,
                )
                for p in served
            )
            margin = (
                candidates[0].score - candidates[1].score if len(candidates) > 1 else 0
            )
            on_miss(
                MissRecord(
                    intent_term_count=len(q_tokens),
                    matched_score=top_score,
                    wired_program_count=len(wired_programs),
                    top_candidates=candidates,
                    margin=margin,
                    floor="guess",  # every miss serves guesses (or nothing) — countable
                )
            )

    if not q_tokens:
        _miss(0, ())
        return FindStartResult(
            starts=(),
            catalog=candidates,
            no_start=True,
            note="the intent carried no content-bearing terms — nothing to route",
        )
    if hint_unwired:
        _miss(0, ())
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
        # The scorer's fallback flag is not a floor: a single incidental token overlap
        # clears it, and the result was presented as a RUNNABLE start. Require the match
        # to rest on at least one term that actually narrows the field; a card matched
        # only on terms everything shares is demoted to a guess, which is what it is.
        distinguishing = _distinguishing_terms(q_tokens, cards)
        points = []
        for se in genuine:
            card = by_op[id(se.entry.operation)]
            why = tuple(sorted(q_tokens & _card_terms(card)))
            matched = set(why)
            # A runnable start needs evidence the caller meant THIS program: either the
            # match names the program or its instruction, or it rests on two independent
            # distinguishing terms. One shared noun is not evidence — "get me out of
            # hyUSD into USDC" matched metadao/fund on the single word "usdc" and was
            # served as a plan to run.
            named = bool(matched & _identity_terms(card))
            corroborated = len(matched & distinguishing) >= 2
            # Only RUNNABLE cards are gated. A `surface` card already says "start from
            # this program's derive tools", which is a hedge, not a plan — demoting it
            # would erase the useful middle answer ("this program is plausibly relevant")
            # without preventing any harm.
            gated = card.kind == "start" and not (named or corroborated)
            kind = "guess" if gated else card.kind
            points.append(_start_point(card, se.score, why, kind=kind))
        points.sort(key=lambda p: (-p.score, _KIND_RANK[p.kind], p.program))
        if not any(point.kind == "start" for point in points):
            _miss(max((p.score for p in points), default=0), tuple(points))
            return FindStartResult(
                starts=tuple(points),
                catalog=candidates,
                no_start=True,
                note=(
                    "no start found — nothing matched a term naming a wired program "
                    "or its instruction, and no candidate was corroborated by two "
                    "distinguishing terms. The entries below are closest candidates "
                    "(GUESSES), not starts."
                ),
            )
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
    guesses = tuple(
        _start_point(by_op[id(se.entry.operation)], 0, (), kind="guess")
        for se in scored
    )
    _miss(0, guesses)
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
