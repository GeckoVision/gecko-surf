"""The any-query tools, behind the gate: ``call_anyquery_tool`` and its deps.

Every planner, store, runner and billing piece is injected, so the whole path runs
offline on :class:`~gecko.anyquery.planner.FakePlanner`, :class:`MemoryStore` and
:class:`FakeBilling`, and production swaps in the lanes 1-3 implementations without
touching this file.

THE ORDER OF A RUN, and why (founder ruling, 8 Oct, after the review: charge BEFORE any
paid call). A preflight that says the wallet cannot pay stops the run before anything.
The service claims the quote (``start_run``: one run per quote, so a retried confirm
cannot run or charge twice), captures the payment ONCE at the confirmed quote, and reads
the ledger to see it landed. Only then does ``execute`` make a paid call, and it checks
the claim is paid itself. A run that then delivers no fresh or repaired fact, or fails to
deliver after its facts were written, is ``credited``: what it cost is a ledger credit on
the next run (no transfer) and its rows are withheld. A payment broadcast but not yet
visible is ``pending``: confirming the same quote again re-checks its signature and
never charges twice.

WHO. The customer is the account the Gecko-key gate resolved. No tool argument can name
a customer, a wallet, a payer, a price or a plan body (``FORBIDDEN_ARGUMENTS``); an
unknown or forbidden argument is refused, not ignored.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from gecko.orthogonal.errors import ListJobError
from gecko.orthogonal.supplier.history import changes_since as history_changes
from gecko.orthogonal.supplier.text import untrusted_summary

from .billing import (
    LEDGER_SCAN_LIMIT,
    Billing,
    BillingError,
    QuoteOverCapError,
    available_discount,
    credit_undelivered,
)
from .bundles import BUNDLE_NAME_RE, bundle_view, plan_from_bundle, reuse_problem
from .config import AnyQueryConfig
from .dataset_owner import OwnerDeps, create_dataset, datasets_list, refresh_dataset
from .datasets import DatasetError
from .planner import CandidatesFor, Planner, PlanRejectedError, ValidatePlan
from .history import is_delivered
from .run import (
    RUN_ID_RE,
    Execute,
    RunDeps,
    RunRefusedError,
    claim_record,
    new_run_id,
)
from .snapshots import SnapshotError, SnapshotSink
from .store import DuplicateChargeError, DuplicateRunError, Store, StoreError
from .surface import (
    ANYQUERY_TOOLS,
    FORBIDDEN_ARGUMENTS,
    GATE_HINT,
    NO_ACCOUNT,
    RENAMED_TOOLS,
    UNTRUSTED_NOTE,
)
from .types import (
    PAID,
    PAY_WITH,
    CandidateSet,
    LedgerEntry,
    Payment,
    PayWith,
    PlanRequest,
    Bundle,
    QueryPlan,
    Quote,
    RunRecord,
)
from .views import (
    changes_view,
    fact_view,
    ledger_view,
    plan_view,
    price_text,
    quote_view,
    run_view,
)

__all__ = [
    "AnyQueryDeps",
    "AnyQueryRefusal",
    "GATE_HINT",
    "call_anyquery_tool",
    "iso_now",
]

logger = logging.getLogger(__name__)

_GOOD = ("fresh", "repaired")
_TOOL_PROPS = {
    t["name"]: frozenset(t["inputSchema"]["properties"]) for t in ANYQUERY_TOOLS
}
_TOOL_REQUIRED = {
    t["name"]: frozenset(t["inputSchema"].get("required", ())) for t in ANYQUERY_TOOLS
}


class AnyQueryRefusal(Exception):
    """A refused call. ``message`` is our words only; ``code`` is stable for callers."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, **self.extra}


def iso_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class AnyQueryDeps:
    config: AnyQueryConfig
    store: Store
    billing: Billing
    planner: Planner
    candidates_for: CandidatesFor
    validate_plan: ValidatePlan
    catalog: Callable[[], CandidateSet]
    execute: Execute
    run_deps: RunDeps
    #: ATA(fee_wallet, USDC mint). Derived in code (see ``hosted.payee_usdc_ata``).
    payee_usdc_ata: Callable[[], str]
    clock: Callable[[], str] = iso_now
    #: Where dataset snapshots go. ``None`` keeps every new version ``pending``; the
    #: table in Mongo is whole either way.
    dataset_sink: SnapshotSink | None = None


