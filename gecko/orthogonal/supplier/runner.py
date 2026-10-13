"""One logical upstream call for the supplier watch, with its spend and its ledger rows.

Every call goes through the shared ``paginate.fetch_pages`` and ``SpendGuard``. A
failure that would repeat on every remaining call sets ``stop``; later calls are
counted as skipped instead of sent. Each page (and each failed first page) leaves one
field-ledger row: names and counts, never values.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from ..errors import (
    ContinuationChargedError,
    KeyRejectedError,
    ListJobError,
    NoKeyError,
    NotAllowedError,
    OutOfCreditsError,
    SpendCapError,
    UpstreamError,
)
from ..paginate import CallLine, FetchResult, fetch_pages
from ...modes import CallMode
from ..job import JobDeps
from ..spec import Target
from ..spend import SpendGuard
from ..steps import PublicStep
from .calls import CallSpec
from .readers import data_of, field_names, row_count

#: Failures that would repeat on every remaining call, so the run stops calling.
_STOPPING = (
    SpendCapError,
    OutOfCreditsError,
    KeyRejectedError,
    NoKeyError,
    NotAllowedError,
    ContinuationChargedError,
)


class RunnerDeps(Protocol):
    """What a runner needs from a job's deps: the Orthogonal core and the mode.

    The supplier watch and the living file both satisfy it, so both spend through one
    loop, one guard and one ledger-row shape.
    """

    @property
    def base(self) -> JobDeps: ...

    @property
    def mode(self) -> CallMode: ...


D = TypeVar("D", bound=RunnerDeps)


@dataclass
class Runner(Generic[D]):
    deps: D
    guard: SpendGuard
    quotes: dict[Target, int]
    run_id: str
    calls: list[CallLine] = field(default_factory=list)
    ledger_rows: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    stop: ListJobError | None = None
    skipped: int = 0

    def call(self, spec: CallSpec) -> FetchResult | None:
        """One logical call (all its pages). ``None`` when it produced no reply."""
        if self.stop is not None:
            self.skipped += 1
            return None
        try:
            result = fetch_pages(
                self.deps.base.api,
                spec.body_for(),
                spec.reader(),
                self.guard,
                self.quotes[spec.target],
                max_pages=spec.max_pages,
                endpoint=spec.endpoint,
            )
        except ListJobError as exc:
            self._ledger(spec, None, None, exc)
            if isinstance(exc, _STOPPING):
                self.stop = exc
            else:
                self.notices.append(f"{spec.endpoint}: {exc.message}")
            return None
        for index, line in enumerate(result.calls):
            self.calls.append(line)
            last = index == len(result.calls) - 1
            self._ledger(
                spec, line, result.payloads[index], result.abort if last else None
            )
        if result.abort is not None:
            if isinstance(result.abort, _STOPPING):
                self.stop = result.abort
            else:
                self.notices.append(f"{spec.endpoint}: {result.abort.message}")
        return result

    def affordable(self, spec: CallSpec) -> str | None:
        """``None`` when one more full-price call fits both caps, else the reason.

        A failover asks first, so a source that would cross the cap is skipped and the
        field goes stale — the run is not stopped for a field that already has a value.
        """
        if self.stop is not None:
            return f"the run had stopped ({self.stop.code})"
        try:
            self.guard.check_fits(self.quotes[spec.target])
        except SpendCapError as exc:
            return exc.message
        return None

    def ledger_base(self) -> dict[str, Any]:
        return {
            "ts": dt.datetime.fromtimestamp(
                self.deps.base.clock(), dt.timezone.utc
            ).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "mode": self.deps.mode,
        }

    def public_ledger_row(
        self, provider: str, step: PublicStep, subject: str
    ) -> dict[str, Any]:
        """A free call outside Orthogonal (GLEIF, RPC, a web page), kept like any other."""
        row = {
            **self.ledger_base(),
            "kind": "call",
            "subject": subject,
            "catalog": "public",
            "api": provider,
            "path": step.path,
            "page": 1,
            "input_fields": list(step.input_fields),
            "output_fields": list(step.output_fields),
            "rows": step.rows,
            "status": step.error or "ok",
            "http_status": step.http_status,
            "quoted_cents": 0,
            "charged_cents": 0,
            "charge_assumed": False,
            "continuation": False,
        }
        self.ledger_rows.append(row)
        return row

    def _ledger(
        self,
        spec: CallSpec,
        line: CallLine | None,
        payload: Any,
        error: ListJobError | None,
    ) -> None:
        page = line.page if line else 1
        # A failed first page that may have been charged is counted at its quote
        # (``paginate``); ``counted_cents`` says so, and the row must agree with the guard.
        counted = error.counted_cents if error is not None and line is None else 0
        inputs = spec.input_fields()
        if page > 1:
            inputs = [*inputs, "page", "parentRequestId"]
        self.ledger_rows.append(
            {
                **self.ledger_base(),
                "kind": "call",
                "subject": spec.subject,
                "catalog": "orthogonal",
                "api": spec.target.api,
                "path": spec.target.path,
                "page": page,
                "input_fields": inputs,
                "output_fields": field_names(data_of(payload)) if payload else [],
                "rows": row_count(payload) if payload else 0,
                "status": error.code if error else "ok",
                "http_status": (
                    error.status
                    if isinstance(error, UpstreamError)
                    and (line is None or line.failed)
                    else (200 if line else None)
                ),
                "quoted_cents": line.quoted_cents if line else self.quotes[spec.target],
                "charged_cents": line.charged_cents if line else counted,
                "charge_assumed": line.charge_assumed if line else counted > 0,
                "continuation": page > 1,
                # The x402 receipt: what was paid on chain for this call, and its
                # signature. Both None on the key path.
                "payment_signature": line.payment_signature if line else None,
                "paid_usdc_micro": line.paid_usdc_micro if line else None,
            }
        )
