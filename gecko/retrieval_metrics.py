"""recall@k and MRR, once — with the POPULATION carried next to the number.

Three modules computed these independently before this one existed, and they did not
disagree on the arithmetic. They disagreed on the DENOMINATOR, each for a documented
reason:

* ``evaluate._recall_mrr`` divides by every positive task, misses included — "how often
  does an agent asking this get the right op".
* ``retrieval_eval`` divides by *scoreable* rows only, because "un-retrievable golds in
  recall would conflate wiring with ranking" — "how good is the RANKER on what it could
  possibly find".
* ``fcc_eval.retrieval_recall_at_k`` divides by tasks that produced a record, deduped —
  "what CEILING does retrieval put on first-call correctness".

All three are real questions and none is wrong. What was wrong is that all three were
called "recall" and reported as one kind of number, so a reader could compare two figures
that answer different questions and conclude something about neither.

So this module does not unify them. It makes the population **impossible to omit**:
:class:`RetrievalScore` cannot be constructed without naming it, and renders it beside
the value. A figure that travels without its denominator is how the 347x got believed,
and it is how a paraphrase recall of 0.00 got read as a ranker weakness when it was the
fixture's own definition.

MISSES ARE RANKS OF ``None``, never dropped. Silently excluding them is the single
easiest way to turn a bad retrieval number into a good one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

__all__ = ["Population", "RetrievalScore", "recall_at", "mrr", "score"]

#: Which rows the denominator counts. Named, not inferred — see the module docstring.
Population = Literal["all_positive", "scoreable_only", "recorded_deduped"]

_POPULATION_MEANING: Mapping[Population, str] = {
    "all_positive": "every positive task, misses included",
    "scoreable_only": "positive tasks whose gold op is reachable at all",
    "recorded_deduped": "tasks that produced a run record, deduped per task",
}


@dataclass(frozen=True)
class RetrievalScore:
    """A retrieval figure that cannot be quoted without its denominator."""

    population: Population
    n: int
    recall_at: Mapping[int, float]
    mrr: float

    @property
    def means(self) -> str:
        """Plain-language denominator, for a report line or a scorecard cell."""
        return _POPULATION_MEANING[self.population]

    def line(self, k: int = 3) -> str:
        """One reportable line. The population is not optional here either."""
        value = self.recall_at.get(k)
        shown = "n/a" if value is None else f"{value:.3f}"
        return f"recall@{k}={shown} mrr={self.mrr:.3f} (n={self.n}, {self.means})"


def recall_at(ranks: Sequence[int | None], k: int) -> float:
    """Fraction of ``ranks`` at or above ``k``. A ``None`` rank is a miss, and counts."""
    if not ranks:
        return 0.0
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks)


def mrr(ranks: Sequence[int | None]) -> float:
    """Mean reciprocal rank; a miss contributes 0 rather than being dropped."""
    if not ranks:
        return 0.0
    return sum((1.0 / r) if r else 0.0 for r in ranks) / len(ranks)


def score(
    ranks: Sequence[int | None],
    *,
    population: Population,
    ks: Sequence[int] = (1, 3, 5, 10),
) -> RetrievalScore:
    """Score ``ranks`` under a NAMED population. There is no default population on
    purpose: choosing one silently is the mistake this module exists to prevent."""
    return RetrievalScore(
        population=population,
        n=len(ranks),
        recall_at={k: recall_at(ranks, k) for k in ks},
        mrr=mrr(ranks),
    )