# -- arguments --------------------------------------------------------------------------


def _check_args(name: str, args: dict[str, Any]) -> None:
    given = set(args)
    forbidden = sorted(given & FORBIDDEN_ARGUMENTS)
    if forbidden:
        raise AnyQueryRefusal(
            "forbidden_argument",
            f"{name} does not take {', '.join(forbidden)}: the account comes from your "
            "Gecko key, and prices and plans come from the store.",
        )
    unknown = sorted(given - _TOOL_PROPS[name])
    if unknown:
        raise AnyQueryRefusal(
            "bad_arguments",
            f"{name} does not take {', '.join(unknown)}. It takes: "
            f"{', '.join(sorted(_TOOL_PROPS[name]))}.",
        )
    missing = sorted(_TOOL_REQUIRED[name] - given)
    if missing:
        raise AnyQueryRefusal("bad_arguments", f"{name} needs {', '.join(missing)}.")


def _text(args: dict[str, Any], key: str, cap: int) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AnyQueryRefusal("bad_arguments", f"{key} must be a non-empty string.")
    if len(value) > cap:
        raise AnyQueryRefusal(
            "bad_arguments", f"{key} is longer than {cap} characters."
        )
    return value.strip()


def _run_id(args: dict[str, Any], key: str) -> str | None:
    value = _text(args, key, 40)
    if value is not None and not RUN_ID_RE.match(value):
        raise AnyQueryRefusal(
            "bad_arguments", f"{key} looks like 20261008-082501-a1b2c3."
        )
    return value


# -- plan_query -------------------------------------------------------------------------


def _subjects(raw: Any, cap: int) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        raise AnyQueryRefusal("bad_arguments", "subjects must be a list of strings.")
    cleaned = [untrusted_summary(s, cap=200) for s in raw]
    out = tuple(dict.fromkeys(s for s in cleaned if s))
    if len(out) > cap:
        raise AnyQueryRefusal(
            "bad_arguments", f"at most {cap} subjects per query; split the query."
        )
    return out


def _fresh_quote(deps: AnyQueryDeps, plan: QueryPlan, now: str) -> Quote:
    try:
        quote = deps.billing.quote(plan, now=now)
    except QuoteOverCapError as exc:
        raise AnyQueryRefusal(
            "quote_over_cap", str(exc), plan_id=plan.plan_id
        ) from None
    deps.store.save_quote(quote)
    return quote


def _bundle_name(args: dict[str, Any]) -> str | None:
    name = _text(args, "bundle" if "bundle" in args else "name", 40)
    if name is not None and not BUNDLE_NAME_RE.match(name):
        raise AnyQueryRefusal(
            "bad_arguments",
            "a bundle name is lowercase letters, digits and dashes, e.g. lead-contacts.",
        )
    return name


def _plan_bundle(
    deps: AnyQueryDeps, account: str, name: str, args: dict[str, Any], now: str
) -> tuple[QueryPlan, bool]:
    """The bundle's plan for these subjects: the stored one when they were asked
    before, else the bundle's fields and sources copied onto them. No model call."""
    if args.get("query") is not None:
        raise AnyQueryRefusal("bad_arguments", "give a bundle or a query, not both.")
    subjects = _subjects(args.get("subjects"), deps.config.max_subjects)
    if not subjects:
        raise AnyQueryRefusal(
            "bad_arguments", "a bundle runs for the subjects you give: pass subjects."
        )
    bundle = deps.store.bundle(account, name)
    template = None if bundle is None else deps.store.plan(account, bundle.plan_id)
    if bundle is None or template is None:
        raise AnyQueryRefusal(
            "bundle_not_found",
            f"No bundle {name} for your account. Call bundles to list yours.",
        )
    plan = deps.store.plan_for(account, bundle.query, subjects)
    if plan is not None:
        return plan, True
    plan = plan_from_bundle(template, subjects, name=name, now=now)
    deps.store.save_plan(plan)
    return plan, False


