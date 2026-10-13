"""The host-wide upstream spend cap, kept in Mongo so a deploy does not reset it.

Before: the hosted any-query ledger was a file in the Fargate task, so every new task
started at zero spent, and ``SpendGuard`` read the total and then added to it, so two
runs could both read "room left" and both spend. Now the hosted ledger is one Mongo
document outside every customer database, and the guard RESERVES the worst case with a
single conditional increment before the call, then settles it to what was charged.

Fakes only: the in-memory Mongo client; nothing here connects to a server.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from gecko.anyquery.upstream_spend import (
    UPSTREAM_SPEND_COLLECTION,
    UPSTREAM_SPEND_DB,
    MongoSpendLedger,
)
from gecko.orthogonal.errors import (
    KeyRejectedError,
    NoKeyError,
    NotAllowedError,
    OutOfCreditsError,
    SpendCapError,
    UpstreamError,
)
from gecko.orthogonal.paginate import PageRead, fetch_pages
from gecko.orthogonal.spend import FileLedger, ReservingLedger, SpendGuard
from tests.anyquery_fake_mongo import (
    FakeDuplicateKeyError,
    FakeMongoClient,
    FakeUnavailableError,
)


def _ledger(client: FakeMongoClient | None = None) -> MongoSpendLedger:
    return MongoSpendLedger(
        client if client is not None else FakeMongoClient(),
        unavailable=(FakeUnavailableError,),
    )


def _guard(ledger: Any, total_cap: int = 100, per_run_cap: int = 100) -> SpendGuard:
    return SpendGuard(ledger, per_run_cap, total_cap)


# --- cap refusal -----------------------------------------------------------------------


def test_a_call_that_would_cross_the_total_cap_is_refused_and_spends_nothing() -> None:
    ledger = _ledger()
    ledger.add(95)

    with pytest.raises(SpendCapError, match=r"capped at \$1.00.*\$0.95"):
        _guard(ledger).check_before(10)

    assert ledger.total_cents() == 95, "a refused reservation leaves no trace"


def test_a_call_that_fits_exactly_is_allowed() -> None:
    ledger = _ledger()
    ledger.add(90)
    guard = _guard(ledger)

    guard.check_before(10)
    guard.record(10)

    assert ledger.total_cents() == 100


# --- two concurrent claims -------------------------------------------------------------


def test_two_runs_that_check_before_either_records_cannot_both_pass() -> None:
    """The race the file ledger has: both read 0 spent, both pass. Here the second
    reservation sees the first one's hold and is refused."""
    client = FakeMongoClient()
    first, second = _guard(_ledger(client), 10), _guard(_ledger(client), 10)

    first.check_before(6)
    with pytest.raises(SpendCapError):
        second.check_before(6)

    first.record(6)
    assert _ledger(client).total_cents() == 6


def test_many_threads_claiming_at_once_never_overshoot_the_cap() -> None:
    client = FakeMongoClient()
    cap, bound = 10, 1
    passed: list[int] = []
    refused: list[int] = []
    start = threading.Barrier(25)

    def claim(n: int) -> None:
        guard = _guard(_ledger(client), cap)
        start.wait()
        try:
            guard.check_before(bound)
        except SpendCapError:
            refused.append(n)
            return
        passed.append(n)
        guard.record(bound)

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(passed) == cap and len(refused) == 15
    assert _ledger(client).total_cents() == cap


# --- reservation settles to the charge -------------------------------------------------


def test_record_settles_the_hold_to_what_was_charged() -> None:
    ledger = _ledger()
    guard = _guard(ledger)

    guard.check_before(10)
    assert ledger.total_cents() == 10, "the worst case is held while the call runs"
    guard.record(3)

    assert ledger.total_cents() == 3
    assert guard.spent == 3


def test_a_charge_above_the_hold_is_still_recorded_in_full() -> None:
    """The bound is a claim about price; what the upstream charged is the fact."""
    ledger = _ledger()
    guard = _guard(ledger)

    guard.check_before(5)
    guard.record(7)

    assert ledger.total_cents() == 7


class _FailingApi:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def call(self, op: Any, body: Any = None) -> Any:
        raise self.error


