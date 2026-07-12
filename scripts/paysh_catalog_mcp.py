#!/usr/bin/env python3
"""Build the pay.sh catalog MCP from the live catalog and report comprehension state.

Aggregates pay.sh's whole Solana-DeFi x402 catalog (70 providers) into ONE
first-call-correct MCP surface — WITHOUT re-listing or replacing pay.sh (aggregate, not a
marketplace; pay.sh keeps 100%, x402 settles direct). $0: challenge-only, never settles.

Usage:
  python scripts/paysh_catalog_mcp.py                     # build + report + sample search
  python scripts/paysh_catalog_mcp.py --search "…"        # custom sample intent
  python scripts/paysh_catalog_mcp.py --drift             # Tier-2 drift-watch (live 402 probe)
  python scripts/paysh_catalog_mcp.py --watch[:SECONDS]   # hourly self-refresh drift-watch loop
  python scripts/paysh_catalog_mcp.py --serve-http[:PORT] # serve the aggregated MCP over HTTP
  python scripts/paysh_catalog_mcp.py --serve-stdio       # serve the aggregated MCP over stdio

Everything but --serve-* / --drift / --watch is offline-safe metadata (control plane only).
"""

from __future__ import annotations

import sys

from gecko.catalog_mcp import CatalogMcpSurface
from gecko.paysh_catalog import CatalogRegistry, challenge_probe, fetch_catalog

_DEFAULT_INTENT = "find the onchain DEX pool for the jupiter token on solana"


def _build() -> tuple[CatalogRegistry, CatalogMcpSurface]:
    entries = fetch_catalog()
    registry = CatalogRegistry.build(entries)
    return registry, CatalogMcpSurface(registry)


def _report(registry: CatalogRegistry, surface: CatalogMcpSurface, intent: str) -> None:
    counts = registry.counts()
    tools = surface.list_tools()
    total = len(registry.providers())
    print("pay.sh catalog MCP — aggregated (pay.sh untouched, runs side by side)\n")
    print(f"  providers comprehended : {total}")
    print(f"  first-call-correct     : {counts['verified']} (live-verified 402)")
    print(f"  pending verification   : {counts['pending']} (flagged, not guessed)")
    print(f"  broken (drift)         : {counts['broken']}")
    print(
        f"  list_tools() count     : {len(tools)}  (= {total} providers + 1 search tool)\n"
    )

    print(f'  search_capabilities("{intent}"):')
    hits = surface.search_capabilities(intent)
    if not hits:
        print("    (no provider matched — try a different intent)")
    for h in hits[:5]:
        price = h["price_usd"]
        price_s = f"${price[0]}" if price[0] == price[1] else f"${price[0]}-${price[1]}"
        print(
            f"    - {h['name']:28s} [{h['comprehension']:>8s}] "
            f"{h['method']} {h['host']}{h['path']}  ({price_s})"
        )

    verified = [ps for ps in registry.providers() if ps.status == "verified"]
    if verified:
        print("\n  a first-call-correct request (challenge-only, $0):")
        ps = verified[0]
        from gecko.paysh_catalog import VERIFIED

        args = dict(VERIFIED[ps.entry.fqn].probe_args)
        req = ps.client.prepare(ps.tool_name, args)
        print(f"    {ps.tool_name}: {req.method} {req.url}")
        if req.json_body:
            print(f"      body {req.json_body}")


def _drift(registry: CatalogRegistry) -> None:
    print(
        "Tier-2 drift-watch — re-probing resolved endpoints challenge-only (expect 402):\n"
    )
    for r in registry.drift_watch(challenge_probe):
        flag = " (CHANGED)" if r.changed else ""
        print(f"  {r.fqn:28s} status={r.status:>8s} probe={r.probe_status}{flag}")


def _watch(registry: CatalogRegistry, seconds: int) -> None:
    """Run the SAME self-refresh drift-watch loop the hosted server runs, printing every
    drift transition to stdout (grep-friendly). Ctrl-C to stop. $0 / challenge-only."""
    import asyncio

    from gecko.paysh_watch import watch_loop

    print(
        f"pay.sh drift-watch — self-refresh every {seconds}s "
        "(Tier-1 sha-diff + Tier-2 challenge-only 402 re-probe, $0). Ctrl-C to stop.\n"
    )
    try:
        asyncio.run(
            watch_loop(
                registry,
                interval=seconds,
                fetch=fetch_catalog,
                probe=challenge_probe,
                sink=print,
            )
        )
    except KeyboardInterrupt:
        print("\nstopped.")


def _serve_http(surface: CatalogMcpSurface, port: int) -> None:
    from gecko.http_server import serve_http

    host = "127.0.0.1"
    print(
        f"Serving pay.sh catalog MCP over Streamable HTTP at http://{host}:{port}/mcp"
    )
    print("  one-click add:")
    print(f"    claude mcp add --transport http paysh http://{host}:{port}/mcp\n")
    serve_http(
        surface,
        host=host,
        port=port,
        mode="recorded",
        server_name="paysh",
        allowed_hosts=[f"{host}:{port}", f"localhost:{port}"],
    )


def _serve_stdio(surface: CatalogMcpSurface) -> None:
    from gecko.mcp_server import serve_stdio

    print("Serving pay.sh catalog MCP over stdio (server_name=paysh)…", file=sys.stderr)
    serve_stdio(surface, mode="recorded", server_name="paysh")


def main(argv: list[str]) -> None:
    intent = _DEFAULT_INTENT
    for arg in argv:
        if arg.startswith("--search="):
            intent = arg.split("=", 1)[1]
        elif arg == "--search" and argv.index(arg) + 1 < len(argv):
            intent = argv[argv.index(arg) + 1]

    registry, surface = _build()

    if any(a.startswith("--serve-http") for a in argv):
        raw = next(a for a in argv if a.startswith("--serve-http"))
        port = int(raw.split(":", 1)[1]) if ":" in raw else 8000
        _serve_http(surface, port)
        return
    if "--serve-stdio" in argv:
        _serve_stdio(surface)
        return
    watch = next((a for a in argv if a.startswith("--watch")), None)
    if watch is not None:
        from gecko.paysh_watch import refresh_seconds

        seconds = int(watch.split(":", 1)[1]) if ":" in watch else refresh_seconds()
        _watch(registry, seconds)
        return

    _report(registry, surface, intent)
    if "--drift" in argv:
        print()
        _drift(registry)


if __name__ == "__main__":
    main(sys.argv[1:])