def _plan_query(deps: AnyQueryDeps, account: str, args: dict[str, Any]) -> dict:
    now = deps.clock()
    name = _bundle_name(args) if "bundle" in args else None
    if name is not None:
        bundled, reused = _plan_bundle(deps, account, name, args, now)
        return _planned(deps, bundled, reused, now)
    cap = deps.config.max_query_chars
    raw = _text(args, "query", cap)
    query = untrusted_summary(raw, cap=cap) if raw else ""
    if not query:
        raise AnyQueryRefusal("bad_arguments", "query must be a non-empty string.")
    request = PlanRequest(
        customer=account,
        query=query,
        subjects=_subjects(args.get("subjects"), deps.config.max_subjects),
    )
    # The same question again keeps its plan, so its runs share one history and
    # run_changes can compare them; a new plan would start a new history.
    plan = deps.store.plan_for(account, request.query, request.subjects)
    asked_before = plan is not None
    if plan is None:
        try:
            candidates = deps.candidates_for(request, deps.catalog())
            draft = deps.planner.draft(request, candidates)
            plan = deps.validate_plan(
                draft, request, candidates, deps.config, planner=deps.planner, now=now
            )
        except PlanRejectedError as exc:
            raise AnyQueryRefusal(
                "plan_rejected",
                f"Gecko could not plan this query: {exc}. Nothing was spent. Try naming "
                "the fields you want (for example: funding, headcount, latest news) and "
                "the companies or tokens to look up.",
            ) from None
        if plan.customer != account:
            # validate_plan is another lane's code; a plan for someone else is never saved.
            raise AnyQueryRefusal(
                "plan_rejected", "the planner returned a foreign plan."
            )
        deps.store.save_plan(plan)
    return _planned(deps, plan, asked_before, now)


def _planned(
    deps: AnyQueryDeps, plan: QueryPlan, asked_before: bool, now: str
) -> dict[str, Any]:
    quote = _fresh_quote(deps, plan, now)
    run_next = (
        f"Show the user the fields and the price ({price_text(quote)}; the "
        "Gecko fee is shown apart from the data). If they agree, call run_query with this plan_id and "
        f"quote_id and confirm=true before {quote.expires_at}."
    )
    if asked_before:
        run_next += (
            " This question was asked before, so this is the same plan: after the run, "
            "call run_changes with the earlier run_id to show what changed."
        )
    return {
        "plan": plan_view(plan),
        "quote": quote_view(quote),
        "asked_before": asked_before,
        "spent": "nothing",
        "next": run_next,
    }


# -- bundles ----------------------------------------------------------------------------


def _save_bundle(deps: AnyQueryDeps, account: str, args: dict[str, Any]) -> dict:
    name = _bundle_name(args)
    plan_id = _text(args, "plan_id", 64)
    plan = None if plan_id is None else deps.store.plan(account, plan_id)
    if name is None or plan is None:
        raise AnyQueryRefusal("plan_not_found", f"No plan {plan_id} for your account.")
    problem = reuse_problem(plan)
    if problem is not None:
        raise AnyQueryRefusal(
            "not_reusable", f"This plan cannot be saved as a bundle: {problem}."
        )
    replaced = deps.store.bundle(account, name) is not None
    bundle = Bundle(
        customer=account,
        name=name,
        plan_id=plan.plan_id,
        query=plan.query,
        created_at=deps.clock(),
    )
    deps.store.save_bundle(bundle)
    return {
        "bundle": bundle_view(bundle, plan),
        "replaced": replaced,
        "spent": "nothing",
        "next": (
            f"Run it for new subjects with plan_query(bundle='{name}', subjects=[...]): "
            "the same fields and sources, no new planning."
        ),
    }


def _bundles(deps: AnyQueryDeps, account: str, args: dict[str, Any]) -> dict:
    return {
        "bundles": [
            bundle_view(b, deps.store.plan(account, b.plan_id))
            for b in deps.store.bundles(account)
        ]
    }


