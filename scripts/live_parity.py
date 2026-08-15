"""Is the hosted surface running this code? — local vs live, as an equality not a guess.

The failure this exists to prevent: a change merges, the deploy does not happen, and the
next live test reports the OLD behaviour. Twice in one session that was diagnosed by
calling a tool and reading the shape of its error, and the first diagnosis was wrong.

`tools_rev` is a content hash of the served tool set, so building the same surface locally
and comparing is exact. When the revs differ this prints WHAT differs — tools added,
removed, or changed shape — which is usually enough to name the missing commit.

    uv run python scripts/live_parity.py
    uv run python scripts/live_parity.py --url https://mcp.geckovision.tech/orquestra

Exit 0 when live matches local, 1 when it does not, 2 when live could not be reached.
Read-only: it fetches `/version` and lists tools over MCP. It never calls a tool, so it
cannot spend, sign, or write anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.providers.catalog_surface import OrquestraCatalogSurface  # noqa: E402
from gecko.surfaces import tools_rev  # noqa: E402

DEFAULT_BASE = "https://mcp.geckovision.tech/orquestra"
_TIMEOUT = 45


def _post_mcp(
    base: str, payload: dict, session: str | None
) -> tuple[dict | None, str | None]:
    """One JSON-RPC call over MCP Streamable HTTP. Returns (result, session-id)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["mcp-session-id"] = session
    request = urllib.request.Request(
        f"{base.rstrip('/')}/mcp", data=json.dumps(payload).encode(), headers=headers
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        session = response.headers.get("mcp-session-id") or session
        for line in response.read().decode().splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), session
    return None, session


def live_tools(base: str) -> list[dict]:
    """The tool list the hosted surface actually serves."""
    _, session = _post_mcp(
        base,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "live-parity", "version": "1"},
            },
        },
        None,
    )
    _post_mcp(base, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    body, _ = _post_mcp(
        base, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session
    )
    return list((body or {}).get("result", {}).get("tools", []))


def live_version(base: str) -> dict | None:
    """`/version` if the deploy has it. Absent means the live build PREDATES this script,
    which is itself the answer — report it rather than failing."""
    try:
        with urllib.request.urlopen(
            f"{base.rstrip('/')}/version", timeout=_TIMEOUT
        ) as r:
            return dict(json.loads(r.read().decode()))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None


def _shape(tool: dict) -> dict:
    """The parts of a tool an agent's behaviour actually depends on."""
    schema = tool.get("inputSchema") or {}
    return {
        "required": sorted(schema.get("required") or []),
        "properties": sorted((schema.get("properties") or {})),
        "description_len": len(tool.get("description") or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_BASE)
    args = parser.parse_args()

    local_tools = list(OrquestraCatalogSurface().list_tools())
    local_rev = tools_rev(local_tools)
    print(f"local  {local_rev}  {len(local_tools)} tools")

    try:
        served = live_tools(args.url)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        print(f"live   UNREACHABLE at {args.url}: {exc}")
        return 2

    version = live_version(args.url)
    remote_rev = (version or {}).get("tools_rev")
    if remote_rev is None:
        print(
            f"live   /version ABSENT  {len(served)} tools  "
            "(the deployed build predates the endpoint — that alone means it is stale)"
        )
    else:
        print(f"live   {remote_rev}  {len(served)} tools")
        if remote_rev == local_rev:
            print("\nMATCH — the hosted surface is serving this code.")
            return 0

    local_by_name = {t["name"]: t for t in local_tools}
    live_by_name = {t["name"]: t for t in served}
    print("\nDRIFT:")
    for name in sorted(set(local_by_name) - set(live_by_name)):
        print(f"  + {name}  (built locally, NOT served live)")
    for name in sorted(set(live_by_name) - set(local_by_name)):
        print(f"  - {name}  (served live, gone locally)")
    for name in sorted(set(local_by_name) & set(live_by_name)):
        here, there = _shape(local_by_name[name]), _shape(live_by_name[name])
        for field in ("required", "properties", "description_len"):
            if here[field] != there[field]:
                print(f"  ~ {name}.{field}: live={there[field]} local={here[field]}")
    print("\nSTALE — redeploy the hosted surface.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
