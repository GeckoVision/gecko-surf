"""The fork lane — the "see what would MOVE" escalation, on a throwaway key.

Stage 4 of the hosted flow (architecture §0, §4). Where the recorded lane
(:mod:`gecko.recorded_lane`) SIMULATES the exact bytes read-only and never signs,
the fork lane goes one step further: it leases a colocated surfpool worker, funds
a throwaway key by cheatcode, SIGNS and lands the purchase on that private fork,
and judges the result by what actually MOVED on the ledger — the buyer fell by
exactly the price, the store rose by it, a receipt was written. It is the
one-click escalation from the recorded green, and it still spends nobody's money:
the fork cannot reach mainnet and the key is ephemeral.

Two boundaries this module keeps, and they are the whole point:

* **The RECORD stays payload-free.** What lands in the corpus is a
  :class:`~gecko.corpus.SimulatedOutcome` — status, a closed revert family,
  compute units, slot, network — built by :func:`~gecko.corpus.simulated_outcome_from`,
  which reads ONLY those categorical fields off the rehearsal. The "what moved"
  amounts live on the returned :class:`ForkRun` for the caller to SHOW, and are
  never written. ``source`` is fixed to ``"simulated"`` on the outcome, so a fork
  pass feeds the simulate/land rate at its own (weaker-than-mainnet) evidence tier
  and can never inflate a provider's observed FCC number.
* **This lane never holds a key.** It receives a leased worker (the
  :class:`ForkWorker` seam) whose proof is its OWN 127.0.0.1 loopback; the key
  authorizes COMPUTE, not spend. The API tier that calls this never talks to a
  fork directly — it dispatches here, and here dispatches to the pool.

Pattern B: the rehearsal is injected (a :class:`ForkWorker`), so the whole lane is
falsifiable offline with a fake worker and no surfpool. In production the worker
is a colocated ``surfpool`` + ephemeral-signer sidecar (see :class:`WarmForkPool`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol

from gecko.corpus import SimulatedOutcome, simulated_outcome_from
from gecko.sandbox.rehearse import Rehearsal
from gecko.simulate import Receipt
from gecko.store.collections import Collection
from gecko.store.projections import record_simulated_outcome

#: The label every fork outcome carries. A closed token, never an RPC URL — the
#: store must never learn which loopback served the rehearsal.
FORK_NETWORK = "fork"

#: The standing caveats on ANY fork pass, attached to every :class:`ForkRun` so the
#: playground cannot render a sandbox green as a mainnet one. These are facts about
#: the lane, not about a particular purchase.
_FORK_CAVEATS: tuple[str, ...] = (
    "A fork pass is not a mainnet pass: it ran against forked state with a "
    "throwaway signer, and the fork cannot reach mainnet.",
    "The lamports moved were funded by cheatcode — no real value changed hands.",
    "The blockhash window on a fork is not the ~60-second window a real buyer "
    "races; do not read landing here as proof the live window is comfortable.",
)


class ForkWorkerError(Exception):
    """A leased fork worker could not produce a rehearsal. Fails closed."""


class ForkWorker(Protocol):
    """A leased fork: prove-own-loopback, fund-by-cheatcode, sign-and-land, judge.

    The single method the lane needs. A production worker wraps
    :func:`gecko.sandbox.rehearse.rehearse_purchase` against its own colocated
    ``surfpool`` and an :func:`~gecko.sandbox.signer.ephemeral_signer`; a test
    worker returns a canned :class:`~gecko.sandbox.rehearse.Rehearsal`. Either
    way the lane treats the rehearsal as untrusted evidence and reads only its
    categorical verdict into the corpus.
    """

    def rehearse(self, *, store: str, product: str) -> Rehearsal:
        """Sign and land ``product`` from ``store`` on this worker's fork."""
        ...