# -- run_query --------------------------------------------------------------------------


def _existing_run(deps: AnyQueryDeps, account: str, quote: Quote) -> RunRecord | None:
    for run in deps.store.runs(account, plan_id=quote.plan_id, limit=LEDGER_SCAN_LIMIT):
        if run.quote_id == quote.quote_id:
            return run
    return None


def _needs_confirmation(
    deps: AnyQueryDeps, plan: QueryPlan, now: str, why: str
) -> dict[str, Any]:
    quote = _fresh_quote(deps, plan, now)
    return {
        "status": "needs_confirmation",
        "why": why,
        "quote": quote_view(quote),
        "spent": "nothing",
        "next": (
            "Show the user this price. If they agree, call run_query again with this "
            "quote_id and confirm=true."
        ),
    }


def _parse_now(now: str) -> dt.datetime:
    return dt.datetime.fromisoformat(now.replace("Z", "+00:00"))


def _run_query(deps: AnyQueryDeps, account: str, args: dict[str, Any]) -> dict:
    plan_id = _text(args, "plan_id", 64) or ""
    quote_id = _text(args, "quote_id", 64)
    confirm = args.get("confirm")
    if not isinstance(confirm, bool):
        raise AnyQueryRefusal("bad_arguments", "confirm must be true or false.")
    pay_with = args.get("pay_with", "USDC")
    if pay_with not in PAY_WITH:
        raise AnyQueryRefusal("bad_arguments", "pay_with must be USDC or SOL.")
    plan = deps.store.plan(account, plan_id)
    if plan is None:
        raise AnyQueryRefusal(
            "plan_not_found", "No plan with that plan_id. Call plan_query first."
        )
    now = deps.clock()
    quote = deps.store.quote(account, quote_id) if quote_id else None
    if quote is not None and quote.plan_id != plan.plan_id:
        quote = None
    if quote is not None:
        done = _existing_run(deps, account, quote)
        if done is not None:
            # Before the expiry check: a payment re-checked after the quote expired has
            # still bought this run.
            return _resume(deps, account, plan, quote, done)
    if quote is None:
        return _needs_confirmation(deps, plan, now, "no quote for this plan was given")
    if now >= quote.expires_at:
        return _needs_confirmation(deps, plan, now, "that quote expired")
    if not confirm:
        return _needs_confirmation(deps, plan, now, "confirm was false")
    return _confirmed_run(deps, account, plan, quote, pay_with, now)


def _confirmed_run(
    deps: AnyQueryDeps,
    account: str,
    plan: QueryPlan,
    quote: Quote,
    pay_with: PayWith,
    now: str,
) -> dict[str, Any]:
    """Claim the quote, charge it, see the charge land, and only then run."""
    preflight = deps.billing.preflight(account, quote, pay_with)
    if not preflight.ok:
        raise AnyQueryRefusal(
            "cannot_pay",
            f"Not run, nothing spent: {preflight.reason}.",
            payer=preflight.payer,
            quote=quote_view(quote),
        )
    claimed = claim_record(
        plan,
        quote,
        run_id=new_run_id(_parse_now(now)),
        mode=deps.run_deps.mode,
        now=now,
    )
    try:
        # The claim comes first so a retried confirm finds this run, and the ledger's
        # one-charge-per-run key can stop a second charge, before anything is signed.
        deps.store.start_run(claimed)
    except DuplicateRunError:
        done = _existing_run(deps, account, quote)
        if done is None:
            raise
        return _resume(deps, account, plan, quote, done)
    charge = _capture(deps, account, claimed, quote, pay_with, preflight.payer)
    return _after_charge(deps, account, plan, quote, claimed, charge, repeated=False)


