"""The rerank seam — a second pass over candidates the first pass already found.

WHAT A RERANKER IS FOR HERE, AND IT IS NOT WHAT THE SPEC ASSUMED. The 2026-07-13
plan specified rerank as "compose over the fused top-N pool; inherit the OOS
floor (rerank re-orders, never promotes across the floor)". That is re-ordering,
and re-ordering alone cannot buy what this engine is missing. Measured against
Atlas on 2026-09-23: the hybrid arm already puts the gold op in the top 8 for
EVERY paraphrase task on txodds and pegana. The candidate is in the pool. Moving
it from rank 4 to rank 1 is worth something, but the recall number that made the
dense arm look useless was never about order.

The measured problem is the other one. Under the ``retrieved`` reading, hybrid's
out-of-scope pass rate falls from 1.00 to 0.00: the dense arm answers an
out-of-scope query as readily as an in-scope one, because ``voyage-4-lite``
cosine scores sit in a band about 0.005 wide across the whole pool. Cosine is a
RELATIVE measure -- it ranks -- and confidence needs an ABSOLUTE one.

So a reranker is interesting here for a reason the spec did not name: a
cross-encoder reads the query and the candidate together and scores THAT pair,
which is the kind of number a floor can be built on. Whether any particular
reranker actually produces a separable score on this corpus is a measurement, and
this module exists so that measurement can be made without buying a model first.

WHAT SHIPS TODAY: the seam, a deterministic identity, and an oracle. The oracle
is the point. Before adding a dependency, run it and read the ceiling: if a
perfect reranker cannot move the number you care about, no real one will either.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Reranker(Protocol):
    """Injected second pass: ``(query, candidate names) -> reordered names``.

    Names, not hits, on purpose. The fusion layer already joins by name
    (``gecko/search.py``), the dense index is string-keyed
    (``DenseIndex.search -> list[tuple[str, float]]``), and a reranker that
    accepted hit objects would couple this seam to whichever arm produced them.

    A reranker MAY drop a candidate. It must never invent one: a name that was
    not in ``candidates`` cannot appear in the result, and a caller is entitled to
    assume that without checking.
    """

    def rerank(
        self, query: str, candidates: Sequence[str], top_k: int
    ) -> list[str]: ...


@dataclass(frozen=True)
class IdentityReranker:
    """Returns the pool as it arrived, truncated. The control arm.

    Every rerank measurement is a comparison against this, so that "the reranker
    helped" can never be confused with "the pool was already right".
    """

    def rerank(self, query: str, candidates: Sequence[str], top_k: int) -> list[str]:
        return list(candidates[:top_k])


@dataclass(frozen=True)
class OracleReranker:
    """Knows the right answer FOR THIS QUERY, and is therefore a ceiling, not a reranker.

    Per query, not per set. A first version of this took the union of every gold
    op in the fixture, which quietly made every out-of-scope query look answerable:
    the pool still contained something that was gold for a DIFFERENT task, so the
    oracle never had to refuse and reported a ceiling that did not exist. An oracle
    that is wrong is worse than no oracle, because it is believed.

    Measured on 2026-09-23 over txodds and pegana: perfect ordering buys almost
    nothing. txodds paraphrase is already 1.00 at rank 1 without it, pegana does
    not move at rank 1 at all, and out-of-scope refusal is unchanged. Reordering
    cannot refuse: a permutation of a wrong pool is still a wrong pool.
    """

    gold_by_query: Mapping[str, frozenset[str]]

    def rerank(self, query: str, candidates: Sequence[str], top_k: int) -> list[str]:
        gold = self.gold_by_query.get(query, frozenset())
        hits = [name for name in candidates if name in gold]
        rest = [name for name in candidates if name not in gold]
        return (hits + rest)[:top_k]


@dataclass(frozen=True)
class RefusingOracleReranker:
    """The ceiling for a second pass that is allowed to say "none of these".

    This is the one that matters, and the gap between it and ``OracleReranker`` is
    the entire case for buying a reranker. Measured on the same run:

        arm                          paraphrase retrieved@8    out-of-scope pass
        hybrid                              1.00                     0.00
        hybrid + ordering oracle            1.00                     0.00
        hybrid + THIS                       1.00                     1.00

    Full recall and full refusal at the same time, which neither the lexical arm
    nor the dense arm can reach alone. The dense arm finds every paraphrase and
    also answers every out-of-scope query, because cosine similarity RANKS and
    confidence needs an ABSOLUTE judgement of whether this candidate answers this
    query at all.

    So what to shop for is not a re-orderer. It is a scorer whose number means
    something on its own, and whose threshold can be fitted and then measured here.
    """

    gold_by_query: Mapping[str, frozenset[str]]

    def rerank(self, query: str, candidates: Sequence[str], top_k: int) -> list[str]:
        gold = self.gold_by_query.get(query, frozenset())
        hits = [name for name in candidates if name in gold]
        if not hits:
            return []
        rest = [name for name in candidates if name not in gold]
        return (hits + rest)[:top_k]


def reorder(hits: Sequence[object], names: Sequence[str]) -> list[object]:
    """Apply a rerank result to scored hits, keeping every hit object intact.

    The reranker speaks in names; the caller holds hits carrying score, provenance
    and confidence flags. Rebuilding those from a name would lose exactly the
    fields a measurement needs, so this reorders the originals and drops nothing
    the reranker kept.
    """
    by_name = {getattr(hit, "name", None): hit for hit in hits}
    return [by_name[name] for name in names if name in by_name]
