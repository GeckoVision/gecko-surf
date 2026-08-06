"""``gecko-orquestra`` — serve an Orquestra program's front-door surface over MCP.

The entry is the **provider** (Orquestra); the **program** is a parameter — one command
for all of Orquestra's programs, parameterized, never a new entry per program (the
per-provider model: docs/specs/2026-07-31-orquestra-provider-integration.md).

    gecko-orquestra --program meteora --stdio      # add straight into Claude Code
    gecko-orquestra --program meteora              # or serve over HTTP

    gecko-orquestra comprehend --list              # browse the Orquestra catalog
    gecko-orquestra comprehend --project <slug> \\
        [--source <path>] [--overlay <path>]       # generate a program config

    gecko-orquestra find-start "buy token X on pump"   # intent → the right start
    gecko-orquestra eval-retrieval                 # golden-set retrieval report
    gecko-orquestra --catalog --stdio              # serve the catalog router surface

Adding a program later (pump, jupiter, …) is a new entry in ``_PROGRAMS`` + its recipes —
not a new CLI. Keyless, control plane only: it derives the accounts and points at
Orquestra's builder; it never proxies or signs. ``comprehend`` is the auto path that
replaces hand-authoring those configs (:mod:`gecko.orquestra_comprehend`): the
generated JSON goes to stdout (no file writes), the provenance report to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..provider_config import load_packaged_provider, load_packaged_provider_base_url
from .orquestra import Intent, OrquestraProgramSurface

if TYPE_CHECKING:
    from ..find_start import StartSpec

__all__ = [
    "main",
    "comprehend_main",
    "eval_retrieval_main",
    "find_start_main",
    "PROGRAMS",
    "build_surface_from_config",
    "intent_registries",
    "start_specs",
]


def build_surface_from_config(
    provider: str, api_id: str, intents: dict[str, Intent]
) -> OrquestraProgramSurface:
    """Build a program surface from packaged config (identity + PDA recipes) + a
    supplied intent registry (the plan callables). This is the config-driven
    backbone: PDAs are DATA, only the multi-step plan is code."""
    _, apis = load_packaged_provider(provider)
    api = apis[api_id]
    if api.program is None:
        raise ValueError(f"api {api_id!r} of provider {provider!r} is not a program")
    if api.program.orquestra_project is None:
        raise ValueError(
            f"api {api_id!r} has no orquestra_project — comprehended for derivation only, "
            "not yet servable (no execute/build URL wired)"
        )
    base = load_packaged_provider_base_url(provider).rstrip("/")
    project_base_url = f"{base}/{api.program.orquestra_project}"
    wanted = {k: v for k, v in intents.items() if k in set(api.program.intents)}
    return OrquestraProgramSurface(
        program_id=api.program.program_id,
        project_base_url=project_base_url,
        pdas=dict(api.program.pdas),
        intents=wanted,
    )


def intent_registries() -> dict[str, dict[str, Intent]]:
    """api_id → the intent registry supplying that program's plan callables."""
    from .jupiter import JUPITER_INTENTS
    from .metadao import METADAO_INTENTS
    from .meteora import METEORA_INTENTS
    from .ore import ORE_INTENTS
    from .pumpfun import PUMPFUN_INTENTS

    return {
        "meteora": METEORA_INTENTS,
        "pumpfun": PUMPFUN_INTENTS,
        "ore": ORE_INTENTS,
        "metadao_ico": METADAO_INTENTS,
        "jupiter": JUPITER_INTENTS,
    }


def start_specs() -> dict[str, dict[str, "StartSpec"]]:
    """api_id → intent name → its declarative StartSpec (what find_start routes to)."""
    from .jupiter import JUPITER_STARTS
    from .metadao import METADAO_STARTS
    from .meteora import METEORA_STARTS
    from .ore import ORE_STARTS
    from .pumpfun import PUMPFUN_STARTS

    return {
        "meteora": METEORA_STARTS,
        "pumpfun": PUMPFUN_STARTS,
        "ore": ORE_STARTS,
        "metadao_ico": METADAO_STARTS,
        "jupiter": JUPITER_STARTS,
    }


def _discover_programs() -> dict[str, Callable[[], OrquestraProgramSurface]]:
    """Discover servable programs from packaged config. Each config-listed program
    is paired with its code-side intent registry (the plan callables)."""
    intents_by_key: dict[tuple[str, str], dict[str, Intent]] = {
        ("orquestra", api_id): intents
        for api_id, intents in intent_registries().items()
    }
    programs: dict[str, Callable[[], OrquestraProgramSurface]] = {}
    for (provider, api_id), intents in intents_by_key.items():

        def _make(
            provider: str = provider,
            api_id: str = api_id,
            intents: dict[str, Intent] = intents,
        ) -> OrquestraProgramSurface:
            return build_surface_from_config(provider, api_id, intents)

        programs[api_id] = _make
    return programs