def _capture(
    deps: AnyQueryDeps,
    account: str,
    claimed: RunRecord,
    quote: Quote,
    pay_with: PayWith,
    payer: str | None,
) -> LedgerEntry | None:
    """The one charge for this run, at the confirmed quote. What the LEDGER holds after
    the attempt is the answer, not the return value alone: a capture that raised after
    writing its pending entry may have broadcast, and must be re-checked, not failed."""
    payment = Payment(
        customer=account,
        run_id=claimed.run_id,
        quote_id=quote.quote_id,
        amount_usdc_micro=quote.total_usdc_micro,
        discount_usdc_micro=quote.discount_usdc_micro,
        pay_with=pay_with,
        payer=payer or "",
        payee_usdc_ata=deps.payee_usdc_ata(),
    )
    try:
        return deps.billing.capture(payment)
    except (BillingError, DuplicateChargeError):
        logger.error("any-query capture for run %s did not settle", claimed.run_id)
    except Exception as exc:
        # Anything else (an RPC 429, a dropped socket) may have come after the
        # broadcast. The ledger, not the exception, says whether money moved; only the
        # type is logged, since a transport message can carry a keyed URL.
        logger.error(
            "any-query capture for run %s raised %s; settling from the ledger",
            claimed.run_id,
            type(exc).__name__,
        )
    return _charge_of(deps, account, claimed.run_id)


def _charge_of(deps: AnyQueryDeps, account: str, run_id: str) -> LedgerEntry | None:
    for entry in deps.store.ledger(account, limit=LEDGER_SCAN_LIMIT):
        if entry.run_id == run_id and entry.kind == "charge":
            return entry
    return None


def _after_charge(
    deps: AnyQueryDeps,
    account: str,
    plan: QueryPlan,
    quote: Quote,
    claimed: RunRecord,
    charge: LedgerEntry | None,
    *,
    repeated: bool,
) -> dict[str, Any]:
    """Run only on a charge the ledger shows landed at the confirmed quote (or a quote
    the discount covered); otherwise record why nothing ran."""
    status = charge.status if charge is not None else "failed"
    if status == "pending":
        held = replace(claimed, payment_status="pending")
        deps.store.finish_run(held)
        return _run_answer(deps, account, held, repeated=repeated)
    if charge is None or status not in PAID:
        unpaid = replace(
            claimed,
            status="failed",
            payment_status="failed",
            finished_at=deps.clock(),
            notices=(*claimed.notices, "the payment did not land; nothing was run"),
        )
        deps.store.finish_run(unpaid)
        return _run_answer(deps, account, unpaid, repeated=repeated)
    paid = replace(claimed, payment_status=status)
    deps.store.finish_run(paid)
    if charge.amount_usdc_micro != quote.total_usdc_micro:
        return _undelivered(
            deps, account, paid.run_id, "the charge was not the confirmed quote"
        )
    return _execute_paid(deps, account, plan, quote, paid)


def _execute_paid(
    deps: AnyQueryDeps, account: str, plan: QueryPlan, quote: Quote, paid: RunRecord
) -> dict[str, Any]:
    run_id = paid.run_id
    try:
        result = deps.execute(plan, quote, deps.run_deps, run_id=run_id)
    except RunRefusedError as exc:
        return _undelivered(deps, account, run_id, f"not run: {exc}")
    except Exception as exc:  # noqa: BLE001 - a paid run that failed is credited
        logger.error("any-query run %s failed: %s", run_id, type(exc).__name__)
        stop = exc.code if isinstance(exc, ListJobError) else None
        # A ListJobError's message is our words; any other error's may not be.
        why = f": {exc.message}" if isinstance(exc, ListJobError) else ""
        return _undelivered(
            deps,
            account,
            run_id,
            f"the run failed before its rows were delivered{why}",
            stop_code=stop,
        )
    record = result.record
    if record.run_id != run_id or record.quote_id != quote.quote_id:
        return _undelivered(deps, account, run_id, "the runner returned another run")
    if not any(f.status in _GOOD for f in result.facts if f.customer == account):
        return _undelivered(
            deps, account, run_id, "no fresh or repaired value was found"
        )
    held = deps.store.run(account, run_id)
    if held is None or not is_delivered(held):
        return _undelivered(deps, account, run_id, "the run's record was not stored")
    return _run_answer(deps, account, held, repeated=False)


