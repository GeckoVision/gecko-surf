"""Every dataclass and Literal of the any-query contract. The only definition.

Money is integers only: USDC in micro units (6 decimals); upstream prices in cents, as the
Orthogonal core keeps them (cents -> micro is x10_000). Times are ISO-8601 UTC strings,
which avoids the pymongo naive-datetime trap noted in ``registry/class_wallet_store.py``
and keeps lexical order equal to time order.

``FieldState`` and ``Agreement`` come from the supplier core, ``CallMode`` from
``gecko.modes`` and ``Catalog`` from ``gecko.servicemap.types``; they are imported,
never redeclared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from gecko.modes import CallMode
from gecko.orthogonal.supplier.fields import Agreement, FieldState
from gecko.servicemap.types import Catalog

__all__ = [
    "Agreement",
    "Bundle",
    "CallMode",
    "CandidateEndpoint",
    "CandidateSet",
    "Catalog",
    "Compare",
    "Dataset",
    "DatasetChangeKind",
    "DatasetCell",
    "DatasetField",
    "DatasetRow",
    "DatasetVersion",
    "FactRow",
    "FieldState",
    "InputBinding",
    "InputFrom",
    "LEDGER_KINDS",
    "LedgerEntry",
    "LedgerKind",
    "PAID",
    "PAYMENT_STATUSES",
    "PAY_WITH",
    "PayWith",
    "Payment",
    "PaymentStatus",
    "PlanDraft",
    "PlanRequest",
    "PlannedField",
    "PlannedSource",
    "PlannerKind",
    "PriceFrom",
    "PriorValue",
    "Quote",
    "QuotePriceFrom",
    "QueryPlan",
    "RUN_STATUSES",
    "RunRecord",
    "RunStatus",
    "SNAPSHOT_STATUSES",
    "STORAGES",
    "SnapshotStatus",
    "Storage",
    "Unplanned",
    "USDC_MICRO_PER_CENT",
]

# ``Catalog`` lives in gecko.servicemap.types (the registry's catalogs); re-exported here.
#: What a source's terms say about storing its values. METADATA ONLY since the founder
#: ruling of 8 Oct: every source's values are stored for the customer who asked, and no
#: label drops a source or nulls a value.
Storage = Literal["allowed", "unknown", "forbidden"]
Compare = Literal["text", "number", "date", "presence"]
InputFrom = Literal["subject", "literal"]
PlannerKind = Literal["model", "fake"]
PayWith = Literal["USDC", "SOL"]
RunStatus = Literal["running", "complete", "partial", "stopped", "failed"]
#: ``pending`` is a charge that may be on chain: written before signing, and kept with
#: its signature when the transaction was broadcast but is not yet visible. It is
#: re-checked by that signature and never charged again (the ledger holds one charge per
#: run). ``credited`` is a run that was paid and delivered no fresh or repaired fact: its
#: cost is a ``run_credit`` on the next run, and its rows are withheld.
PaymentStatus = Literal[
    "not_due", "pending", "landed", "failed", "not_charged", "credited"
]
#: ``run_credit``: what an undelivered run cost, given back as a discount on the next
#: run. A ledger line only, never a transfer. ``settlement``: Gecko's own transaction that
#: swapped a landed SOL charge to USDC and forwarded it to the treasury (option B, 9 Oct).
#: It belongs to the run whose SOL it settled; it never moves the user's money, and a
#: failed one is retried, never charged again. Rebates are matched to ITS slot.
LedgerKind = Literal["charge", "discount", "rebate_credit", "run_credit", "settlement"]
PriceFrom = Literal["live", "pinned"]
QuotePriceFrom = Literal["live", "pinned", "mixed"]
#: A dataset version's Parquet snapshot: ``pending`` until the sink holds it.
SnapshotStatus = Literal["written", "pending"]

STORAGES: tuple[Storage, ...] = get_args(Storage)
PAY_WITH: tuple[PayWith, ...] = get_args(PayWith)
RUN_STATUSES: tuple[RunStatus, ...] = get_args(RunStatus)
PAYMENT_STATUSES: tuple[PaymentStatus, ...] = get_args(PaymentStatus)
LEDGER_KINDS: tuple[LedgerKind, ...] = get_args(LedgerKind)
SNAPSHOT_STATUSES: tuple[SnapshotStatus, ...] = get_args(SnapshotStatus)
#: A run with one of these may make paid calls, and once finished hand out its rows:
#: the charge landed, or the quote was 0 after the discount.
PAID: frozenset[PaymentStatus] = frozenset({"landed", "not_charged"})

USDC_MICRO_PER_CENT = 10_000


@dataclass(frozen=True)
class CandidateEndpoint:
    candidate_id: str  # f"{catalog}:{api}:{method}:{path}"
    catalog: Catalog
    api: str
    method: Literal["GET", "POST"]
    path: str
    description: str  # untrusted_summary, capped at 300
    price_cents: int  # live /v1/details quote, else the pinned price
    price_from: PriceFrom
    query_keys: frozenset[str]
    body_keys: frozenset[str]
    required_keys: frozenset[str]
    response_paths: tuple[
        str, ...
    ]  # declared responseParams, readers.field_names grammar
    wired: bool  # False for pay.sh until lane 5 lands
    storage: Storage
    #: "name (type): description" for each input the catalog describes: the only way a
    #: planner learns an opt-in such as ``include`` (untrusted_summary, capped).
    input_notes: tuple[str, ...] = ()
    #: Inputs the catalog types as arrays: a literal for one is sent as a list.
    list_inputs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CandidateSet:
    candidates: tuple[CandidateEndpoint, ...]
    snapshot_sha: str  # sha256 over sorted candidate_ids + prices


@dataclass(frozen=True)
class PlanRequest:
    customer: str  # the gate's account id
    query: str  # sanitized, at most max_query_chars
    subjects: tuple[str, ...] = ()  # optional; otherwise the planner extracts them


@dataclass(frozen=True)
class InputBinding:
    param: str  # must be in the candidate's query_keys | body_keys
    source: InputFrom
    literal: str | None = (
        None  # <=200 chars, no scheme://, only when source == "literal"
    )


@dataclass(frozen=True)
class PlannedSource:
    rank: int  # 0 fills the field first; then failover order
    candidate_id: str
    catalog: Catalog
    api: str
    path: str
    price_cents: int  # copied from the candidate, never from the model
    field_path: str  # must be one of candidate.response_paths
    inputs: tuple[InputBinding, ...]
    storage: Storage
    evidence: Literal["declared", "confirmed"] = "declared"
    # Not in the first draft of the contract: without it a Quote cannot say whether its
    # prices are live or pinned, because the plan no longer holds the CandidateSet.
    # validate_plan overwrites it from the candidate, like price_cents.
    price_from: PriceFrom = "pinned"
    #: The candidate's array-typed inputs, copied by validate_plan: a literal bound to
    #: one is sent as a comma-split list (``include: work_email, phone``).
    list_inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedField:
    name: str  # ^[a-z][a-z0-9_]{0,39}$
    meaning: str  # our words, at most 120
    compare: Compare
    sources: tuple[PlannedSource, ...]  # 1..max_sources_per_field, ranked


@dataclass(frozen=True)
class Unplanned:
    field: str
    why: str  # "no catalog endpoint declares it", "only pay.sh (not wired)", ...


@dataclass(frozen=True)
class PlanDraft:
    """The planner's output. Untrusted until ``validate_plan`` has turned it into a plan."""

    subjects: tuple[str, ...]
    fields: tuple[PlannedField, ...]
    unplanned: tuple[Unplanned, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class QueryPlan:
    plan_id: str
    customer: str
    query: str
    subjects: tuple[str, ...]  # at most max_subjects
    fields: tuple[PlannedField, ...]  # at most max_fields
    unplanned: tuple[Unplanned, ...]
    planner: PlannerKind
    planner_model: str | None
    catalog_sha: str
    created_at: str


@dataclass(frozen=True)
class Bundle:
    """A question the customer saved by name, to run again for new subjects.

    It points at the plan it was saved from: that plan's fields and ranked sources are
    reused as they are, so a bundle run is planned without the model and priced the same
    per subject."""

    customer: str
    name: str  # ^[a-z0-9][a-z0-9-]{0,39}$
    plan_id: str
    query: str
    created_at: str


@dataclass(frozen=True)
class Quote:
    quote_id: str
    plan_id: str
    customer: str
    #: Every distinct (endpoint, subject, inputs) request, failover included (worst case).
    upstream_max_cents: int
    upstream_max_usdc_micro: int
    gecko_fee_usdc_micro: int  # max(floor, margin_pct of upstream)
    discount_usdc_micro: int  # min(available_discount, subtotal), from the ledger
    total_usdc_micro: int  # subtotal - discount; at most config.max_run_usdc_micro
    max_calls: int
    price_from: QuotePriceFrom
    created_at: str
    expires_at: str  # created_at + quote_ttl_s


@dataclass(frozen=True)
class PriorValue:
    run_id: str
    value: str | None
    status: FieldState


@dataclass(frozen=True)
class FactRow:
    customer: str
    plan_id: str
    run_id: str
    mode: CallMode
    subject: str
    field: str
    value: str | None  # untrusted_summary(cap 200); stored whatever the storage label
    source: str  # candidate_id that filled it ("" for no data)
    fetched_at: str
    status: FieldState  # fresh | repaired | stale | no data
    previous: PriorValue | None  # last good value of (plan, subject, field)
    agreement: Agreement = "only one"
    failover: bool = False
    stale_since: str | None = None
    note: str = ""  # our words only (Resolution.line())


@dataclass(frozen=True)
class RunRecord:
    """One run. The service claims it (``status="running"``, ``payment_status=
    "pending"``) and captures the payment BEFORE ``execute`` makes any paid call. A
    payment that did not land leaves ``status="failed"`` and ``payment_status=
    "failed"`` (``unpaid``: nothing was run). A paid run that delivered nothing is
    ``credited``. Rows are handed out only for a finished run whose payment is in
    :data:`PAID`."""

    run_id: str
    customer: str
    plan_id: str
    quote_id: str  # unique: one run per quote (idempotency)
    mode: CallMode
    status: RunStatus
    payment_status: PaymentStatus
    started_at: str
    finished_at: str | None
    upstream_quoted_cents: int
    upstream_charged_cents: int
    calls: int
    skipped_calls: int
    counts: dict[FieldState, int]
    stop_code: str | None  # ListJobError.code that stopped it
    notices: tuple[str, ...]  # our words


@dataclass(frozen=True)
class Payment:
    """What capture is asked to do. Built in code, never by the agent.

    ``amount_usdc_micro`` is what moves on chain, captured BEFORE the run:
    ``quote.total_usdc_micro``, which is ALREADY net of the discount (0 when the discount
    covers it all). ``discount_usdc_micro`` is recorded as a ``discount`` ledger entry
    only; it is never subtracted a second time from the transfer.
    """

    customer: str
    run_id: str
    quote_id: str
    amount_usdc_micro: int
    discount_usdc_micro: int
    pay_with: PayWith
    payer: str  # resolved from WalletDirectory, never from args
    payee_usdc_ata: str  # ATA(fee_wallet, USDC mint), derived


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    customer: str
    kind: LedgerKind
    run_id: str | None
    quote_id: str | None
    amount_usdc_micro: int  # always >= 0; kind gives the direction
    status: PaymentStatus
    pay_with: PayWith | None
    signature: str | None
    slot: int | None
    send_label: str | None  # "helius-mainnet" | "public" | "fake"
    rebate_requested: bool
    rebate_lamports: int | None  # observed, rebate_credit only
    split_user_pct: int | None  # config value at observation time
    #: micro-USDC per SOL: the pool price a SOL charge was converted at, or the rate a
    #: settlement's swap realised (``usdc_out_micro * 1e9 / sol_in_lamports``).
    sol_usdc_rate_micro: int | None
    created_at: str
    note: str
    #: Lamports that moved: what a SOL charge paid into the settlement wallet, or what a
    #: settlement swapped. ``None`` for every USDC-only entry (and every older entry).
    amount_lamports: int | None = None


#: How one cell moved between two versions of a dataset (the endpoint's changes_since).
DatasetChangeKind = Literal["added", "changed", "went_stale", "recovered", "removed"]


@dataclass(frozen=True)
class DatasetField:
    """One column of a dataset, copied from its template plan's ``PlannedField``."""

    name: str
    meaning: str
    compare: Compare
    pii: bool  # pii.is_pii_field(name), fixed at creation


@dataclass(frozen=True)
class DatasetVersion:
    """One delivered run adopted into a dataset. Never edited, except the snapshot
    fields of the last one."""

    n: int  # 1, 2, 3 ... equal to its index + 1
    run_id: str
    plan_id: str
    subjects_sha: str  # sha256 of the sorted subject set at this version
    rows: int
    counts: dict[FieldState, int]  # cells per status after materialize
    rows_sha256: str  # sha256 of the canonical JSON of the rows' cells
    materialized_at: str
    snapshot_uri: str | None  # None until the snapshot is written
    snapshot_status: SnapshotStatus


@dataclass(frozen=True)
class Dataset:
    """A customer's living table: one row per subject, history in ``versions``.

    ``dataset_id`` is unique per customer only. The template plan is pinned at creation
    and never follows the bundle name: a new schema is a new dataset."""

    dataset_id: str  # bundles.BUNDLE_NAME_RE; stored as _id
    customer: str
    bundle: str  # display only
    template_plan_id: str
    query: str
    mode: CallMode  # never mixed
    subjects: tuple[str, ...]  # at most config.max_subjects
    schema: tuple[DatasetField, ...]
    plan_ids: tuple[str, ...]  # every plan it ran under, append-only, in order
    versions: tuple[DatasetVersion, ...]
    revision: int  # optimistic lock; +1 on every save
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DatasetCell:
    field: str
    value: str | None  # whole value, as in facts; masked only in views
    source: str  # candidate_id that produced ``value``, "" for none
    fetched_at: str | None  # when ``value`` was fetched
    checked_at: str | None  # when this cell was last asked for, whatever the outcome
    status: FieldState
    stale_since: str | None
    failover: bool
    note: str  # our words only, from FactRow.note
    run_id: str | None  # the run that produced ``value``


@dataclass(frozen=True)
class DatasetRow:
    """One subject of a dataset. ``cells`` is a tuple in schema order, not a dict: the
    Mongo codec rebuilds dataclasses inside a tuple, never inside a dict."""

    row_id: str  # sha256(dataset_id + "\n" + subject)[:24]
    customer: str
    dataset_id: str
    subject: str
    version: int  # the version that last wrote this row
    cells: tuple[DatasetCell, ...]
    updated_at: str
