"""Score intent -> (store, product) against the frozen purchase-intent set.

The set landed in #476 with its overlap MEASURED rather than enforced, and then nothing
read it. A frozen golden set with no evaluator is a fixture, not a measurement — it can
only be quoted, never fail.

WHAT THIS MEASURES, and it is deliberately unflattering: today the product resolver is a
case-insensitive SUBSTRING filter (`store_directory.list_stores(product=...)`, whose own
docstring says "'water' finds 'Water' and 'Sparkling water'"). That is the baseline arm
here, because measuring an imagined resolver would tell us nothing about what a user
gets. A better arm is injected, never assumed — same shape, same set, same denominators,
so two arms are comparable by construction.

TWO POPULATIONS, NEVER SUMMED. Positive rows ask "did the right product surface, and
where"; out-of-scope rows ask "did we correctly return nothing". Averaging a recall over
both would let an honest refusal cancel a retrieval miss. :mod:`gecko.retrieval_metrics`
requires the population to be named for exactly this reason, and this is its first caller.

AND A THIRD NUMBER THAT IS NOT RETRIEVAL. `expect_plan` records what SHOULD happen —
`build`, `ask`, `swap_then_build`, `refuse`. A row can retrieve perfectly and still be
wrong about the plan: "an Espresso and a bottle of water" is expected to ASK, because
water matches four products in one store and one in another. Reporting plan accuracy
inside recall would score a system that silently picks as better than one that asks.

Offline. Reads the frozen menu snapshot, never the chain, so a ranking change can never
be confused with a merchant editing their storefront overnight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .catalog import _tokens
from .lexnorm import fold_tokens
from .retrieval_metrics import RetrievalScore, score

__all__ = ["IntentRow", "Report", "evaluate", "substring_arm", "load_rows", "load_menu"]

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tests" / "fixtures" / "golden" / "purchase_intents.jsonl"
SNAPSHOT = ROOT / "tests" / "fixtures" / "golden" / "purchase_menu_snapshot.json"

#: An arm takes a goal and the menu, and returns (store, product) pairs, best first.
Arm = Callable[[str, Mapping[str, Sequence[str]]], list[tuple[str, str]]]


@dataclass(frozen=True)
class IntentRow:
    goal: str
    archetype: str
    expect_products: tuple[str, ...]
    expect_stores: tuple[str, ...]
    expect_plan: str
    author: str


@dataclass(frozen=True)
class Report:
    positives: RetrievalScore
    #: Out-of-scope rows answered with nothing — a rate, on its own denominator.
    refusals: tuple[int, int]
    #: Rows whose archetype is `paraphrase_natural`, scored apart from keyword echo.
    paraphrase: RetrievalScore

    def render(self) -> str:
        ok, total = self.refusals
        return "\n".join(
            [
                f"positives    {self.positives.line(3)}",
                f"paraphrase   {self.paraphrase.line(3)}",
                f"refusals     {ok}/{total} out-of-scope rows returned nothing",
                "",
                "Populations are separate on purpose: a correct refusal is not a recall "
                "hit and must never average with one.",
            ]
        )


def load_rows() -> list[IntentRow]:
    out = []
    for line in TASKS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        out.append(
            IntentRow(
                goal=raw["goal"],
                archetype=raw["archetype"],
                expect_products=tuple(raw["expect_products"]),
                expect_stores=tuple(raw["expect_stores"]),
                expect_plan=raw["expect_plan"],
                author=raw["author"],
            )
        )
    return out


def load_menu() -> dict[str, list[str]]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def substring_arm(
    goal: str, menu: Mapping[str, Sequence[str]]
) -> list[tuple[str, str]]:
    """Today's behaviour, honestly: folded-token overlap against each product name.

    `list_stores(product=…)` filters on a caller-supplied substring; a user's whole
    sentence is not that substring, so the closest faithful stand-in is to score every
    product by how many of its folded tokens the goal carries. More overlap first, then
    the shorter name — a shorter name sharing the same terms is the more specific match.
    """
    goal_tokens = set(fold_tokens(set(_tokens(goal))))
    scored: list[tuple[int, int, str, str]] = []
    for store, products in menu.items():
        for product in products:
            shared = len(goal_tokens & set(fold_tokens(set(_tokens(product)))))
            if shared:
                scored.append((-shared, len(product), store, product))
    scored.sort()
    return [(store, product) for _, _, store, product in scored]


def evaluate(arm: Arm = substring_arm) -> Report:
    rows, menu = load_rows(), load_menu()
    positives: list[int | None] = []
    paraphrase: list[int | None] = []
    refused = considered = 0

    for row in rows:
        hits = arm(row.goal, menu)
        if row.archetype == "out_of_scope":
            considered += 1
            refused += int(not hits)
            continue
        wanted = set(row.expect_products)
        rank = next(
            (i for i, (_, product) in enumerate(hits, 1) if product in wanted), None
        )
        positives.append(rank)
        if row.archetype == "paraphrase_natural":
            paraphrase.append(rank)

    return Report(
        positives=score(positives, population="all_positive"),
        paraphrase=score(paraphrase, population="all_positive"),
        refusals=(refused, considered),
    )