def _undelivered(
    deps: AnyQueryDeps,
    account: str,
    run_id: str,
    notice: str,
    *,
    stop_code: str | None = None,
) -> dict[str, Any]:
    """A paid run that hands out no row: its cost becomes a credit on the next run (a
    ledger line, no transfer), and it is marked ``credited`` so no tool reads it."""
    held = deps.store.run(account, run_id)
    if held is None:
        raise AnyQueryRefusal("run_failed", "the run was lost before it was settled.")
    now = deps.clock()
    credit_undelivered(
        deps.store,
        account,
        run_id,
        new_id=_ledger_id,
        now=now,
        note=f"nothing was delivered ({notice}); a credit on your next run",
    )
    final = replace(
        held,
        status="failed" if held.status == "running" else held.status,
        payment_status="credited",
        finished_at=held.finished_at or now,
        stop_code=stop_code or held.stop_code,
        notices=(*held.notices, notice),
    )
    deps.store.finish_run(final)
    return _run_answer(deps, account, final, repeated=False)


def _ledger_id() -> str:
    return secrets.token_hex(8)


def _resume(
    deps: AnyQueryDeps, account: str, plan: QueryPlan, quote: Quote, run: RunRecord
) -> dict[str, Any]:
    """The quote already has a run. Never a second charge: a pending payment is
    re-checked by its signature, and only a run still waiting on it goes on to run."""
    if run.status != "running" or run.payment_status != "pending":
        return _run_answer(deps, account, run, repeated=True)
    charge = _charge_of(deps, account, run.run_id)
    if charge is None:
        # Claimed, and the capture never wrote its entry: nothing was signed. Another
        # request may be capturing it now, so this one only reports.
        return _run_answer(deps, account, run, repeated=True)
    if charge.status == "pending":
        try:
            charge = deps.billing.recheck(charge)
        except BillingError:
            logger.error("any-query recheck for run %s did not settle", run.run_id)
            return _run_answer(deps, account, run, repeated=True)
    return _after_charge(deps, account, plan, quote, run, charge, repeated=True)


def _status_of(run: RunRecord) -> str:
    if run.payment_status == "pending":
        return "payment_pending"
    if run.payment_status == "failed":
        return "not_run"
    if run.payment_status == "credited":
        return "not_delivered"
    if run.status == "running":
        return "running"
    return "ran"


def _next_step(run: RunRecord, charge: Any, credit: Any) -> str:
    if run.payment_status == "pending":
        signature = charge.signature if charge is not None else None
        seen = f" (signature {signature})" if signature else ""
        return (
            f"The payment was broadcast{seen} but is not visible on chain yet, so "
            "nothing was run. Call run_query again with the same plan_id, quote_id and "
            "confirm=true: it re-checks that signature and never charges again."
        )
    if run.payment_status == "failed":
        return (
            "The payment did not land, so nothing was run and nothing is due. Check "
            "the wallet with payments, then confirm a new quote with run_query."
        )
    if run.payment_status == "credited":
        amount = credit["amount_usdc"] if credit else "0"
        return (
            "Nothing fresh or repaired was delivered, so the rows are withheld. What "
            f"this run cost ({amount} USDC) is a credit on your next run, taken off "
            "its quote (no transfer; see payments)."
        )
    if run.status == "running":
        return "This run is paid and still running; call run_query again shortly."
    return (
        f"Call run_rows with run_id {run.run_id} for the rows, and later "
        f"run_changes with run_id {run.run_id} to see what moved."
    )


def _run_answer(
    deps: AnyQueryDeps, account: str, run: RunRecord, *, repeated: bool
) -> dict[str, Any]:
    entries = [
        e
        for e in deps.store.ledger(account, limit=LEDGER_SCAN_LIMIT)
        if e.run_id == run.run_id
    ]
    charge = next((e for e in entries if e.kind == "charge"), None)
    credit_entry = next((e for e in entries if e.kind == "run_credit"), None)
    credit = ledger_view(credit_entry) if credit_entry else None
    out: dict[str, Any] = {
        "status": _status_of(run),
        "run": run_view(run),
        "payment": ledger_view(charge) if charge else None,
        "credit": credit,
        "repeated": repeated,
        "next": _next_step(run, charge, credit),
    }
    if repeated:
        out["note"] = "This quote already has a run; it is never charged twice."
    return out


