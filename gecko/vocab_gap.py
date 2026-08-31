"""Which words a user said that reach NO card — the input the blurb lever needs.

`retrieval_eval` already classifies a miss as `vocabulary_gap` when the query and the
gold card share no token. That answers *why* the row missed. It does not answer the only
question you can act on: **which words**.

The distinction is the whole point. Knowing that "buy an espresso but I only have USDG,
not USDC" was a vocabulary gap tells you to write a better card. Knowing that `usdg`
reaches zero of six cards, `convert` reaches zero, and `stablecoin` reaches zero tells
you what to write IN it. The second is authorable; the first is a shrug.

Measured on the six wired intents the day this module was written:

    usdg          0 cards
    convert       0 cards
    stablecoin    0 cards
    usdc          1 card   (metadao_ico.plan_fund — not a swap)
    swap          2 cards

So a user stating their constraint the way users state it — by naming the token they
hold — cannot reach a swap through any ranker. That is not a ranking failure and no
reranker fixes it: the tokens are absent from the haystack, so every arm scores zero by
arithmetic. It is the same shape as the golden set whose paraphrase archetype ENFORCES
zero overlap, except here nobody chose it.

WHAT THIS IS NOT. It is not a scoring change, a stopword list, or a synonym table. A
synonym table would let us map `usdg -> mint` and silently make the number better while
the agent still cannot tell a user which pool converts their balance. This measures the
gap and stops, because the fix belongs in what the cards SAY.

Offline, deterministic, no model. Reads the same folded tokens the live ranker reads —
measuring with the raw tokenizer reports plurals as gaps the real path already closes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .find_start import _card_terms, _query_tokens, _wired_cards, candidate_name

__all__ = ["TokenReach", "card_vocabulary", "gap_report", "unreached"]


@dataclass(frozen=True)
class TokenReach:
    """One query token and how many cards it can possibly match."""

    token: str
    cards: int
    #: How many of the supplied queries carried it — how much demand it represents.
    queries: int

    @property
    def unreachable(self) -> bool:
        return self.cards == 0


def card_vocabulary() -> dict[str, set[str]]:
    """Every wired card's folded terms, keyed by card name.

    Built from `_wired_cards` so this measures the haystack the ROUTER sees, not a
    parallel view of it that could drift into flattering us.
    """
    return {
        candidate_name(c.api_id, c.instruction): _card_terms(c) for c in _wired_cards()
    }


def unreached(query: str, vocab: Mapping[str, set[str]] | None = None) -> set[str]:
    """The content tokens in ``query`` that appear in no card at all."""
    cards = card_vocabulary() if vocab is None else vocab
    tokens = set(_query_tokens(query))
    return {t for t in tokens if not any(t in terms for terms in cards.values())}


def gap_report(
    queries: Iterable[str], vocab: Mapping[str, set[str]] | None = None
) -> list[TokenReach]:
    """Every token across ``queries``, ranked by demand then by scarcity.

    Sorted unreachable-first and then by how many queries wanted it, because that order
    is the authoring queue: the word the most people said that nothing answers is the
    first blurb to write.
    """
    cards = card_vocabulary() if vocab is None else vocab
    demand: Counter[str] = Counter()
    for q in queries:
        demand.update(set(_query_tokens(q)))
    out = [
        TokenReach(
            token=token,
            cards=sum(1 for terms in cards.values() if token in terms),
            queries=count,
        )
        for token, count in demand.items()
    ]
    out.sort(key=lambda r: (r.cards, -r.queries, r.token))
    return out


def render(report: Sequence[TokenReach], limit: int = 20) -> str:
    """A report line per token — reachable count first, because zero is the signal."""
    lines = [f"{'token':<18}{'cards':>6}{'queries':>9}"]
    for row in report[:limit]:
        lines.append(f"{row.token:<18}{row.cards:>6}{row.queries:>9}")
    zero = sum(1 for r in report if r.unreachable)
    lines.append("")
    lines.append(f"{zero}/{len(report)} tokens reach no card")
    return "\n".join(lines)