# The provider's program registry — program name → surface builder, discovered from
# packaged config. Add a program by writing its config + registering its intents in
# _discover_programs; no new CLI entry.
PROGRAMS: dict[str, Callable[[], OrquestraProgramSurface]] = _discover_programs()


def serve(surface: OrquestraProgramSurface, args: argparse.Namespace, name: str) -> int:
    """Serve a built provider surface over stdio or Streamable-HTTP."""
    print(
        f"gecko-orquestra[{name}]: {len(surface.pdas)} PDA(s), intents={list(surface.intents)} "
        f"→ executes via {surface.project_base_url}",
        file=sys.stderr,  # stdout is the stdio JSON-RPC channel
    )
    return _serve_transport(surface, args, name)


def _serve_transport(surface: Any, args: argparse.Namespace, name: str) -> int:
    """The shared transport tail (stdio / Streamable-HTTP) for any duck-typed
    list_tools/call_tool surface."""
    if args.stdio:
        from ..mcp_server import serve_stdio

        serve_stdio(surface, server_name=name)
    else:
        from ..http_server import serve_http

        serve_http(
            surface,
            host=args.host,
            port=args.port,
            mode="recorded",
            server_name=name,
            public_url=args.public_url,
        )
    return 0


def _add_serve_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stdio", action="store_true", help="serve over stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--public-url", default=None, help="external URL behind a tunnel/ALB"
    )


# Bound on --catalog-pages: each page is one upstream GET; the catalog is ~225
# pages and a router query never needs more than a few.
_MAX_CATALOG_PAGES = 5