# -- run_rows / run_changes / payments ---------------------------------------------------


def _reveal(args: dict[str, Any]) -> bool:
    """``reveal_pii``: contact details are masked unless the caller asks for them whole."""
    flag = args.get("reveal_pii", False)
    if not isinstance(flag, bool):
        raise AnyQueryRefusal("bad_arguments", "reveal_pii must be true or false.")
    return flag


def _readable_run(deps: AnyQueryDeps, account: str, run_id: str) -> RunRecord:
    run = deps.store.run(account, run_id)
    if run is None:
        raise AnyQueryRefusal("run_not_found", f"No run {run_id} for your account.")
    if not is_delivered(run):
        raise AnyQueryRefusal(
            "withheld",
            f"Run {run_id} delivered nothing you paid for (run {run.status}, payment "
            f"{run.payment_status}); its rows are withheld.",
        )
    return run


def _run_rows(deps: AnyQueryDeps, account: str, args: dict[str, Any]) -> dict:
    run_id = _run_id(args, "run_id")
    plan_id = _text(args, "plan_id", 64)
    if run_id is None:
        runs = deps.store.runs(account, plan_id=plan_id, limit=LEDGER_SCAN_LIMIT)
        paid = [r for r in runs if is_delivered(r)]
        if not paid:
            raise AnyQueryRefusal(
                "no_runs",
                "No paid run yet. Call plan_query, then run_query with confirm=true.",
            )
        run_id = paid[0].run_id
    run = _readable_run(deps, account, run_id)
    rows = deps.store.facts(
        account,
        run_id=run.run_id,
        subject=_text(args, "subject", 200),
        field=_text(args, "field", 40),
    )
    return {
        "run": run_view(run),
        "rows": [fact_view(r, reveal_pii=_reveal(args)) for r in rows],
        "note": UNTRUSTED_NOTE,
    }


def _run_changes(deps: AnyQueryDeps, account: str, args: dict[str, Any]) -> dict:
    run_id = _run_id(args, "run_id") or ""
    to_run = _run_id(args, "to_run")
    base = _readable_run(deps, account, run_id)
    if to_run is not None:
        target = _readable_run(deps, account, to_run)
        if target.plan_id != base.plan_id:
            raise AnyQueryRefusal("bad_arguments", "to_run is a run of another plan.")
    runs = deps.store.runs(account, plan_id=base.plan_id, limit=LEDGER_SCAN_LIMIT)
    readable = {r.run_id for r in runs if is_delivered(r)}
    rows = [
        r
        for r in deps.store.plan_facts(account, base.plan_id)
        if r.get("run_id") in readable
    ]
    try:
        changes = history_changes(
            rows, run_id, to_run=to_run, subject_key="subject", latest_hint="run_rows"
        )
    except ListJobError as exc:
        raise AnyQueryRefusal(exc.code, exc.message) from None
    values = {
        (str(r["run_id"]), str(r["subject"]), str(r["field"])): r.get("value")
        for r in rows
    }
    return {
        **changes_view(changes, values, reveal_pii=_reveal(args)),
        "note": UNTRUSTED_NOTE,
    }


def _payments(deps: AnyQueryDeps, account: str, args: dict[str, Any]) -> dict:
    limit = args.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise AnyQueryRefusal("bad_arguments", "limit must be an integer 1..50.")
    observed = deps.billing.observe_rebates(account)
    entries = deps.billing.entries(account, limit=limit)
    available = available_discount(deps.store.ledger(account, limit=LEDGER_SCAN_LIMIT))
    return {
        "entries": [ledger_view(e) for e in entries],
        "rebates_credited_now": len(observed),
        "discount_on_next_run_usdc_micro": available,
        "note": (
            "There is no balance: each run is paid once, before any paid call, at the "
            "price you confirmed. A run that delivers no fresh or repaired value "
            "becomes a credit of what it cost on your next run (no transfer). Rebates "
            "are intermittent and become a discount only when observed."
        ),
    }


# -- the owner's dataset tools (logic in dataset_owner) ---------------------------------


