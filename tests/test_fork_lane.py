"""The fork lane: lease → sign+land on a fork → judge by what MOVED → record verdict.

Load-bearing properties, each with a test:
  * a landed, balanced rehearsal is the fork green (first_call_correct=True);
  * a landed-but-UNBALANCED rehearsal is a FAIL — landing is not correctness;
  * the recorded row is payload-free, source="simulated", network="fork" (never
    the observed FCC rate), and the moved amounts are on the ForkRun, NOT the row;
  * every ForkRun is sandbox=True and carries the "does not prove" caveats;
  * a plaintext key is refused at the writer;
  * the score reads the fork pass on the simulate/land rate at its own tier;
  * the warm pool never hands the same worker to two callers, resets on return,
    and fails closed when drained.
"""

from __future__ import annotations

import pytest

from gecko.fork_lane import (
    FORK_NETWORK,
    ForkRun,
    ForkWorkerError,
    WarmForkPool,
    rehearse_on_pool,
    run_fork_purchase,
)
from gecko.sandbox.rehearse import LamportDelta, Rehearsal, TokenDelta, WrittenReceipt
from gecko.store import InMemoryCollection, ProjectionError, endpoint_score

SURFACE = "orquestra:let_me_buy"
PROGRAM = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
HASH = "b" * 64


def _rehearsal(
    *,
    landed: bool = True,
    discrepancies: tuple[str, ...] = (),
    buyer_after_tokens: int = 0,
) -> Rehearsal:
    """A geckocoffee Sparkling-water purchase, priced 0.05 USDC = 50_000 base units."""
    return Rehearsal(
        store="geckocoffee",
        product="Sparkling water",
        price_raw=50_000,
        mint=MINT,
        buyer="BuyerPubkey",
        landed=landed,
        signature="5jwHoU3" if landed else None,
        simulated_units=41_900,
        units_consumed=41_900,
        fee_lamports=5_000,
        buyer_token=TokenDelta(
            account="buyer_ata",
            owner="BuyerPubkey",
            mint=MINT,
            before=50_000,
            after=buyer_after_tokens,
        ),
        store_token=TokenDelta(
            account="store_ata",
            owner="Authority",
            mint=MINT,
            before=1_000_000,
            after=1_050_000,
        ),
        buyer_sol=LamportDelta(
            address="BuyerPubkey", before=50_000_000, after=49_995_000
        ),
        receipt=WrittenReceipt(
            receipt_id=42,
            product_name="Sparkling water",
            price_raw=50_000,
            table_number=1,
            delivered=False,
            total_purchases_before=0,
            total_purchases_after=1,
        )
        if landed
        else None,
        discrepancies=discrepancies,
    )


class _FakeWorker:
    """A leased fork that returns a canned rehearsal and records what it was asked."""

    def __init__(self, rehearsal: Rehearsal) -> None:
        self._rehearsal = rehearsal
        self.calls: list[tuple[str, str]] = []

    def rehearse(self, *, store: str, product: str) -> Rehearsal:
        self.calls.append((store, product))
        return self._rehearsal


def _run(
    outcomes: InMemoryCollection,
    rehearsal: Rehearsal,
    *,
    api_key_id: str = HASH,
) -> ForkRun:
    return run_fork_purchase(
        _FakeWorker(rehearsal),
        outcomes,
        surface_id=SURFACE,
        program_id=PROGRAM,
        instruction="make_purchase",
        store="geckocoffee",
        product="Sparkling water",
        recipe_hash="c" * 64,
        api_key_id=api_key_id,
        run_id="run-fork-1",
        ts=0,
    )


def test_landed_and_balanced_is_the_fork_green() -> None:
    outcomes = InMemoryCollection()
    run = _run(outcomes, _rehearsal())

    assert run.status == "pass"
    assert run.first_call_correct is True
    assert run.landed is True
    assert run.balanced is True
    # What MOVED is on the result, for the caller to show.
    assert run.moved_buyer_tokens == -50_000
    assert run.moved_store_tokens == 50_000
    assert run.moved_buyer_lamports == -5_000  # just the fee; token moved off-SOL
    assert run.fee_lamports == 5_000
    assert run.price_raw == 50_000


def test_landed_but_unbalanced_is_a_fail_not_a_green() -> None:
    # It landed, but the store objected: the buyer did not fall by the price. Landing
    # is not correctness — the fork lane refuses to grade this as a pass.
    outcomes = InMemoryCollection()
    run = _run(
        outcomes,
        _rehearsal(
            discrepancies=("buyer fell 40000, expected 50000",),
            buyer_after_tokens=10_000,
        ),
    )

    assert run.landed is True
    assert run.status == "fail"
    assert run.first_call_correct is False
    assert run.balanced is False
    assert run.discrepancies == ("buyer fell 40000, expected 50000",)


