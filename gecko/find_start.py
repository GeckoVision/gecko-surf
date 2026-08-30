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
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Sequence

from .catalog import Catalog, _tokens
from .ingest import Operation
from .lexnorm import STOPWORDS, fold_tokens, normalize_query
from .orquestra_client import ProjectCatalogPage
from .pda import PdaNode
from .program_graph import chain_order_with_cycle, derivation_order_with_cycle
from .provenance import AccountProvenance, ChainStatus, ProgramProvenanceTier

if (
    TYPE_CHECKING
):  # the provider_config import stays lazy at runtime (light + cycle-free)
    from .provider_config import ConfigTrust

__all__ = [
    "GapSpec",
    "PreludeSpec",
    "StartSpec",
    "ChainEdge",
    "ChainEdgeKind",
    "ChainSpec",
    "ChainStepSpec",
    "ChainLink",
    "ChainPlan",
    "ChainStep",
    "CHAIN_VERDICTS",
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


# --- declared lifecycle chains (the join an IDL/llms.txt loses) ------------------

# The kind of dependency ONE chain edge carries. CLOSED, and deliberately narrow:
#
#   ``initializes`` — the consumer requires an account the producer CREATED. The
#     dependency is the account's existence; nothing the producer wrote is read back.
#   ``produces``    — a real DATA dependency: an argument of the consumer is a value
#     the producer wrote into the shared account (let_me_buy's ``receipt_id``).
#
# The distinction is load-bearing for the agent: an ``initializes`` edge can be
# satisfied by a store somebody else opened, while a ``produces`` edge cannot be
# satisfied at all until the producing call has landed.
ChainEdgeKind = Literal["initializes", "produces"]


@dataclass(frozen=True)
class ChainEdge:
    """One DECLARED ordering edge between two instructions of the same program.

    Every field exists so the edge can answer, without reading code, the four
    questions an edge must answer: what it connects (``produces`` → ``consumes``),
    over what (``account`` — the shared account carrying the dependency), on what
    ``basis``, and what evidence would refute it (``refuted_by``). The provenance
    class is not asserted here: it is COMPUTED per plan from the linking account's
    own tag on both endpoints (see :class:`ChainLink`), so a declaration can never
    mint a stronger claim than the accounts underneath it.
    """

    produces: str
    consumes: str
    account: str
    kind: ChainEdgeKind
    basis: str
    refuted_by: str


@dataclass(frozen=True)
class ChainStepSpec:
    """One instruction of a declared chain: what it is called, how it describes
    itself to the router, what the agent supplies, and its account set."""

    instruction: str
    description: str
    inputs: tuple[str, ...]
    spec: StartSpec


@dataclass(frozen=True)
class ChainSpec:
    """A DECLARED multi-instruction lifecycle of one program — authored data, in the
    same trust class as :class:`StartSpec` (hand-authored from the program artifact,
    reviewed, asserted in tests), never extracted from an untrusted document.

    ``steps`` is the declared instruction set; ``edges`` are the dependencies. The
    ORDER is not stored — it is derived from ``edges`` by
    :func:`gecko.program_graph.chain_order_with_cycle`, the same rail the account
    level uses, so an unorderable chain is reported rather than sequenced.
    """

    name: str
    api_id: str
    summary: str
    steps: tuple[ChainStepSpec, ...]
    edges: tuple[ChainEdge, ...]


# --- results ---------------------------------------------------------------------


#: Who produces an account's ADDRESS. This is a different axis from ``provenance``, and
#: conflating them is what made a plan read as ready when it was not: ``provenance``
#: grades the RECIPE (did the surface state it, did we recover it, is it flagged), while
#: this says whether there is a recipe to run at all. A ``caller`` step is routinely
#: ``extracted`` — the program artifact really does name the slot — and still has no
#: obtainable value until the caller goes and finds one.
AccountSupplier = Literal["derived", "caller"]


@dataclass(frozen=True)
class DeriveStep:
    """One account of the dependency-ordered derive plan, provenance-tagged."""

    account: str
    provenance: AccountProvenance
    note: str = ""
    resolver: str | None = None  # the declared read recipe/reason, when present
    #: Defaults to ``caller`` because that is the fail-closed value: a step that never
    #: said who produces it must not read as one Gecko derives.
    supplied_by: AccountSupplier = "caller"


# The closed chain-AGREEMENT vocabulary this module ACCEPTS as evidence. It is not a
# ladder of ours — it is a verdict computed elsewhere (against landed transactions) and
# handed in, so it is validated on the way in and normalized into :data:`ChainStatus`,
# which IS ours. Pinned with ``==`` in tests against the producer's own set, so the two
# drifting apart is a visible, failing edit rather than a silent downgrade to
# NOT_EVALUATED. Anything outside this set — absent, misspelled, a bool, an object — is
# NOT_EVALUATED: untrusted input never argues its way into confidence.
CHAIN_VERDICTS = frozenset({"AGREE", "DISAGREE", "NOT_EVALUATED"})


@dataclass(frozen=True)
class ChainStep:
    """One placed step of a chain plan: the instruction, its position in the derived
    order, and its FULL derive plan — flagged accounts included, never trimmed."""

    instruction: str
    position: int
    inputs: tuple[str, ...]
    derive_plan: tuple[DeriveStep, ...]
    gaps: tuple[GapSpec, ...]


@dataclass(frozen=True)
class ChainLink:
    """One realized edge: the shared account that actually links two placed steps.

    ``provenance`` is the WEAKER of the linking account's tag on the two endpoints —
    a link is only as strong as the weakest end of it, and a ``flagged`` end means the
    dependency cannot be carried at all (the plan says so instead of ordering around
    it). ``basis``/``refuted_by`` ride along from the declared edge."""

    account: str
    produces: str
    consumes: str
    kind: ChainEdgeKind
    provenance: AccountProvenance
    basis: str
    refuted_by: str


@dataclass(frozen=True)
class ChainPlan:
    """The multi-instruction verdict for a start point.

    NOTHING here has a default — ``ChainPlan()`` raises ``TypeError`` on purpose.
    ``status`` in particular must be an explicit decision: a chain status that could
    fall into a zero value is the exact defect the codebase has paid for repeatedly
    (a verdict type with no representation for "not evaluated" reads as approval).
    Consumers must test ``status == "ordered"`` positively — never truthiness, never a
    ``!=`` denylist; all three members are non-empty, truthy strings.

    An UNRESOLVABLE chain still returns every step with its full derive plan and one
    :class:`GapSpec` per offender in ``unresolved`` — never ``steps=()``, never
    ``None``, never a raise. ``steps=()`` means one thing only: no chain is declared
    for this start (``status == "not_evaluated"``, and the note says so).
    """

    name: str
    status: ChainStatus
    verdict: str  # normalized into CHAIN_VERDICTS; the axis ``status`` summarizes
    steps: tuple[ChainStep, ...]
    links: tuple[ChainLink, ...]
    unresolved: tuple[GapSpec, ...]
    note: str


# Whether a plan can be RUN. Deliberately NOT a bool and deliberately not called
# "ready": ``no_blockers_found`` means this router found nothing left to obtain, which is
# weaker than a promise that the call will land, and ``not_assessed`` exists so a
# hand-built StartPoint can never read as approval it never earned.
ReadinessStatus = Literal["blocked", "no_blockers_found", "not_assessed"]


@dataclass(frozen=True)
class Readiness:
    """What ``gaps`` never answered: is there anything left to obtain before building?

    A LIVE AGENT SESSION IS WHY THIS EXISTS. It read ``gaps: []`` as "this plan is ready
    to run" while five of the instruction's eight accounts had no obtainable value. It
    was not misreading: ``gaps`` grades DERIVATION RECIPES, so an empty list means every
    recipe here is trusted, and says nothing whatsoever about whether the caller can
    produce the values those recipes consume. Both facts are true at once and they are
    different facts, so this is a separate field rather than a redefinition of ``gaps``.

    ``must_obtain`` is the union of the two things a caller still has to go and find: the
    plan's accounts that NOTHING here derives (``supplied_by == "caller"``), and the
    declared ``inputs``.
    """

    status: ReadinessStatus
    must_obtain: tuple[str, ...]
    note: str


#: The honest default for a :class:`StartPoint` built outside the router (tests, fixtures,
#: an eval harness). "Not assessed" is a third value on purpose — a missing assessment and
#: a passed one must never be the same value.
READINESS_NOT_ASSESSED = Readiness(
    status="not_assessed",
    must_obtain=(),
    note="this start point was not built by the router, so nothing assessed it",
)


_READINESS_NOTE = (
    "`gaps` grades DERIVATION RECIPES, not readiness: an empty `gaps` means every recipe "
    "in this plan is trusted, NOT that the plan can run. Everything in `must_obtain` "
    "still has no value here — the accounts among them are slots the program NAMES and "
    "nothing derives, and the rest are the declared inputs. When the value you need is a "
    "fact about an existing account of this program (which admin, which id), "
    "`read_accounts` reads it off the chain and proves it by re-derivation."
)


@dataclass(frozen=True)
class StartPoint:
    """One ranked starting point. ``kind`` is honest: ``start`` = a runnable plan —
    an executable plan intent, or a step of a DECLARED lifecycle chain with a full
    derive plan and a builder URL; ``surface`` = a wired program without a start for
    this ask (start from its derive/graph tools); ``guess`` = below the retrieval
    floor — a closest-candidate, NOT a start.

    ``chain`` is REQUIRED (no default): a start that is one call of a multi-call
    lifecycle and does not say so is the failure this field exists to prevent."""

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
    chain: ChainPlan
    #: Defaulted so a StartPoint built outside the router stays constructible — and
    #: defaulted to NOT ASSESSED, never to a pass.
    readiness: Readiness = READINESS_NOT_ASSESSED
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


# --- the DECLARED chains (authored data) -----------------------------------------

# let_me_buy (letmebuy.app) — a storefront program whose EIGHT instructions all take
# the same `receipts` account, writable. That is what makes it a lifecycle rather than
# eight unrelated calls, and it is precisely the join an IDL loses: the IDL states each
# instruction's accounts and args, never that one call must follow another. Both flows
# below are hand-authored from the packaged config's own account/arg lists (the same
# trust class as StartSpec) and are asserted in tests/test_let_me_buy_chain.py.
_RECEIPTS_BASIS = (
    "the packaged program config states one PDA per store, receipts = "
    "['receipts', store_name], and lists it as account 0 of BOTH instructions — the "
    "same address for the same store name, so the two calls address one account"
)

_LET_ME_BUY_SURFACE_NAMED = (
    "authority",
    "signer",
    "mint",
    "token_program",
    "system_program",
    "associated_token_program",
)

_OPEN_A_STORE = ChainSpec(
    name="open_a_store",
    api_id="let_me_buy",
    summary=(
        "open a store, then list what it sells — initialize creates the one store "
        "account every later call addresses; add_product writes a product into it"
    ),
    steps=(
        ChainStepSpec(
            instruction="initialize",
            # Every word of a card description is retrieval surface, so incidental
            # prose is a liability: the first draft of this card said "Open a NEW
            # store … BEFORE any product is listed", and those two words — which name
            # nothing — were enough to serve `initialize` as a RUNNABLE start for
            # "purchase some of that new memecoin before it bonds" (two rare terms
            # clear the corroboration floor: rarity is not distinctiveness). They are
            # gone; the sentence says the same thing with terms that name something.
            description=(
                "Open a store on let_me_buy: create the storefront account a "
                "merchant sells from, named by the store name buyers scan a QR code "
                "to reach. Run this ONCE per store — every later call of the store's "
                "lifecycle addresses this same account, derived from the store name, "
                "and nothing can be listed or sold until it exists."
            ),
            inputs=("store_name", "authority"),
            spec=StartSpec(
                accounts=("receipts", "authority", "system_program"),
                surface_named=_LET_ME_BUY_SURFACE_NAMED,
            ),
        ),
        ChainStepSpec(
            instruction="add_product",
            description=(
                "List a product in a let_me_buy store: its name, its price, and the "
                "SPL token mint that price is denominated in. The store must already "
                "be open, and the mint fixes the currency for every purchase of that "
                "product."
            ),
            inputs=("store_name", "authority", "name", "price", "mint"),
            spec=StartSpec(
                accounts=("receipts", "authority", "mint", "system_program"),
                surface_named=_LET_ME_BUY_SURFACE_NAMED,
            ),
        ),
    ),
    edges=(
        ChainEdge(
            produces="initialize",
            consumes="add_product",
            account="receipts",
            kind="initializes",
            basis=(
                _RECEIPTS_BASIS
                + "; initialize is the only instruction that creates it (the others "
                "take it as an existing, writable account)"
            ),
            refuted_by=(
                "an add_product that lands against a store name never initialized — "
                "or an IDL/source revision in which add_product also creates the "
                "account (init_if_needed), which would make this edge ordering "
                "advice rather than a requirement"
            ),
        ),
    ),
)

_SELL_AND_DELIVER = ChainSpec(
    name="sell_and_deliver",
    api_id="let_me_buy",
    summary=(
        "sell, then deliver — make_purchase writes a receipt into the store account "
        "and mark_as_delivered settles it by the receipt_id that purchase created"
    ),
    steps=(
        ChainStepSpec(
            instruction="make_purchase",
            description=(
                "Buy one listed product from a let_me_buy store and pay the merchant "
                "in their SPL token — a beer, a coffee, an espresso or a cappuccino "
                "at the bar counter, ordered by scanning the store's QR code. Writes "
                "a receipt into the store account, which the merchant later marks "
                "delivered."
            ),
            inputs=(
                "store_name",
                "product_name",
                "table_number",
                "signer",
                "authority",
                "mint",
            ),
            spec=StartSpec(
                accounts=(
                    "receipts",
                    "signer",
                    "authority",
                    "mint",
                    "sender_token_account",
                    "recipient_token_account",
                    "token_program",
                    "system_program",
                    "associated_token_program",
                ),
                surface_named=_LET_ME_BUY_SURFACE_NAMED,
                gaps=(),
            ),
        ),
        ChainStepSpec(
            instruction="mark_as_delivered",
            description=(
                "Mark a purchase receipt as delivered: the merchant confirms the "
                "order was handed to the buyer, addressed by the receipt id the "
                "purchase wrote into the store account."
            ),
            inputs=("store_name", "receipt_id", "authority"),
            spec=StartSpec(
                accounts=("receipts", "authority"),
                surface_named=_LET_ME_BUY_SURFACE_NAMED,
            ),
        ),
    ),
    edges=(
        ChainEdge(
            produces="make_purchase",
            consumes="mark_as_delivered",
            account="receipts",
            kind="produces",
            basis=(
                _RECEIPTS_BASIS
                + "; and mark_as_delivered's `receipt_id: u64` selects an entry of "
                "the receipts vec that only a landed make_purchase writes — a DATA "
                "dependency, not merely an ordering preference"
            ),
            refuted_by=(
                "a mark_as_delivered that settles a receipt_id no make_purchase "
                "wrote — or a source revision where receipt_id is caller-chosen "
                "rather than assigned by the purchase"
            ),
        ),
    ),
)

# api_id → the chains declared for that program. Keyed, not scanned, so an unwired
# program can never pick up another program's chain.
# jurassic_fi (Jurassic Finance) — a real-world-asset sale: a museum-grade Triceratops
# skull fractionalised as $TRCH1. Contribute USDC while the sale is open, claim $TRCH1
# after it settles. Both steps are hand-authored from the LIVE IDL's own account lists
# (the same trust class as StartSpec) and asserted in tests/test_jurassic_chain.py.
#
# The join is `user_position`: contribute writes it, claim reads it. The IDL states each
# instruction's accounts; it never states that one call must follow the other. That is
# exactly the edge an IDL loses and a lifecycle needs.
_USER_POSITION_BASIS = (
    "the packaged program config states user_position = ['user_position', launch, "
    "contributor], and the live IDL lists it [writable] on BOTH contribute and claim — "
    "the same address for the same contributor in the same launch, so the two calls "
    "address one account"
)

_JURASSIC_SURFACE_NAMED = (
    "contributor",
    "payment_mint",
    "contributor_payment_account",
    "token_mint",
    "contributor_token_account",
    "token_program",
    "system_program",
)

_CONTRIBUTE_THEN_CLAIM = ChainSpec(
    name="contribute_then_claim",
    api_id="jurassic_fi",
    summary=(
        "contribute to a token sale, then claim the tokens it owes you — contribute "
        "opens the position the sale records your stake in; claim pays that position "
        "out once the sale has settled"
    ),
    steps=(
        ChainStepSpec(
            instruction="contribute",
            # Kept deliberately spare. Every word here is retrieval surface, and the
            # let_me_buy cards already taught this repo that incidental prose ("new",
            # "before") serves a card as a RUNNABLE start for intents it has nothing
            # to do with. Rarity is not distinctiveness.
            description=(
                "Contribute USDC to a Jurassic Finance token sale and receive a claim "
                "on $TRCH1. The sale must be open; the payment mint is fixed by the "
                "launch, and the amount is in that mint's base units."
            ),
            inputs=(
                "admin",
                "launch_id",
                "requested_amount",
                "min_accepted_amount",
                "contributor",
            ),
            spec=StartSpec(
                accounts=(
                    "launch",
                    "user_position",
                    "payment_vault",
                ),
                surface_named=_JURASSIC_SURFACE_NAMED,
                recovered={
                    "launch": (
                        "seeded on ['launch', launch.admin, launch.launch_id] — two "
                        "fields of the account being derived, so the surface cannot "
                        "derive it from itself. In contribute the id arrives as the "
                        "ARGUMENT params.launch_id at width 8 (u64); read at u8 it "
                        "derives a different, perfectly valid address"
                    )
                },
            ),
        ),
        ChainStepSpec(
            instruction="claim",
            description=(
                "Claim the $TRCH1 a settled Jurassic Finance sale owes a contributor, "
                "paying out the position that contribute recorded. The sale must have "
                "settled, and only the contributor named in the position can claim it."
            ),
            inputs=("admin", "launch_id", "contributor"),
            spec=StartSpec(
                accounts=(
                    "launch",
                    "user_position",
                    "token_vault",
                    "dust_token_account",
                ),
                surface_named=_JURASSIC_SURFACE_NAMED,
                gaps=(
                    GapSpec(
                        name="dust_token_account",
                        note=(
                            "reads as clean in the IDL but is not buildable from it — "
                            "the seed the recipe needs is carried by a sibling account, "
                            "not by this one"
                        ),
                    ),
                ),
            ),
        ),
    ),
    edges=(
        ChainEdge(
            produces="contribute",
            consumes="claim",
            account="user_position",
            kind="initializes",
            basis=_USER_POSITION_BASIS,
            refuted_by=(
                "a claim that lands against a launch the contributor never contributed "
                "to, or a user_position readable by claim without contribute having "
                "written it"
            ),
        ),
    ),
)


_DECLARED_CHAINS: dict[str, tuple[ChainSpec, ...]] = {
    "let_me_buy": (_OPEN_A_STORE, _SELL_AND_DELIVER),
    "jurassic_fi": (_CONTRIBUTE_THEN_CLAIM,),
}


def declared_chains(api_id: str) -> tuple[ChainSpec, ...]:
    """The DECLARED lifecycle chains of one program (empty when none)."""
    return _DECLARED_CHAINS.get(api_id, ())


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
    # WHOSE WORD the config behind this card is (``gecko.provider_config.ConfigTrust``).
    # Defaults to ``packaged`` deliberately, and it is not a fail-open: the fail-closed
    # decision is made ONCE, at the loader, where a config of unknown origin already
    # arrives capped. A card is built from an artifact that has been through that door,
    # and defaulting to ``external`` here would demote the surface/catalog cards that have
    # no ProgramSpec at all and whose steps come from data we hold ourselves.
    trust: ConfigTrust = "packaged"
    # the DECLARED chain this card is a step of (``None`` = a standalone start).
    chain: str | None = None


#: (program, instruction) -> the Gecko MCP tool that plans AND CHECKS that call.
#:
#: `next_tool` otherwise carries a wired plan INTENT name (`plan_swap`), which is a
#: different kind of thing — an intent is a label on a derive plan, a tool is something an
#: agent can call. For `make_purchase` there is no intent, so this field was `null` and the
#: note said "no Gecko plan tool is wired", which is false: `prepare_purchase` derives
#: offline, refuses a self-paying plan before building, patches a fresh blockhash,
#: simulates the exact bytes and binds a receipt. Routing past it to a raw builder is the
#: strictly worse path, recommended by our own router.
#:
#: Empty for everything else ON PURPOSE — a name here is a promise that the tool exists and
#: is served, and a fabricated one is worse than a null.
_GECKO_TOOL_FOR: dict[tuple[str, str], str] = {
    ("let_me_buy", "make_purchase"): "prepare_purchase",
}


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
        chains = declared_chains(api_id)
        for chain in chains:
            for chain_step in chain.steps:
                program_recovered.update(chain_step.spec.recovered)
        # The surface card must not claim "no start is wired" when chain steps are
        # start points; they are named here alongside any plan intents.
        start_point_names = tuple(program.intents) + tuple(
            step.instruction for chain in chains for step in chain.steps
        )
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
                wired_intents=start_point_names,
                origins=dict(program.pda_origins),
                trust=program.trust,
            )
        )
        # one card per DECLARED chain step (a runnable start with no plan callable:
        # Gecko derives the accounts, Orquestra's /build assembles the instruction)
        for chain in chains:
            for chain_step in chain.steps:
                cards.append(
                    _Card(
                        kind="start",
                        api_id=api_id,
                        program_id=program.program_id,
                        instruction=chain_step.instruction,
                        intent_name=None,
                        inputs=chain_step.inputs,
                        accounts=chain_step.spec.accounts,
                        pdas=dict(program.pdas),
                        spec=chain_step.spec,
                        notes=notes,
                        execute_url=(
                            f"{project_base}/instructions/{chain_step.instruction}/build"
                            if project_base
                            else None
                        ),
                        operation=_operation(
                            operation_id=chain_step.instruction,
                            path=f"/{api_id}/{chain_step.instruction}",
                            summary=_first_sentence(chain_step.description),
                            description=(
                                f"{chain_step.description} "
                                f"inputs: {', '.join(chain_step.inputs)}"
                            ),
                            tags=[api_id, chain_step.instruction],
                        ),
                        origins=dict(program.pda_origins),
                        trust=program.trust,
                        chain=chain.name,
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
                    trust=program.trust,
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
    # A recipe is what makes an address derivable. Without a node there is nothing to run,
    # however well the surface names the slot — and that is the fact an empty `gaps` was
    # never reporting.
    supplier: AccountSupplier = "derived" if node is not None else "caller"
    if resolver_seeds and not has_recipe:
        return DeriveStep(
            account=name,
            provenance="flagged",
            note=overlay_why.get(name, ""),
            resolver=resolver_note,
            supplied_by="caller",
        )
    if name in recovered:
        return DeriveStep(
            account=name,
            provenance="recovered",
            note=recovered[name],
            resolver=resolver_note,
            supplied_by=supplier,
        )
    if name in overlay_pdas:
        return DeriveStep(
            account=name,
            provenance="recovered",
            note=overlay_why.get(name, "beyond-surface recipe (manual overlay)"),
            resolver=resolver_note,
            supplied_by=supplier,
        )
    if has_recipe:
        return DeriveStep(
            account=name,
            provenance="recovered",
            note="a declared control-plane read recipe resolves this seed at plan time",
            resolver=resolver_note,
            supplied_by=supplier,
        )
    if node is not None or name in surface_named:
        return DeriveStep(account=name, provenance="extracted", supplied_by=supplier)
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

#: The same cap, a different REASON — and the reason is agent-facing, so it may not be
#: borrowed. An external config that simply said nothing has asserted no invalid tier;
#: telling an agent it did would be a false sentence about a provider's file.
_UNVERIFIED_CONFIG_NOTE = (
    "this recipe comes from a config we did not author and have not verified — "
    "reported as a gap on purpose: a provider's assertion about their own program is "
    "not evidence, however confidently the file states it"
)


def _apply_origin_cap(
    step: DeriveStep,
    origins: Mapping[str, ProgramProvenanceTier | None],
    trust: ConfigTrust = "packaged",
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
        # `trust` only chooses WHICH true sentence to tell — the cap itself is identical.
        note = _INVALID_ORIGIN_NOTE if trust == "packaged" else _UNVERIFIED_CONFIG_NOTE
        return replace(step, provenance="flagged", note=note)
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
            card.trust,
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


# --- the chain rail (the account-level rail, one level up) ------------------------

_NO_CHAIN_NOTE = (
    "no lifecycle chain is DECLARED for this program — this start is a single "
    "instruction, so the chain axis was not evaluated (it is not a chain that passed)"
)

# Why an unsupplied verdict is not an oversight: the chain-agreement evidence (a derived
# account set compared against landed transactions) deliberately does NOT live in the
# packaged config — ProgramSpec has no field for it and the loader drops unknown keys, so
# a config-level pin would be an unenforced claim. It is handed in by the caller instead,
# and its ABSENCE is the honest default: not evaluated, therefore not confident.
_NO_VERDICT_NOTE = (
    "no chain-agreement verdict was supplied for this chain, so the account sets were "
    "never checked against landed transactions — the plan is reported, never claimed "
    "executable. A chain verdict is lifted from EVIDENCE by the caller of the library "
    "(find_start's chain_verdicts argument) — deliberately not by anything an agent can "
    "pass through a tool, because a verdict an agent can assert is one it can fabricate"
)
_DISAGREE_NOTE = (
    "the chain-agreement verdict is DISAGREE: landed transactions contradict the "
    "account set this plan derives — every step is still reported, and none of it may "
    "be executed as given"
)


def _normalize_verdict(raw: object) -> str:
    """Accept a chain-agreement verdict from an UNTRUSTED caller.

    Anything outside :data:`CHAIN_VERDICTS` — absent, misspelled, a bool, an object —
    normalizes to ``NOT_EVALUATED``. It never raises: a malformed verdict must degrade
    to "we did not check", never to a refusal that never happened, and never to trust.
    """
    return raw if isinstance(raw, str) and raw in CHAIN_VERDICTS else "NOT_EVALUATED"


def _link_of(
    edge: ChainEdge, plans: Mapping[str, tuple[DeriveStep, ...]]
) -> tuple[ChainLink | None, GapSpec | None]:
    """Realize one declared edge against the two endpoints' derive plans.

    Returns ``(link, gap)`` — exactly one of them. The edge is only a link if BOTH
    endpoints actually carry the shared account; otherwise the declaration is wrong (or
    the config moved under it) and that is a gap, never a silently dropped edge.
    """
    missing = [name for name in (edge.produces, edge.consumes) if name not in plans]
    if missing:
        return None, GapSpec(
            edge.account,
            f"declared {edge.kind} edge {edge.produces} → {edge.consumes} names "
            f"instruction(s) {', '.join(missing)} that are not steps of this chain",
        )
    tags: list[AccountProvenance] = []
    for instruction in (edge.produces, edge.consumes):
        step = next(
            (s for s in plans[instruction] if s.account == edge.account),
            None,
        )
        if step is None:
            return None, GapSpec(
                edge.account,
                f"declared {edge.kind} edge {edge.produces} → {edge.consumes} is "
                f"carried by {edge.account}, which {instruction} does not list — the "
                "chain declaration and the account sets disagree",
            )
        tags.append(step.provenance)
    weakest = min(tags, key=lambda tag: _CLAIM_STRENGTH.get(tag, 0))
    if weakest == "flagged":
        return None, GapSpec(
            edge.account,
            f"the account linking {edge.produces} → {edge.consumes} is FLAGGED on at "
            "least one endpoint, so the dependency cannot be carried — the steps are "
            "reported, the link is not claimed",
        )
    return (
        ChainLink(
            account=edge.account,
            produces=edge.produces,
            consumes=edge.consumes,
            kind=edge.kind,
            provenance=weakest,
            basis=edge.basis,
            refuted_by=edge.refuted_by,
        ),
        None,
    )


def _chain_plan(
    card: _Card, cards: Sequence[_Card], verdicts: Mapping[str, object] | None
) -> ChainPlan:
    """Plan the DECLARED chain this card is a step of — the account-level rail
    EXTENDED, never bypassed.

    Each step's accounts still go through :func:`_derive_plan` (and therefore through
    ``derivation_order_with_cycle``, which flags an unorderable account rather than
    dropping it); the instructions themselves go through
    :func:`~gecko.program_graph.chain_order_with_cycle`, the same ``(order, cycle)``
    contract one level up. ``ordered`` requires ALL of: every instruction placed, every
    declared link carried by an unflagged shared account on both ends, and an explicit
    ``AGREE`` on the chain-agreement axis. Anything else is reported — ``unresolved``
    when something was checked and refused, ``not_evaluated`` when it was never checked.
    """
    spec = next((c for c in declared_chains(card.api_id) if c.name == card.chain), None)
    if spec is None:
        return ChainPlan(
            name="",
            status="not_evaluated",
            verdict="NOT_EVALUATED",
            steps=(),
            links=(),
            unresolved=(),
            note=_NO_CHAIN_NOTE,
        )

    by_instruction = {
        c.instruction: c
        for c in cards
        if c.api_id == card.api_id and c.chain == spec.name and c.instruction
    }
    order, cyclic = chain_order_with_cycle(
        [s.instruction for s in spec.steps],
        [(e.produces, e.consumes) for e in spec.edges],
    )
    plans: dict[str, tuple[DeriveStep, ...]] = {}
    steps: list[ChainStep] = []
    unresolved: list[GapSpec] = []
    for position, instruction in enumerate(order, 1):
        step_card = by_instruction.get(instruction)
        step_spec = next(s for s in spec.steps if s.instruction == instruction)
        if step_card is None:
            # a declared step with no card cannot be derived — reported, never dropped
            unresolved.append(
                GapSpec(
                    instruction,
                    "declared as a step of this chain but no card was built for it — "
                    "the chain is reported without its derive plan rather than "
                    "silently shortened",
                )
            )
            plan: tuple[DeriveStep, ...] = ()
        else:
            plan = _derive_plan(step_card)
        plans[instruction] = plan
        steps.append(
            ChainStep(
                instruction=instruction,
                position=position,
                inputs=step_spec.inputs,
                derive_plan=plan,
                gaps=_gaps_of(step_card, plan) if step_card else (),
            )
        )
    for instruction in sorted(cyclic):
        unresolved.append(
            GapSpec(
                instruction,
                "unorderable: dependency cycle among "
                f"{', '.join(sorted(cyclic))} — this instruction's position in the "
                "chain is arbitrary, so the order above must not be executed",
            )
        )

    links: list[ChainLink] = []
    for edge in spec.edges:
        link, gap = _link_of(edge, plans)
        if link is not None:
            links.append(link)
        if gap is not None:
            unresolved.append(gap)

    verdict = _normalize_verdict((verdicts or {}).get(spec.name))
    if unresolved:
        status: ChainStatus = "unresolved"
        note = (
            f"{spec.summary}. UNRESOLVED: {len(unresolved)} unresolved item(s) below — "
            "every step is still reported with its full derive plan"
        )
    elif verdict == "DISAGREE":
        status, note = "unresolved", f"{spec.summary}. {_DISAGREE_NOTE}"
    elif verdict != "AGREE":
        status, note = "not_evaluated", f"{spec.summary}. {_NO_VERDICT_NOTE}"
    else:
        status = "ordered"
        note = (
            f"{spec.summary}. Every step is placed, every declared link is carried by "
            "a shared account both steps hold, and the chain-agreement verdict is "
            "AGREE (the derived account set matched landed transactions). ORDERED is "
            "not authorization: nothing here signs or broadcasts"
        )
    return ChainPlan(
        name=spec.name,
        status=status,
        verdict=verdict,
        steps=tuple(steps),
        links=tuple(links),
        unresolved=tuple(unresolved),
        note=note,
    )


def _readiness_of(inputs: tuple[str, ...], plan: tuple[DeriveStep, ...]) -> Readiness:
    """What is left to obtain: accounts nothing derives, then the declared inputs.

    Order matters to a reader — the accounts come first because they are the half a
    caller does NOT expect to be missing, having just been handed a derive plan that
    lists them.
    """
    accounts = tuple(
        dict.fromkeys(step.account for step in plan if step.supplied_by == "caller")
    )
    must_obtain = tuple(dict.fromkeys(accounts + inputs))
    return Readiness(
        status="blocked" if must_obtain else "no_blockers_found",
        must_obtain=must_obtain,
        note=_READINESS_NOTE,
    )


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
    # Bare numbers carry how MANY, never WHICH — no program or instruction is named by
    # a digit string, so here (and only here) they are not evidence. Measured: "buy 2
    # Espressos" routed to pumpfun/buy as a runnable START because the `2` matched the
    # `2` that falls out of tokenizing `bonding_curve_v2` in that card's implementation
    # notes. The API-operation side KEEPS digits ("kind 77" legitimately selects
    # `getWidgetKind77`), which is why this filter lives in this query builder and not
    # in the shared `content_tokens`. The scorer sees the same filtered set — the tokens
    # below ARE the query it ranks with — so the gate and the ranking cannot drift.
    return {
        t
        for t in normalize_query(_tokens(intent[:MAX_INTENT_CHARS]))
        if not t.isdigit()
    }


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
    card: _Card,
    score: int,
    why: tuple[str, ...],
    kind: StartKind,
    *,
    cards: Sequence[_Card] = (),
    verdicts: Mapping[str, object] | None = None,
) -> StartPoint:
    plan = _derive_plan(card)
    chain = _chain_plan(card, cards, verdicts)
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
                f"this program's start points ({', '.join(card.wired_intents)}) are "
                "separate entries; this entry is its raw surface "
                "(get_program_graph / derive_pda). " + _first_sentence(card.notes)
            )
        else:
            note = (
                "no executable plan intent is wired for this program yet — start "
                "from its surface tools (get_program_graph / derive_pda). "
                + _first_sentence(card.notes)
            )
    elif card.chain:
        position = next(
            (s.position for s in chain.steps if s.instruction == card.instruction), 0
        )
        note = (
            f"step {position} of {len(chain.steps)} in the DECLARED "
            f"'{chain.name}' chain — no Gecko plan tool is wired for this "
            "instruction: the derive plan IS the plan, and Orquestra's /build "
            "assembles it. " + _first_sentence(card.notes)
        )
    else:
        note = _first_sentence(card.notes)
    return StartPoint(
        kind=kind,
        program=card.api_id,
        program_id=card.program_id,
        instruction=card.instruction,
        next_tool=(
            _GECKO_TOOL_FOR.get((card.api_id, card.instruction))
            if card.instruction
            else None
        )
        or card.intent_name,
        score=score,
        why=why,
        inputs=card.inputs,
        derive_plan=plan,
        preludes=card.spec.preludes if card.spec else (),
        gaps=_gaps_of(card, plan),
        execute=execute,
        serve=f"gecko-orquestra --program {card.api_id} --stdio",
        chain=chain,
        readiness=_readiness_of(card.inputs, plan),
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
    chain_verdicts: Mapping[str, object] | None = None,
) -> FindStartResult:
    """Route ``intent`` to ranked start points across the wired programs.

    ``program`` is an optional hint (a wired api_id narrows the search; an
    unwired name is looked up in ``catalog_pages``). ``catalog_pages`` are
    already-fetched, already-validated catalog pages (the caller controls the
    fetch policy; this function is pure and offline). ``on_miss`` is the opt-in
    instrumentation seam — called with a CATEGORICAL :class:`MissRecord` when
    nothing clears the floor. Off by default; never receives the intent text.

    ``chain_verdicts`` maps a DECLARED chain's name to its chain-agreement verdict
    (``AGREE`` / ``DISAGREE`` / ``NOT_EVALUATED``) — evidence computed elsewhere,
    against landed transactions, and handed in here. Omitting it is safe by
    construction: an unsupplied or unrecognised verdict is ``NOT_EVALUATED``, and a
    chain plan is ``ordered`` only on an explicit ``AGREE``.
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
            # Naming an action is not describing a task. `named` used to be sufficient
            # on its own, and it served "buy a house" as pumpfun.buy on why=('buy',) —
            # along with "sell my car", "deliver my mail" and "swap my shift with a
            # coworker", 6 of the golden set's 12 out-of-scope rows, every one of them
            # matching a single word that was the instruction's own name.
            #
            # The verb says what to do and says nothing about WHAT is acted on, so a
            # runnable start now needs one matched term BEYOND the name. Note what could
            # not have fixed this: "swap my shift with a coworker" and "swap sol for usdc"
            # match the identical term set {swap}. No rule over the MATCHED words separates
            # them — the evidence is in what the caller said beyond the verb.
            identity = _identity_terms(card)
            named = bool(matched & identity) and bool(matched - identity)
            corroborated = len(matched & distinguishing) >= 2
            # Only RUNNABLE cards are gated. A `surface` card already says "start from
            # this program's derive tools", which is a hedge, not a plan — demoting it
            # would erase the useful middle answer ("this program is plausibly relevant")
            # without preventing any harm.
            gated = card.kind == "start" and not (named or corroborated)
            kind = "guess" if gated else card.kind
            points.append(
                _start_point(
                    card,
                    se.score,
                    why,
                    kind=kind,
                    cards=cards,
                    verdicts=chain_verdicts,
                )
            )
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
        _start_point(
            by_op[id(se.entry.operation)],
            0,
            (),
            kind="guess",
            cards=cards,
            verdicts=chain_verdicts,
        )
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
    # OUTSIDE the bracket on purpose: the bracket is the provenance tag and readers
    # (and tests) pin it. Who supplies the address is a second axis, not a fourth tag.
    supplier = "  <- YOU SUPPLY THIS" if step.supplied_by == "caller" else ""
    line = f"    {index:>2}. {step.account:<28} [{tag}]{supplier}"
    lines = [line]
    if step.note:
        lines.append(f"        {step.note}")
    if step.resolver:
        lines.append(f"        resolver: {step.resolver}")
    return lines


# The rendered label of a chain status. A status no output surface prints is a gate
# with no caller — the CLI shows this verbatim.
_CHAIN_LABEL: dict[ChainStatus, str] = {
    "ordered": "ORDERED",
    "unresolved": "UNRESOLVED",
    "not_evaluated": "NOT EVALUATED",
}


def _render_chain(chain: ChainPlan) -> list[str]:
    if not chain.steps:
        return []  # no chain declared — the single-instruction note already says so
    lines = [
        f"    lifecycle chain: {chain.name} "
        f"[{_CHAIN_LABEL[chain.status]}] (agreement: {chain.verdict})",
        f"        {chain.note}",
    ]
    for step in chain.steps:
        accounts = ", ".join(s.account for s in step.derive_plan) or "(no derive plan)"
        lines.append(f"      {step.position}. {step.instruction} — {accounts}")
    for link in chain.links:
        lines.append(
            f"      link: {link.account} [{link.provenance}] "
            f"{link.produces} --{link.kind}--> {link.consumes}"
        )
        lines.append(f"            basis:      {link.basis}")
        lines.append(f"            refuted by: {link.refuted_by}")
    for gap in chain.unresolved:
        lines.append(f"      ! unresolved {gap.name}: {gap.note}")
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
        lines.extend(_render_chain(point.chain))
        if point.preludes:
            lines.append("    preludes (DECLARED):")
            for prelude in point.preludes:
                lines.append(f"      - {prelude.kind}: {prelude.note}")
        if point.gaps:
            lines.append("    flagged gaps (honest — resolve before building):")
            for gap in point.gaps:
                lines.append(f"      ! {gap.name}: {gap.note}")
        if point.readiness.status != "not_assessed":
            lines.append(f"    readiness:  {point.readiness.status.upper()}")
            if point.readiness.must_obtain:
                lines.append(
                    "      still to obtain: " + ", ".join(point.readiness.must_obtain)
                )
            lines.append(f"      {point.readiness.note}")
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