def _owner_deps(deps: AnyQueryDeps, account: str) -> OwnerDeps:
    def run_query(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _run_query(deps, account, args)
        except AnyQueryRefusal as refusal:
            return refusal.to_dict()

    return OwnerDeps(
        store=deps.store,
        sink=deps.dataset_sink,
        clock=deps.clock,
        mode=deps.run_deps.mode,
        max_subjects=deps.config.max_subjects,
        run_query=run_query,
    )


def _create_dataset(deps: AnyQueryDeps, account: str, args: dict[str, Any]) -> dict:
    bundle = _bundle_name({"bundle": args.get("bundle")}) or ""
    name = _bundle_name({"name": args["name"]}) if "name" in args else None
    subjects = (
        _subjects(args["subjects"], deps.config.max_subjects)
        if "subjects" in args
        else None
    )
    return create_dataset(
        _owner_deps(deps, account),
        account,
        bundle=bundle,
        dataset_id=name or bundle,
        subjects=subjects,
        from_run=_run_id(args, "from_run"),
    )


def _refresh_dataset(deps: AnyQueryDeps, account: str, args: dict[str, Any]) -> dict:
    dataset_id = _text(args, "dataset_id", 40) or ""
    confirm = args.get("confirm")
    if not isinstance(confirm, bool):
        raise AnyQueryRefusal("bad_arguments", "confirm must be true or false.")
    pay_with = args.get("pay_with", "USDC")
    if pay_with not in PAY_WITH:
        raise AnyQueryRefusal("bad_arguments", "pay_with must be USDC or SOL.")
    subjects = (
        _subjects(args["subjects"], deps.config.max_subjects)
        if "subjects" in args
        else None
    )
    return refresh_dataset(
        _owner_deps(deps, account),
        account,
        dataset_id=dataset_id,
        confirm=confirm,
        quote_id=_text(args, "quote_id", 64),
        subjects=subjects,
        pay_with=pay_with,
    )


def _datasets(deps: AnyQueryDeps, account: str, args: dict[str, Any]) -> dict:
    return datasets_list(deps.store, account)


_HANDLERS: dict[str, Callable[[AnyQueryDeps, str, dict[str, Any]], dict]] = {
    "plan_query": _plan_query,
    "run_query": _run_query,
    "run_rows": _run_rows,
    "run_changes": _run_changes,
    "create_dataset": _create_dataset,
    "refresh_dataset": _refresh_dataset,
    "datasets": _datasets,
    "payments": _payments,
    "save_bundle": _save_bundle,
    "bundles": _bundles,
}


def call_anyquery_tool(
    deps: AnyQueryDeps, account: str | None, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """One tool call. Refusals come back as ``{"error", "code"}``; nothing raises a
    store or billing message to the caller, so no URI or key can ride out in one.

    A renamed tool's old name (``RENAMED_TOOLS``) runs the new one and says so."""
    renamed = RENAMED_TOOLS.get(name)
    if renamed is not None:
        out = call_anyquery_tool(deps, account, renamed, args)
        return {**out, "renamed": renamed}
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name}", "code": "unknown_tool"}
    if account is None or not account.strip():
        return {"error": GATE_HINT, "code": NO_ACCOUNT}
    try:
        _check_args(name, args)
        return handler(deps, account, args)
    except AnyQueryRefusal as refusal:
        return refusal.to_dict()
    except DatasetError as refusal:
        return {"error": str(refusal), "code": refusal.code}
    except SnapshotError as exc:
        # The sink's message names a bucket or a key; the caller gets our words only.
        logger.error(
            "any-query %s: snapshot sink failed (%s)", name, type(exc).__name__
        )
        return {
            "error": "The snapshot store could not answer; try again in a minute. "
            "A confirmed quote never runs or charges twice.",
            "code": "snapshot_unavailable",
        }
    except StoreError as exc:
        logger.error("any-query %s: store refused (%s)", name, type(exc).__name__)
        return {
            "error": "The dataset store could not answer; try again in a minute. A "
            "confirmed quote never runs or charges twice.",
            "code": "store_unavailable",
        }