def _fetch_failing(guard: SpendGuard, error: BaseException) -> None:
    fetch_pages(
        _FailingApi(error),  # type: ignore[arg-type]
        lambda _cursor, _parent: {},
        lambda _payload: None,  # type: ignore[arg-type,return-value]
        guard,
        10,
    )


def test_a_call_that_failed_after_it_was_sent_keeps_its_hold_at_the_quote() -> None:
    """Founder-approved, 9 Oct: the upstream may have charged for a call that came back
    as a failure, so it is counted at its quote instead of being released."""
    ledger = _ledger()
    guard = _guard(ledger)

    with pytest.raises(UpstreamError) as caught:
        _fetch_failing(guard, UpstreamError("upstream refused the call", status=500))

    assert ledger.total_cents() == 10
    assert guard.spent == 10 and guard.held == 0
    assert caught.value.counted_cents == 10


@pytest.mark.parametrize(
    "error",
    [
        NoKeyError(),
        NotAllowedError("not on the allowlist"),
        KeyRejectedError("rejected", status=401),
        OutOfCreditsError("$0.00", 10),
        UpstreamError("no payment was sent", status=503, charge_possible=False),
        RuntimeError("a bug before the request left"),
    ],
)
def test_a_call_refused_before_anything_was_spent_releases_its_hold(
    error: BaseException,
) -> None:
    ledger = _ledger()
    guard = _guard(ledger)

    with pytest.raises(type(error)):
        _fetch_failing(guard, error)

    assert ledger.total_cents() == 0
    assert guard.spent == 0


class _SecondPageFails:
    """Page 1 answers with a cursor; page 2 fails after it was sent."""

    def __init__(self) -> None:
        self.calls = 0

    def call(self, op: Any, body: Any = None) -> Any:
        self.calls += 1
        if self.calls == 1:
            return {"priceCents": 2, "requestId": "req1", "data": {}}
        raise UpstreamError("page 2 broke", status=502)


def test_a_later_page_that_fails_after_sending_is_counted_and_written_down() -> None:
    ledger = _ledger()
    guard = _guard(ledger)
    result = fetch_pages(
        _SecondPageFails(),  # type: ignore[arg-type]
        lambda _cursor, _parent: {},
        lambda _payload: PageRead(items=(), cursor="c2"),
        guard,
        10,
    )
    assert result.stop_reason == "upstream_error"
    first, failed = result.calls
    assert (first.charged_cents, first.failed) == (2, False)
    assert (failed.charged_cents, failed.charge_assumed, failed.failed) == (
        10,
        True,
        True,
    )
    assert len(result.payloads) == len(result.calls)
    assert ledger.total_cents() == 12 and guard.spent == 12


def test_a_transport_that_breaks_after_sending_is_an_upstream_failure() -> None:
    """The wire broke mid-call: the request may have reached Orthogonal. The type is
    named, the transport's message (which can carry a URL or a header) is not."""
    from gecko.orthogonal.api import OrthogonalApi
    from gecko.orthogonal.spec import RUN_OP

    class _Key:
        def auth_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer orth_test_secret"}

    def broken(_req: Any) -> tuple[int, Any]:
        raise TimeoutError(
            "read timed out https://api.orthogonal.com?k=orth_test_secret"
        )

    api = OrthogonalApi(transport=broken, session=_Key())  # type: ignore[arg-type]
    with pytest.raises(UpstreamError) as caught:
        api.call(RUN_OP, {"api": "predictleads", "path": "/v3/companies/a.com"})
    assert caught.value.charge_possible
    assert "TimeoutError" in caught.value.message
    assert "orth_test_secret" not in caught.value.message


# --- restart keeps the total -----------------------------------------------------------


def test_a_restart_keeps_the_total_and_the_cap_still_binds() -> None:
    """A new task is a new ledger object on the same database."""
    client = FakeMongoClient()
    before = _guard(_ledger(client))
    before.check_before(60)
    before.record(60)

    after = _ledger(client)
    assert after.total_cents() == 60
    with pytest.raises(SpendCapError):
        _guard(after).check_before(50)


