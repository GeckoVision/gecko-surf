"""`start(intent)` — the router's answer projected to the one call an agent makes.

Measured before this existed: ``find_start("buy a coffee")`` returns **17,717 characters**,
of which the chosen start is 4,382. The remaining 13,335 are four runners-up, each
carrying a full derive plan — and three of the four are ``guess``, which is the router's
own word for "below the floor, do not run this". An agent pays for four plans to make one
call, and a live field report already made the matching complaint: it had to assemble the
tools itself, and comprehension did not persist between calls.

**The rule, not a truncation.** A derive plan is a plan to CALL something, and you do not
plan a call you are not making. The chosen start keeps everything it had — plan, chain,
preludes, gaps, the honesty notes. A runner-up becomes what a runner-up actually is: a
name, a score, and why it lost, which is exactly enough for a caller who disagrees with
the ranking to ask for it by name.

**Additive by construction.** This projects a :class:`~gecko.find_start.FindStartResult`
and touches neither ``find_start`` nor ``StartPoint``. Anything reading the full shape is
unaffected, and the two can be compared against each other — which is what
``tests/test_start_view.py`` does.

**Nothing here is synthesized.** Every field is lifted from the result. A projection that
invents a field becomes a second source of truth about what the router decided, and the
first thing to drift is always the summary.
"""

from __future__ import annotations

from typing import Any

from .find_start import FindStartResult

__all__ = ["ALTERNATIVE_FIELDS", "project_start", "start_view"]

#: What a runner-up keeps. Deliberately a fixed, reviewed list rather than "everything
#: except the heavy keys": a blocklist silently admits whatever gets added to StartPoint
#: next, which is how a summary grows back into the payload it replaced.
#:
#: `why` and `score` are here because the ranking has to remain arguable — an agent that
#: thinks the router chose wrong needs to see what each candidate matched on. `kind`
#: is here because `guess` is a verdict, and hiding it would present a below-floor
#: candidate as a peer of the chosen start.
ALTERNATIVE_FIELDS = (
    "program",
    "program_id",
    "instruction",
    "kind",
    "score",
    "why",
)


def project_start(rendered: dict[str, Any]) -> dict[str, Any]:
    """Project an already-rendered ``FindStartResult`` dict.

    Everything except ``starts`` is passed through UNCHANGED. That is deliberate: the
    caller adds honesty fields to this dict (``catalog_note`` when the catalog ride-along
    failed, for one), and a projection that listed the keys it keeps would drop the next
    one silently. We transform the thing we are transforming and touch nothing else.
    """
    points: list[dict[str, Any]] = list(rendered.get("starts") or ())

    chosen: dict[str, Any] | None = None
    for point in points:
        if point.get("kind") == "start":
            chosen = point
            break

    view = {key: value for key, value in rendered.items() if key != "starts"}
    view["start"] = chosen
    view["alternatives"] = [
        {key: point[key] for key in ALTERNATIVE_FIELDS if key in point}
        for point in points
        if point is not chosen
    ]
    return view


def start_view(result: FindStartResult) -> dict[str, Any]:
    """Project ``result`` into the shape an agent acts on.

    ``start`` is the first point the router calls runnable, and is ``None`` exactly when
    the router refused — the same condition as ``no_start``, so a caller cannot read a
    refusal as a plan. ``alternatives`` are every other candidate, summarized.
    """
    return project_start(result.to_json())