@dataclass(frozen=True)
class ForkRun:
    """The gradable result of one fork purchase — a receipt summary plus what MOVED.

    The ``moved_*`` fields are per-transaction ledger deltas. They are the reason
    the fork lane exists (a purchase is "correct" only if the buyer fell by exactly
    the price and the store rose by it), and they are shown to the caller — but they
    are NEVER recorded: the corpus row built alongside this carries only the
    categorical verdict. ``sandbox`` is always ``True`` and
    ``what_this_does_not_prove`` always carries :data:`_FORK_CAVEATS`, so a caller
    cannot present this as anything but a sandbox rehearsal.
    """

    surface_id: str
    instruction: str
    status: str  # "pass" | "fail" | "unknown"
    landed: bool
    balanced: (
        bool | None
    )  # None when it never landed — an unanswered question, not "no"
    units_consumed: int | None
    fee_lamports: int | None
    moved_buyer_lamports: int | None
    moved_buyer_tokens: int | None
    moved_store_tokens: int | None
    price_raw: int
    discrepancies: tuple[str, ...]
    signature: str | None
    network: str
    first_call_correct: bool
    recorded: bool
    sandbox: bool = True
    what_this_does_not_prove: tuple[str, ...] = _FORK_CAVEATS


def _status_of(rehearsal: Rehearsal) -> str:
    """The land/no-land/balanced verdict, collapsed to the corpus's closed set.

    A pass requires BOTH landing and an empty discrepancy list: a transaction that
    lands but leaves the buyer down the wrong amount is a semantic failure, not a
    success, and the fork lane refuses to call it one.
    """
    if rehearsal.landed and not rehearsal.discrepancies:
        return "pass"
    return "fail"


def _receipt_of(rehearsal: Rehearsal, status: str) -> Receipt:
    """A minimal :class:`Receipt` carrying ONLY what the corpus reads off it.

    :func:`~gecko.corpus.simulated_outcome_from` touches ``status``,
    ``revert_class`` and ``units_consumed`` and nothing else, so the per-user
    fields are left empty rather than copied from the rehearsal — the "what moved"
    amounts never enter the corpus path even by accident. ``revert_class`` is
    ``None``: a fork discrepancy is a ledger objection, not a program revert we can
    classify, and the verdict it carries lives in ``status``.
    """
    return Receipt(
        status=status,  # type: ignore[arg-type]
        err=None,
        revert_class=None,
        units_consumed=rehearsal.units_consumed,
        sol_delta=None,
        tokens_received=None,
        logs_tail=(),
        network_label=FORK_NETWORK,
    )


def run_fork_purchase(
    worker: ForkWorker,
    outcomes: Collection,
    *,
    surface_id: str,
    program_id: str,
    instruction: str,
    store: str,
    product: str,
    recipe_hash: str,
    api_key_id: str,
    run_id: str,
    ts: int,
) -> ForkRun:
    """Lease a fork, sign+land the purchase on it, record the verdict, return the moves.

    ``api_key_id`` must be a key HASH (the writer refuses a plaintext). The recorded
    row is ``source="simulated"`` and ``network="fork"`` — it feeds the simulate/land
    rate at the fork evidence tier and never the observed FCC rate. The returned
    :class:`ForkRun` carries the moved amounts for the caller to display; those are
    not in the recorded row.
    """
    rehearsal = worker.rehearse(store=store, product=product)
    status = _status_of(rehearsal)
    outcome: SimulatedOutcome = simulated_outcome_from(
        _receipt_of(rehearsal, status),
        program_id=program_id,
        instruction=instruction,
        recipe_hash=recipe_hash,
        slot=None,
        network=FORK_NETWORK,
        ts=ts,
        surface_id=surface_id,
    )
    record_simulated_outcome(outcomes, outcome, run_id=run_id, api_key_id=api_key_id)
    return ForkRun(
        surface_id=surface_id,
        instruction=instruction,
        status=status,
        landed=rehearsal.landed,
        balanced=rehearsal.balanced,
        units_consumed=rehearsal.units_consumed,
        fee_lamports=rehearsal.fee_lamports,
        moved_buyer_lamports=(
            rehearsal.buyer_sol.moved if rehearsal.buyer_sol else None
        ),
        moved_buyer_tokens=(
            rehearsal.buyer_token.moved if rehearsal.buyer_token else None
        ),
        moved_store_tokens=(
            rehearsal.store_token.moved if rehearsal.store_token else None
        ),
        price_raw=rehearsal.price_raw,
        discrepancies=rehearsal.discrepancies,
        signature=rehearsal.signature,
        network=outcome.network,
        first_call_correct=status == "pass",
        recorded=True,
    )