def find_start_main(argv: list[str]) -> int:
    """``gecko-orquestra find-start "<intent>"`` — intent → ranked start points.

    Offline by default (the wired programs' packaged configs). ``--catalog-pages N``
    additionally consults the live Orquestra catalog (capped) for unwired
    comprehend-first candidates. Exit 0 = a start was found; 1 = honest no-start.
    """
    parser = argparse.ArgumentParser(
        prog="gecko-orquestra find-start",
        description=(
            "Route a plain intent to the right starting point: (program, "
            "instruction) + the provenance-tagged, dependency-ordered derive plan "
            "+ DECLARED preludes + honest flagged gaps + the Orquestra execute "
            "pointer. Never fabricates: below the retrieval floor it says so."
        ),
    )
    parser.add_argument("intent", help="what you want to do, in plain words")
    parser.add_argument(
        "--program", default=None, help="optional hint: a wired program or catalog name"
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--catalog-pages",
        type=int,
        default=0,
        help=(
            "fetch up to N pages of the live Orquestra catalog for unwired "
            f"comprehend-first candidates (0 = offline, default; capped at "
            f"{_MAX_CATALOG_PAGES})"
        ),
    )
    parser.add_argument(
        "--base-url", default=None, help="override the Orquestra API base URL"
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    parser.add_argument(
        "--log-misses",
        default=None,
        metavar="PATH",
        help=(
            "opt-in retrieval-miss instrumentation: append one CATEGORICAL JSON "
            "line (counts only — never the intent text) to PATH on a miss"
        ),
    )
    args = parser.parse_args(argv)

    from ..find_start import find_start, format_result, jsonl_miss_logger

    pages = []
    if args.catalog_pages > 0:
        from ..orquestra_client import OrquestraClient, OrquestraClientError

        try:
            client = OrquestraClient(base_url=args.base_url)
            first = client.list_projects(page=1)
            pages.append(first)
            last = min(args.catalog_pages, _MAX_CATALOG_PAGES, first.total_pages)
            for page_no in range(2, last + 1):
                pages.append(client.list_projects(page=page_no))
        except OrquestraClientError as exc:
            print(f"catalog unavailable ({exc}); continuing offline", file=sys.stderr)

    on_miss = jsonl_miss_logger(args.log_misses) if args.log_misses else None
    result = find_start(
        args.intent,
        program=args.program,
        catalog_pages=pages,
        limit=args.limit,
        on_miss=on_miss,
    )
    if args.json:
        json.dump(result.to_json(), sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        sys.stdout.write(format_result(result))
    return 1 if result.no_start else 0


def eval_retrieval_main(argv: list[str]) -> int:
    """``gecko-orquestra eval-retrieval`` — replay the committed golden set through
    the unmodified router and print the retrieval report (recall@1/@3, MRR, the
    closed miss-cause histogram, the semantic-flip evidence line). Measures the
    lexical-vs-semantic case; flips nothing. Thin — the logic is
    :mod:`gecko.retrieval_eval`."""
    parser = argparse.ArgumentParser(
        prog="gecko-orquestra eval-retrieval",
        description=(
            "Evaluate find_start retrieval against a golden set (intent -> gold "
            "start). Default: the committed fixture packaged with Gecko."
        ),
    )
    parser.add_argument(
        "--golden",
        default=None,
        metavar="PATH",
        help="override the committed golden JSONL fixture",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=3,
        help="classification threshold: gold within top-k = hit (default 3)",
    )
    args = parser.parse_args(argv)

    from ..retrieval_eval import GoldenSetError, evaluate_golden, format_report

    try:
        report = evaluate_golden(args.golden, k=args.k)
    except (GoldenSetError, OSError) as exc:
        print(f"eval-retrieval: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(format_report(report))
    return 0


def comprehend_main(argv: list[str], *, client: Any = None) -> int:
    """``gecko-orquestra comprehend`` — auto-comprehend-on-pick.

    ``--list`` browses the catalog; ``--project <slug>`` generates the program's
    api-config JSON (stdout only — no file writes) with a per-PDA provenance
    report on stderr. ``--source`` is the SOURCE-TRUST BOUNDARY: a local,
    founder-curated file path only — this command never fetches program source
    from GitHub or anywhere else. ``client`` is injectable for offline tests.
    """
    parser = argparse.ArgumentParser(
        prog="gecko-orquestra comprehend",
        description=(
            "Generate a program config from an Orquestra project surface, merged "
            "with caller-supplied source-recovered seeds + an explicit manual "
            "overlay. Prints JSON to stdout; writes nothing."
        ),
    )
    parser.add_argument("--list", action="store_true", help="list the catalog")
    parser.add_argument("--page", type=int, default=1, help="catalog page (--list)")
    parser.add_argument("--project", help="the Orquestra project slug to comprehend")
    parser.add_argument(
        "--api-id", default=None, help="api_id for the generated config (default: slug)"
    )
    parser.add_argument(
        "--source",
        default=None,
        help=(
            "LOCAL path to curated program source (trust boundary: never fetched "
            "remotely) — recovers the seeds the IDL drops"
        ),
    )
    parser.add_argument(
        "--overlay",
        default=None,
        help="LOCAL path to a manual_overlay JSON (intents/notes/include/pdas/why)",
    )
    parser.add_argument(
        "--base-url", default=None, help="override the Orquestra API base URL"
    )
    args = parser.parse_args(argv)

    from ..orquestra_client import OrquestraClient, OrquestraClientError
    from ..orquestra_comprehend import ComprehendError, comprehend_project

    try:
        if client is None:
            client = OrquestraClient(base_url=args.base_url)

        if args.list:
            page = client.list_projects(page=args.page)
            for project in page.projects:
                print(f"{project.id}  {project.program_id}  {project.name}")
            print(
                f"page {page.page}/{page.total_pages} ({page.total} projects)",
                file=sys.stderr,
            )
            return 0

        if not args.project:
            parser.error("either --project <slug> or --list is required")

        surface = client.fetch_surface(args.project)
        source = Path(args.source).read_text(encoding="utf-8") if args.source else None
        overlay = (
            json.loads(Path(args.overlay).read_text(encoding="utf-8"))
            if args.overlay
            else None
        )
        result = comprehend_project(
            surface,
            api_id=args.api_id or args.project,
            source=source,
            overlay=overlay,
        )
    except (OrquestraClientError, ComprehendError, OSError, ValueError) as exc:
        print(f"gecko-orquestra comprehend: {exc}", file=sys.stderr)
        return 2

    json.dump(result.config, sys.stdout, indent=2, ensure_ascii=False)
    print()
    for name, prov in result.provenance.items():
        flag = (
            f"  FLAGGED (unresolved: {', '.join(prov.unresolved) or 'program id'})"
            if prov.flagged
            else ""
        )
        print(f"  {name}: {prov.tier}{flag}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry (``gecko-orquestra``): serve ``--program <name>`` over MCP (needs
    [serve]), or ``comprehend`` to generate a program config (keyless, no deps)."""
    args_list = list(argv) if argv is not None else sys.argv[1:]
    if args_list and args_list[0] == "comprehend":
        return comprehend_main(args_list[1:])
    if args_list and args_list[0] == "find-start":
        return find_start_main(args_list[1:])
    if args_list and args_list[0] == "eval-retrieval":
        return eval_retrieval_main(args_list[1:])
    parser = argparse.ArgumentParser(
        prog="gecko-orquestra",
        description="Serve an Orquestra program's front-door surface over MCP (keyless).",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--program",
        choices=sorted(PROGRAMS),
        help="which Orquestra program to serve",
    )
    target.add_argument(
        "--catalog",
        action="store_true",
        help=(
            "serve the catalog router surface instead (list_programs + find_start "
            "+ comprehend_program)"
        ),
    )
    _add_serve_args(parser)
    args = parser.parse_args(args_list)

    if args.catalog:
        from .catalog_surface import OrquestraCatalogSurface

        catalog_surface = OrquestraCatalogSurface()
        print(
            f"gecko-orquestra[catalog]: {len(PROGRAMS)} wired program(s); "
            "find_start routes intents, comprehend_program generates configs",
            file=sys.stderr,  # stdout is the stdio JSON-RPC channel
        )
        return _serve_transport(catalog_surface, args, name="orquestra-catalog")

    surface = PROGRAMS[args.program]()
    return serve(surface, args, name=args.program)
