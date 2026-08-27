"""The control that has to light up before a measurement is allowed to be a result.

THE RULE. Before a number counts, push one KNOWN-POSITIVE input through the same path
that produced it and show it comes back non-null. If the control stays dark, the run is
UNINTERPRETABLE — which is not the same as negative, and is the distinction every bug
below collapsed.

WHY IT IS A MODULE. This discipline was independently invented four times here, each time
after being bitten, none of them named and none reusable:

  * ``tests/test_tier_eval.py`` — "Sanity: the transfer class is actually exercised (not a
    vacuous pass)", because an empty label join returned precision 1.0 and cleared a gate.
  * ``scripts/retrieval_arms_eval.py::_fixture_rev`` — stamps the golden set's digest,
    because a re-partition (near_dup 13 -> 9 tasks) read as a product regression.
  * ``gecko.score`` — refuses to pool unlike surfaces, computes a noise floor, and keeps
    ``no_difference`` (measured zero) apart from ``undetermined`` (unmeasurable).
  * ``gecko.fcc_eval.per_api_lift`` — ``arm_absent`` / ``below_floor`` / ``unpaired``
    refusals, and ``lift_for`` returning ``None`` rather than ``0.0``.

Four inventions of one idea is a missing module. This is it. It measures nothing itself —
it decides whether what a caller measured is readable — so it stays a pure function of its
arguments: no I/O beyond hashing a corpus file the caller hands it, no network, no state.

THE FOUR THINGS IT CHECKS, each traceable to a shipped bug:

1. **Denominators are non-empty**, and clear whatever floor the caller declares. An empty
   corpus printed ``0/0 = 0%`` and was read as "we measured, and found nothing".
2. **The join covers everything the caller claims it joined.** A join on the wrong key matched
   3 of 40 labels; the resulting 7% was mistaken for a finding. The refusal prints a
   sample of both key shapes, because that is what turns the coverage number back into
   the wiring bug it is.
3. **The arms can actually diverge.** A "controlled comparison" once differenced two
   values that were the same tick: a zero by construction, unfixable by more data. Two
   arms must answer the known positive DIFFERENTLY before their difference means anything.
4. **A rate over nothing is ``None``, never 1.0 and never 0.0.** ``rate`` is the one-line
   version of the rule ``gecko.score`` and ``gecko.evaluate.TierEval`` already state in
   prose: a measured zero is a result, an unmeasurable one is silence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Literal, Mapping, Sequence

__all__ = [
    "Control",
    "CorpusRev",
    "Joined",
    "Signal",
    "Uninterpretable",
    "UninterpretableReason",
    "corpus_rev",
    "is_silent",
    "rate",
    "require_signal",
]

#: Closed vocabulary, for the same reason ``fcc_eval.RefusalReason`` is closed: a refusal
#: must name itself from a fixed set, or "we could not answer" gets quietly restyled into
#: a finding by whoever renders it next.
UninterpretableReason = Literal[
    "empty_denominator",
    "below_floor",
    "join_shortfall",
    "control_silent",
    "arms_identical",
]

#: How many keys each side of a failed join prints. Enough to SEE the shapes differ
#: (``privy:transfer`` vs ``transfer``); short enough to stay in one error message.
_KEY_SAMPLE = 3


class Uninterpretable(RuntimeError):
    """The run cannot be read as a result — in either direction.

    Deliberately not a "measurement failed" error: the measurement may have completed and
    produced a number. This says the number has no interpretation.
    """

    def __init__(
        self, reason: UninterpretableReason, measuring: str, detail: str
    ) -> None:
        super().__init__(
            f"{measuring}: UNINTERPRETABLE ({reason}) — {detail}. This is not a negative "
            f"result; nothing was measured that could have come out otherwise."
        )
        self.reason: UninterpretableReason = reason
        self.measuring = measuring
        self.detail = detail


def is_silent(value: object) -> bool:
    """Did the known positive fail to light the path up?

    Every shape of nothing counts: ``None``, an empty container, a zero rate, ``False``.
    A control scoring 0.0 is precisely the case this module exists to catch — the path ran
    and found nothing on an input chosen because it must be found.
    """
    if value is None or value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    try:
        return len(value) == 0  # type: ignore[arg-type]
    except TypeError:
        return False


@dataclass(frozen=True)
class CorpusRev:
    """The identity of the exact corpus a number was measured against.

    Two runs are comparable only when the digest matches. A changed digest means the
    QUESTION changed and the honest response is to re-baseline, not to read a trend — the
    size alone cannot say so, because a re-partition moves buckets without moving the
    count.
    """

    name: str
    digest: str
    items: int


def corpus_rev(path: str | Path, *, name: str | None = None) -> CorpusRev:
    """Stamp a corpus file: ``(name, short sha256, non-blank line count)``."""
    p = Path(path)
    raw = p.read_bytes()
    items = len([ln for ln in raw.splitlines() if ln.strip()])
    return CorpusRev(name or p.name, hashlib.sha256(raw).hexdigest()[:12], items)


@dataclass(frozen=True)
class Joined:
    """The join the caller says it made, and what actually came back.

    ``claimed`` is the population the caller believes it is measuring over; ``matched`` is
    what the join produced. ``min_coverage`` defaults to 1.0 — everything claimed must
    join — so a partial join has to be DECLARED, in the call, by someone who knows why.
    """

    name: str
    claimed: Collection[str]
    matched: Collection[str]
    min_coverage: float = 1.0

    @property
    def coverage(self) -> float | None:
        """``|matched ∩ claimed| / |claimed|``, or ``None`` when nothing was claimed."""
        claimed = set(self.claimed)
        if not claimed:
            return None
        return len(claimed & set(self.matched)) / len(claimed)


@dataclass(frozen=True)
class Control:
    """One known-positive input, pushed through the SAME path that produces the result.

    ``arms`` maps arm name -> a thunk that answers ``case`` through that arm. One arm asks
    only "does this path light up at all". Two or more also asks the question a comparison
    depends on and normally never checks: can these arms DIFFER? Arms wired (or
    monkeypatched, or cached) into the same source answer identically here, and their
    difference is a zero no amount of data will move.
    """

    case: str
    arms: Mapping[str, Callable[[], object]]
    silent: Callable[[object], bool] = is_silent

    def __post_init__(self) -> None:
        if not self.arms:
            raise ValueError(
                "a control needs at least one arm to run the known positive"
            )


@dataclass(frozen=True)
class Signal:
    """Evidence that a measurement is readable, carrying what made it so.

    Held onto rather than discarded because the qualifications are what get dropped when a
    number is copied into a document — see :meth:`sentence`.
    """

    measuring: str
    denominators: Mapping[str, int]
    floor: int
    coverage: float | None = None
    control_case: str | None = None
    control_answers: Mapping[str, str] = field(default_factory=dict)
    corpus: tuple[CorpusRev, ...] = ()

    def sentence(self) -> str:
        """The provenance line, RENDERED FROM THESE FIELDS.

        Same rule as ``fcc_eval.ApiLift.sentence``: the sentence beside a number is not
        written by hand, or it drifts from the number it describes.
        """
        parts = [
            self.measuring,
            "over "
            + ", ".join(f"{k}={v}" for k, v in sorted(self.denominators.items())),
        ]
        if self.coverage is not None:
            parts.append(f"join coverage {self.coverage * 100:.0f}%")
        if self.control_case is not None:
            lit = ", ".join(
                f"{arm}->{answer}"
                for arm, answer in sorted(self.control_answers.items())
            )
            parts.append(f"control {self.control_case!r} lit: {lit}")
        for rev in self.corpus:
            parts.append(f"corpus {rev.name}@{rev.digest} ({rev.items} items)")
        return " · ".join(parts)


def require_signal(
    measuring: str,
    *,
    denominators: Mapping[str, int] | None = None,
    floor: int = 1,
    joined: Joined | None = None,
    control: Control | None = None,
    corpus: Sequence[CorpusRev] = (),
) -> Signal:
    """Refuse to let ``measuring`` be read as a result unless it could have come out otherwise.

    Raises :class:`Uninterpretable` — never a bare ``False``, because a caller that ignores
    a return value gets the vacuous pass this module exists to stop. Checks run cheapest
    first, so the message names the most upstream cause rather than a symptom of it.
    """
    counts = dict(denominators or {})
    for name, n in sorted(counts.items()):
        if n <= 0:
            raise Uninterpretable(
                "empty_denominator",
                measuring,
                f"{name} is {n} — an empty set cannot produce a rate, and 0/0 is neither "
                f"0% nor 100%",
            )
        if n < floor:
            raise Uninterpretable(
                "below_floor",
                measuring,
                f"{name} is {n}, under the declared floor of {floor}; a rate over {n} has "
                f"resolution 1/{n}, coarser than the claim being made",
            )

    coverage = _check_join(measuring, joined)
    answers = _check_control(measuring, control)

    return Signal(
        measuring=measuring,
        denominators=counts,
        floor=floor,
        coverage=coverage,
        control_case=control.case if control else None,
        control_answers=answers,
        corpus=tuple(corpus),
    )


def _check_join(measuring: str, joined: Joined | None) -> float | None:
    if joined is None:
        return None
    claimed, matched = set(joined.claimed), set(joined.matched)
    if not claimed:
        raise Uninterpretable(
            "empty_denominator",
            measuring,
            f"join {joined.name!r} claimed nothing, so its coverage has no denominator",
        )
    # Matching keys nobody claimed means the two sides are not the same population — the
    # signature of a join on the wrong key, and invisible in a coverage ratio alone.
    stray = matched - claimed
    if stray:
        raise Uninterpretable(
            "join_shortfall",
            measuring,
            f"join {joined.name!r} matched {len(stray)} key(s) it never claimed "
            f"({_sample(stray)}) — the two sides are not the same population",
        )
    coverage = len(claimed & matched) / len(claimed)
    if coverage < joined.min_coverage:
        missing = claimed - matched
        raise Uninterpretable(
            "join_shortfall",
            measuring,
            f"join {joined.name!r} covered {coverage * 100:.0f}% "
            f"(declared floor {joined.min_coverage * 100:.0f}%): {len(missing)} of "
            f"{len(claimed)} claimed key(s) did not join. claimed like "
            f"{_sample(claimed)}; matched like {_sample(matched) or 'nothing'}",
        )
    return coverage


def _check_control(measuring: str, control: Control | None) -> dict[str, str]:
    if control is None:
        return {}
    answers = {arm: run() for arm, run in control.arms.items()}
    dark = sorted(arm for arm, ans in answers.items() if control.silent(ans))
    if dark:
        raise Uninterpretable(
            "control_silent",
            measuring,
            f"the known positive {control.case!r} came back empty through "
            f"{', '.join(dark)} — the path that produced this run's numbers does not "
            f"light up on an input chosen because it must",
        )
    if len(answers) > 1 and _all_equal(list(answers.values())):
        raise Uninterpretable(
            "arms_identical",
            measuring,
            f"arms {', '.join(sorted(answers))} answer the known positive "
            f"{control.case!r} identically, so any difference between them is zero by "
            f"construction and no amount of data would change it",
        )
    return {arm: repr(ans) for arm, ans in answers.items()}


def _all_equal(values: list[object]) -> bool:
    first = values[0]
    return all(v == first for v in values[1:])


def _sample(keys: Collection[str]) -> str:
    ordered = sorted(str(k) for k in keys)[:_KEY_SAMPLE]
    return ", ".join(repr(k) for k in ordered)


def rate(hits: int, total: int) -> float | None:
    """``hits/total``, or ``None`` when there is no denominator.

    ``None`` is could-not-answer; ``0.0`` is a measured zero and a real finding. Collapsing
    the two is how an empty label join returned a perfect 1.0 against a ship gate, and how
    an empty corpus printed ``0/0 = 0%`` as a result.
    """
    if total < 0 or hits < 0:
        raise ValueError(f"negative counts are not a rate: {hits}/{total}")
    if hits > total:
        raise ValueError(
            f"{hits} hits over {total} attempts is a wiring bug, not a rate — the "
            f"numerator and denominator are counting different populations"
        )
    if total == 0:
        return None
    return hits / total
