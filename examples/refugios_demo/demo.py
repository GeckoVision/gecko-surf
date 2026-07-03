"""Refugios Venezuela: a live, filter-rich shelter registry made agent-usable — and
the first surface on our hosted MCP gated by a PUBLISHABLE key (public by design).

Refugios Venezuela (open-source, dnsantosuosso/refugio-mapa-venezuela) is a collaborative
map of shelters and food centers after the 2026 earthquake. It's Supabase-backed and
every call carries a publishable ``apikey`` header — the kind meant to live in client
code. Gecko comprehends its surface into a first-call-correct tool an agent picks by
intent ("a shelter with water and medical care near me"), and injects the key at call
time so it never appears in the tool the agent sees.

    uv run python examples/refugios_demo/demo.py

Offline / $0 by default (recorded mode synthesizes from the schema; the apikey here is
a placeholder). Live works with the real publishable key + mode="live".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gecko.access import static_session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.evaluate import evaluate_tasks  # noqa: E402

SPEC = str(Path(__file__).resolve().parent / "spec" / "refugios_openapi.json")


# The publishable apikey is injected by the session, never exposed to the agent. In
# recorded mode it's a placeholder (no real call is made).
def _client() -> AgentApiClient:
    return AgentApiClient(
        SPEC, session=static_session({"apikey": "recorded-placeholder"})
    )


TASKS: list[dict[str, Any]] = [
    {
        "goal": "find a shelter with drinking water and medical care",
        "expect_op": "listRefugios",
        "args": {"has_water": True, "has_medical": True},
    },
    {
        "goal": "open shelters in Caracas",
        "expect_op": "listRefugios",
        "args": {"city": "Caracas", "status": "activo"},
    },
    {
        "goal": "shelters that accept pets",
        "expect_op": "listRefugios",
        "args": {"pets_allowed": True},
    },
    {
        "goal": "look up one shelter by its id",
        "expect_op": "listRefugios",
        "args": {"id": "some-uuid"},
    },
]


@dataclass(frozen=True)
class DemoReport:
    ops_total: int
    surfaced: int
    apikey_hidden: bool
    card: dict[str, Any]


def build_report() -> DemoReport:
    client = _client()
    tools = client.list_tools()
    props = (tools[0].get("inputSchema", {}).get("properties") or {}) if tools else {}
    return DemoReport(
        ops_total=len(client.operations),
        surfaced=len(tools),
        apikey_hidden="apikey" not in props,
        card=evaluate_tasks(client, TASKS),
    )


def main() -> None:
    r = build_report()
    print("Refugios Venezuela — comprehended by Gecko (offline, $0)")
    print("=" * 56)
    print(
        f"{r.ops_total} operation -> {r.surfaced} agent tool (publishable apikey gated)"
    )
    print(f"apikey hidden from the agent-facing tool: {r.apikey_hidden}")
    print(f"first-call-correct: well-formed {r.card['well_formed_rate']:.0%}")
    print()
    print("Shelters with has_water / has_medical / has_electricity / pets_allowed +")
    print("coordinates — 'nearest shelter with medical care' becomes answerable, and")
    print("the publishable key stays invisible to the agent (auth is our seam).")


if __name__ == "__main__":
    main()
