"""ReportaVNZLA: a real, live humanitarian API made agent-usable — the SOS-bot's
richest data source, and the first surface centralized on our hosted MCP.

ReportaVNZLA (open-source, MIT, bitupx00/reportavnzla) is the Venezuela-2026 earthquake
missing-persons + relief-centers registry: ~61k records, public reads, no token. Gecko
comprehends its OpenAPI into first-call-correct tools an agent picks by intent — search
a person, filter by status (buscado/encontrado), find donation centers — with the
last-known `lat`/`lng` that makes a "nearest safe place" answer possible.

    uv run python examples/reportavnzla_demo/demo.py

Offline / $0 by default (recorded mode synthesizes from the schema). The API is public,
so a live run works too — flip mode="live".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gecko.access import public_session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.evaluate import evaluate_tasks  # noqa: E402

SPEC = str(Path(__file__).resolve().parent / "spec" / "reportavnzla_openapi.json")

# Natural-language goals a family / volunteer / the bot would actually ask.
TASKS: list[dict[str, Any]] = [
    {
        "goal": "search for a missing person by name",
        "expect_op": "searchPersonas",
        "args": {"q": "Maria"},
    },
    {
        "goal": "look up a person by their national id",
        "expect_op": "searchPersonas",
        "args": {"cedula": "V-1234567"},
    },
    {
        "goal": "show only people who have been found",
        "expect_op": "searchPersonas",
        "args": {"estado": "encontrado"},
    },
    {
        "goal": "how many people are still missing in total",
        "expect_op": "getStats",
        "args": {},
    },
    {
        "goal": "find donation collection centers to bring supplies",
        "expect_op": "listRecursos",
        "args": {"tipo": "centro_acopio"},
    },
    {
        "goal": "what are the newest reports right now",
        "expect_op": "getRecentFeed",
        "args": {"limit": 10},
    },
]


@dataclass(frozen=True)
class DemoReport:
    ops_total: int
    surfaced: int
    card: dict[str, Any]
    person_has_coords_field: bool


def build_report() -> DemoReport:
    client = AgentApiClient(SPEC, session=public_session())
    card = evaluate_tasks(client, TASKS)
    # The closest-safe-place feature hinges on coordinates existing on the surface.
    person = client.spec["components"]["schemas"]["Person"]["properties"]
    return DemoReport(
        ops_total=len(client.operations),
        surfaced=len(client.list_tools()),
        card=card,
        person_has_coords_field="lat" in person and "lng" in person,
    )


def main() -> None:
    r = build_report()
    print("ReportaVNZLA — comprehended by Gecko (offline, $0)")
    print("=" * 56)
    print(f"{r.ops_total} operations -> {r.surfaced} agent tools (public, no token)")
    print(
        f"first-call-correct: top-1 {r.card['top1_rate']:.0%} · well-formed {r.card['well_formed_rate']:.0%}"
    )
    print(
        f"person records carry lat/lng -> nearest-safe-place is possible: {r.person_has_coords_field}"
    )
    print()
    print("A real, crowdsourced, no-docs-for-agents registry — first-call-correct,")
    print("and centralized on one MCP host alongside the other relief APIs.")


if __name__ == "__main__":
    main()