#: A production factory makes one leasable worker (spin up / prove a colocated
#: surfpool, attach the ephemeral signer). Injected so the pool has no surfpool
#: dependency in tests.
WorkerFactory = Callable[[], ForkWorker]


@dataclass
class WarmForkPool:
    """A fixed set of leasable fork workers, handed out one at a time.

    The safety wall from architecture §0: the API tier never holds a key or talks
    to a fork — it borrows a worker from here, and each worker proves its OWN
    127.0.0.1 loopback. This class is the *seam*, deliberately thin: it owns lease
    accounting (never hand the same worker to two callers at once) and the
    restart-on-return hook. It does NOT manage the ``surfpool`` process lifecycle
    or pool sizing — that is the production binding of :data:`WorkerFactory` and is
    devops-owned, exactly as :class:`~gecko.store.collections.Collection` is the
    seam and Mongo is the binding.

    Construct with a pre-warmed set of workers, or via :meth:`warm` to build them
    from a factory. ``on_return`` runs when a lease is released (in production:
    reset the fork to a clean slot so the next lease starts from known state).
    """

    workers: list[ForkWorker]
    on_return: Callable[[ForkWorker], None] | None = None
    _available: list[ForkWorker] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # A copy: leasing mutates availability, the declared set stays the roster.
        self._available = list(self.workers)

    @classmethod
    def warm(cls, factory: WorkerFactory, *, size: int) -> WarmForkPool:
        """Build ``size`` workers up front — the "warm" in warm pool.

        Warming is why the fork escalation feels instant: the surfpool forks are
        already proven and funded when the first lease arrives, so a click does not
        pay the cold-start cost.
        """
        if size < 1:
            raise ForkWorkerError("a warm pool needs at least one worker")
        return cls(workers=[factory() for _ in range(size)])

    @property
    def available(self) -> int:
        """How many workers are free to lease right now."""
        return len(self._available)

    @contextmanager
    def lease(self) -> Iterator[ForkWorker]:
        """Borrow one worker for the duration of the block; return it on exit.

        Fails closed when the pool is drained rather than silently sharing a worker
        between two purchases — a shared fork would let one caller's landed state
        contaminate another's verdict. On exit the worker is returned and
        ``on_return`` (the reset-to-clean hook) runs, even if the body raised.
        """
        if not self._available:
            raise ForkWorkerError(
                "fork pool is drained — no worker free to lease; size the pool for "
                "peak concurrent rehearsals"
            )
        worker = self._available.pop()
        try:
            yield worker
        finally:
            if self.on_return is not None:
                self.on_return(worker)
            self._available.append(worker)


def rehearse_on_pool(
    pool: WarmForkPool,
    outcomes: Collection,
    *,
    surface_id: str,
    program_id: str,
    instruction: str,
    store: str,
    product: str,
    recipe_hash: str,
    api_key_id: str,
    run_id: str,
    ts: int,
) -> ForkRun:
    """Lease a worker from ``pool`` and run one fork purchase on it, then return it.

    The convenience the BFF calls: it never sees a worker, only asks the pool for a
    fork purchase and gets back a :class:`ForkRun`. The lease is held only for the
    rehearsal and released (and reset) immediately after.
    """
    with pool.lease() as worker:
        return run_fork_purchase(
            worker,
            outcomes,
            surface_id=surface_id,
            program_id=program_id,
            instruction=instruction,
            store=store,
            product=product,
            recipe_hash=recipe_hash,
            api_key_id=api_key_id,
            run_id=run_id,
            ts=ts,
        )


__all__: Sequence[str] = (
    "FORK_NETWORK",
    "ForkWorker",
    "ForkWorkerError",
    "ForkRun",
    "run_fork_purchase",
    "WorkerFactory",
    "WarmForkPool",
    "rehearse_on_pool",
)