def test_the_ledger_lives_outside_every_customer_database() -> None:
    client = FakeMongoClient()
    _ledger(client).add(4)

    assert client.database_names() == {UPSTREAM_SPEND_DB}
    assert not UPSTREAM_SPEND_DB.startswith("gq_")
    (row,) = client[UPSTREAM_SPEND_DB][UPSTREAM_SPEND_COLLECTION].docs
    assert set(row) == {"_id", "total_cents"}, "one number; no run, customer or name"


# --- fail closed -----------------------------------------------------------------------


def test_an_unreachable_ledger_refuses_and_never_echoes_the_uri() -> None:
    client = FakeMongoClient(down=True)
    with pytest.raises(SpendCapError) as refused:
        _guard(_ledger(client)).check_before(1)
    assert "hunter2" not in str(refused.value)
    assert refused.value.__cause__ is None


def test_a_corrupt_total_reads_as_unreadable_not_as_zero() -> None:
    client = FakeMongoClient()
    client[UPSTREAM_SPEND_DB][UPSTREAM_SPEND_COLLECTION].insert_one(
        {"_id": "orthogonal", "total_cents": "lots"}
    )
    ledger = _ledger(client)

    with pytest.raises(SpendCapError, match="unreadable"):
        ledger.total_cents()
    with pytest.raises(SpendCapError):
        _guard(ledger).check_before(1)


def test_a_bound_larger_than_the_cap_is_refused_without_a_write() -> None:
    client = FakeMongoClient()
    with pytest.raises(SpendCapError):
        _guard(_ledger(client), total_cap=10, per_run_cap=50).check_before(11)
    assert _ledger(client).total_cents() == 0


# --- file fallback unchanged -----------------------------------------------------------


def test_the_file_ledger_is_not_a_reserving_ledger(tmp_path: Path) -> None:
    assert isinstance(_ledger(), ReservingLedger)
    assert not isinstance(FileLedger(tmp_path / "l.json"), ReservingLedger)


def test_the_file_ledger_still_checks_then_adds(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    guard = _guard(FileLedger(path), total_cap=10)

    guard.check_before(10)
    assert not path.exists(), "a file-ledger check writes nothing"
    guard.record(4)
    assert FileLedger(path).total_cents() == 4

    with pytest.raises(SpendCapError):
        _guard(FileLedger(path), total_cap=10).check_before(7)
    assert FileLedger(path).total_cents() == 4


def test_an_affordability_probe_holds_nothing() -> None:
    """``Runner.affordable`` asks before a failover and may never make the call."""
    ledger = _ledger()
    guard = _guard(ledger, total_cap=10)

    guard.check_fits(10)
    assert ledger.total_cents() == 0 and guard.held == 0
    with pytest.raises(SpendCapError):
        guard.check_fits(11)


# --- the row already exists (live bug, 9 Oct) ------------------------------------------


def _driver_like_ledger(client: FakeMongoClient) -> MongoSpendLedger:
    """pymongo's ``DuplicateKeyError`` IS a ``PyMongoError``, so in production the
    ledger's driver-error guard sees it. The fake's duplicate is listed the same way."""
    return MongoSpendLedger(
        client, unavailable=(FakeUnavailableError, FakeDuplicateKeyError)
    )


def test_pymongo_duplicate_key_is_a_driver_error() -> None:
    errors = pytest.importorskip("pymongo.errors")
    assert issubclass(errors.DuplicateKeyError, errors.PyMongoError)


def test_a_fresh_ledger_object_reserves_on_a_row_that_already_exists() -> None:
    """Live, 9 Oct: total 0 of cap 50, a 4-cent call refused with ``spend_cap``. The
    zero row existed, so ``_ensure_row``'s insert raised a duplicate; the driver-error
    guard turned it into "the ledger did not answer" before the duplicate was read."""
    client = FakeMongoClient()
    _driver_like_ledger(client).add(0)  # an earlier task made the row
    guard = SpendGuard(_driver_like_ledger(client), per_run_cap=8, total_cap=50)

    guard.check_before(4)
    guard.record(4)

    assert _driver_like_ledger(client).total_cents() == 4