def test_never_landed_leaves_balanced_unanswered() -> None:
    outcomes = InMemoryCollection()
    run = _run(outcomes, _rehearsal(landed=False))

    assert run.landed is False
    assert run.status == "fail"
    # None, not False: an unanswered question is not a negative answer.
    assert run.balanced is None
    assert run.signature is None


def test_recorded_row_is_payload_free_simulated_and_fork() -> None:
    outcomes = InMemoryCollection()
    _run(outcomes, _rehearsal())

    rows = list(outcomes.find({}))
    assert len(rows) == 1
    row = rows[0]
    # Its own evidence tier, its own network label — never the observed FCC feed.
    assert row["source"] == "simulated"
    assert row["network"] == FORK_NETWORK
    assert row["status"] == "pass"
    # The moved amounts and the buyer address are NOT in the record — only the
    # categorical verdict crossed the boundary.
    serialized = repr(row)
    assert "BuyerPubkey" not in serialized
    assert "buyer_ata" not in serialized
    assert "50000" not in serialized  # the price/value never enters the corpus
    assert "run_id" in row and "api_key_id" in row


def test_every_run_is_sandbox_and_carries_caveats() -> None:
    outcomes = InMemoryCollection()
    run = _run(outcomes, _rehearsal())

    assert run.sandbox is True
    assert len(run.what_this_does_not_prove) == 3
    assert any("not a mainnet pass" in c for c in run.what_this_does_not_prove)
    assert any("cheatcode" in c for c in run.what_this_does_not_prove)


def test_plaintext_key_is_refused() -> None:
    outcomes = InMemoryCollection()
    with pytest.raises(ProjectionError):
        _run(outcomes, _rehearsal(), api_key_id="gecko_sk_" + "x" * 43)
    # And nothing was written on the way to the refusal.
    assert list(outcomes.find({})) == []


def test_fork_pass_feeds_the_simulate_land_rate() -> None:
    outcomes = InMemoryCollection()
    # Above the N-floor so the rate reports rather than staying None.
    for _ in range(25):
        _run(outcomes, _rehearsal())

    score = endpoint_score(
        outcomes,
        surface_id=SURFACE,
        operation_id="make_purchase",
        spec_rev="sr_whatever",
    )
    assert score.n_simulated == 25
    assert score.simulate_land == 1.0
    # It never touched the observed FCC rate.
    assert score.first_call_correct is None
    assert score.n == 0


def test_worker_is_asked_for_the_right_purchase() -> None:
    outcomes = InMemoryCollection()
    worker = _FakeWorker(_rehearsal())
    run_fork_purchase(
        worker,
        outcomes,
        surface_id=SURFACE,
        program_id=PROGRAM,
        instruction="make_purchase",
        store="geckocoffee",
        product="Sparkling water",
        recipe_hash="c" * 64,
        api_key_id=HASH,
        run_id="run-fork-1",
        ts=0,
    )
    assert worker.calls == [("geckocoffee", "Sparkling water")]


# --- the warm pool seam ---


def test_pool_leases_one_worker_at_a_time_and_returns_it() -> None:
    a, b = _FakeWorker(_rehearsal()), _FakeWorker(_rehearsal())
    pool = WarmForkPool(workers=[a, b])
    assert pool.available == 2

    with pool.lease() as first:
        assert pool.available == 1
        with pool.lease() as second:
            assert pool.available == 0
            assert first is not second  # never the same worker to two callers
    # Both released on exit.
    assert pool.available == 2


def test_pool_fails_closed_when_drained() -> None:
    pool = WarmForkPool(workers=[_FakeWorker(_rehearsal())])
    with pool.lease():
        with pytest.raises(ForkWorkerError):
            with pool.lease():
                pass


def test_pool_resets_on_return_even_after_error() -> None:
    reset: list[object] = []
    pool = WarmForkPool(
        workers=[_FakeWorker(_rehearsal())],
        on_return=reset.append,
    )
    with pytest.raises(ValueError):
        with pool.lease() as worker:
            raise ValueError("boom")
    assert reset == [worker]  # the reset hook ran despite the exception
    assert pool.available == 1


def test_warm_requires_at_least_one_worker() -> None:
    with pytest.raises(ForkWorkerError):
        WarmForkPool.warm(lambda: _FakeWorker(_rehearsal()), size=0)


def test_rehearse_on_pool_runs_a_purchase_and_returns_the_worker() -> None:
    outcomes = InMemoryCollection()
    pool = WarmForkPool.warm(lambda: _FakeWorker(_rehearsal()), size=2)
    run = rehearse_on_pool(
        pool,
        outcomes,
        surface_id=SURFACE,
        program_id=PROGRAM,
        instruction="make_purchase",
        store="geckocoffee",
        product="Sparkling water",
        recipe_hash="c" * 64,
        api_key_id=HASH,
        run_id="run-fork-1",
        ts=0,
    )
    assert run.status == "pass"
    assert pool.available == 2  # leased and returned
    assert len(list(outcomes.find({}))) == 1
