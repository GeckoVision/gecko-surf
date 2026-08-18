"""Measure a partner's live MCP against our graph, over their own catalogue.

    uv run python -m scripts.partner_delta --limit 30

Thin by design: fetches the catalogue and each IDL, hands both to
:mod:`gecko.partner_delta`, prints the table. Read-only — the partner tools called are
listings, and no response is written to disk.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from gecko.mcp_client import McpClient
from gecko.netguard import safe_get
from gecko.partner_delta import ProgramDelta, SeedCoverage, compare_program

DEFAULT_MCP = "https://api.orquestra.dev/mcp"
DEFAULT_API = "https://api.orquestra.dev"
USER_AGENT = {"User-Agent": "gecko-surf/partner-delta"}


def catalogue(api: str, limit: int) -> list[dict]:
    body = safe_get(f"{api}/api/projects?page=1", max_bytes=4_000_000)
    projects = json.loads(body).get("projects") or []
    return [p for p in projects if isinstance(p, dict)][:limit]


def idl_of(api: str, project_id: str) -> dict | None:
    try:
        request = urllib.request.Request(
            f"{api}/api/idl/{project_id}", headers=USER_AGENT
        )
        with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
            payload = json.loads(response.read())
    except Exception:
        return None
    value = payload.get("idl") or payload
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    return value if isinstance(value, dict) and "instructions" in value else None


def total(deltas: list[ProgramDelta], arm: str) -> SeedCoverage:
    out = SeedCoverage()
    for delta in deltas:
        coverage = getattr(delta, arm)
        out.variable_seeds += coverage.variable_seeds
        out.typed_seeds += coverage.typed_seeds
        out.accounts += coverage.accounts
        out.fully_typed_accounts += coverage.fully_typed_accounts
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    client = McpClient(args.mcp_url)
    deltas: list[ProgramDelta] = []

    for project in catalogue(args.api, args.limit):
        project_id = str(project.get("id") or "")
        idl = idl_of(args.api, project_id) if project_id else None
        if not idl:
            continue
        delta = compare_program(
            client,
            name=str(project.get("name") or project_id),
            project_id=project_id,
            idl=idl,
            program_id=project.get("program_id"),
        )
        if delta.partner.accounts == 0 and delta.gecko.accounts == 0:
            continue  # no PDA declarations either side — nothing to compare
        deltas.append(delta)
        print(
            f"  {delta.name[:34]:36} "
            f"partner {delta.partner.typed_seeds:3}/{delta.partner.variable_seeds:<3} "
            f"gecko {delta.gecko.typed_seeds:3}/{delta.gecko.variable_seeds:<3}"
            + (
                f"  +{delta.gecko_only_accounts} unlisted"
                if delta.gecko_only_accounts
                else ""
            )
            + (f"   [{delta.error[:40]}]" if delta.error else ""),
            file=sys.stderr,
        )

    if not deltas:
        print("no comparable programs", file=sys.stderr)
        return 1

    partner, gecko = total(deltas, "partner"), total(deltas, "gecko")
    unlisted = sum(d.gecko_only_accounts for d in deltas)
    print(f"\n{len(deltas)} programs compared, both arms reading the same IDL")
    print("counted ONLY on the (instruction, account) pairs the partner lists,")
    print("so the two arms share a denominator.\n")
    print(f"{'':8} {'typed seeds':>18} {'derivable accounts':>22}")
    for label, arm in (("partner", partner), ("gecko", gecko)):
        print(
            f"{label:8} "
            f"{arm.typed_seeds:6}/{arm.variable_seeds:<5} {arm.typed_rate:6.1%}  "
            f"{arm.fully_typed_accounts:6}/{arm.accounts:<5} {arm.derivable_rate:6.1%}"
        )
    print(
        f"\nplus {unlisted} PDA slots Gecko resolves that the partner's listing does not\n"
        "mention at all (a recipe carried across instructions). Counted, deliberately not\n"
        "folded into the rates above — the other arm was never asked about them."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
