"""The Orquestra CATALOG surface — the router as an MCP front door.

Where a program surface (:mod:`gecko.providers.orquestra`) serves ONE program,
this surface serves the exploration problem itself: an agent that knows nothing
about 40+ instructions across N programs asks ``find_start`` with a plain intent
and gets the exact starting point (see :mod:`gecko.find_start`). Alongside it:
``list_programs`` (the wired programs + one validated catalog page) and
``comprehend_program`` (the D-A auto-comprehend path for anything unwired).

Thin by design — parse arguments, call the package, format output. Duck-typed
``list_tools``/``call_tool`` like every other surface. Keyless, control plane
only: nothing here derives against a chain, signs, or broadcasts; everything
fetched from the catalog is UNTRUSTED input (validated + capped in
:mod:`gecko.orquestra_client`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..orquestra_client import OrquestraClient, OrquestraClientError

__all__ = ["OrquestraCatalogSurface"]

# How many catalog pages a single find_start call may pull for unwired
# candidates (each page = one upstream GET; the catalog is ~225 pages).
MAX_FIND_START_PAGES = 2


@dataclass
class OrquestraCatalogSurface:
    """Duck-typed MCP surface: ``list_programs`` + ``find_start`` + ``comprehend_program``.

    ``client`` is injectable for offline tests; ``None`` builds the default
    catalog client lazily (so serving stays possible with no network until a
    tool actually needs the catalog).
    """

    client: OrquestraClient | None = None
    find_start_pages: int = 1  # catalog pages consulted per find_start call

    surface_id = "orquestra:catalog"

    def _catalog_client(self) -> OrquestraClient:
        if self.client is None:
            self.client = OrquestraClient()
        return self.client

    # -- MCP surface --------------------------------------------------------

    def list_tools(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [_FIND_START_TOOL, _LIST_PROGRAMS_TOOL, _COMPREHEND_TOOL]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        args = arguments or {}
        if name == "find_start":
            return self._find_start(args)
        if name == "list_programs":
            return self._list_programs(args)
        if name == "comprehend_program":
            return self._comprehend_program(args)
        return {"error": f"unknown tool {name!r}"}

    # -- tools --------------------------------------------------------------

    def _find_start(self, args: dict[str, Any]) -> dict[str, Any]:
        from ..find_start import find_start

        intent = args.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            return {"error": "find_start needs an `intent` (plain words)"}
        program = args.get("program")
        program = program if isinstance(program, str) and program else None

        pages = []
        catalog_note = None
        page_cap = max(0, min(int(self.find_start_pages), MAX_FIND_START_PAGES))
        if page_cap:
            try:
                client = self._catalog_client()
                first = client.list_projects(page=1)
                pages.append(first)
                for page_no in range(2, min(page_cap, first.total_pages) + 1):
                    pages.append(client.list_projects(page=page_no))
            except OrquestraClientError as exc:
                # the wired index still answers; the catalog ride-along is honest-degraded
                catalog_note = (
                    f"catalog unavailable ({exc}); unwired candidates omitted"
                )

        result = find_start(intent, program=program, catalog_pages=pages)
        out = result.to_json()
        if catalog_note:
            out["catalog_note"] = catalog_note
        return out

    def _list_programs(self, args: dict[str, Any]) -> dict[str, Any]:
        from ..provider_config import load_packaged_provider
        from .cli import PROGRAMS

        _, apis = load_packaged_provider("orquestra")
        wired = []
        for api_id in sorted(PROGRAMS):
            program = apis[api_id].program
            if program is None:
                continue
            wired.append(
                {
                    "program": api_id,
                    "program_id": program.program_id,
                    "orquestra_project": program.orquestra_project,
                    "intents": list(program.intents),
                    "pdas": sorted(program.pdas),
                    "serve": f"gecko-orquestra --program {api_id} --stdio",
                }
            )
        out: dict[str, Any] = {"wired": wired}
        try:
            page_no = int(args.get("page", 1))
            page = self._catalog_client().list_projects(page=page_no)
            out["catalog"] = {
                "page": page.page,
                "total_pages": page.total_pages,
                "total": page.total,
                "projects": [
                    {"slug": p.id, "name": p.name, "program_id": p.program_id}
                    for p in page.projects
                ],
                "note": (
                    "catalog programs are NOT yet comprehended — pick one and call "
                    "comprehend_program first (the D-A path)"
                ),
            }
        except (OrquestraClientError, ValueError) as exc:
            out["catalog"] = {"error": str(exc)}
        return out

    def _comprehend_program(self, args: dict[str, Any]) -> dict[str, Any]:
        from ..orquestra_comprehend import ComprehendError, comprehend_project

        project = args.get("project")
        if not isinstance(project, str) or not project:
            return {"error": "comprehend_program needs a `project` (catalog slug)"}
        api_id = args.get("api_id")
        api_id = api_id if isinstance(api_id, str) and api_id else project
        try:
            surface = self._catalog_client().fetch_surface(project)
            result = comprehend_project(surface, api_id=api_id)
        except (OrquestraClientError, ComprehendError) as exc:
            return {"error": str(exc)}
        return {
            "config": result.config,
            "provenance": {
                name: {
                    "tier": prov.tier,
                    "flagged": prov.flagged,
                    "unresolved": list(prov.unresolved),
                }
                for name, prov in result.provenance.items()
            },
            "flagged": list(result.flagged),
            "note": (
                "generated from the catalog surface alone — no program source, no "
                "manual overlay. FLAGGED recipes are honest gaps (an unresolved "
                "seed or unknown program id), never fabricated; re-run the CLI "
                "with --source/--overlay to recover them."
            ),
        }


_FIND_START_TOOL = {
    "name": "find_start",
    "description": (
        "Say what you want to do in plain words ('buy this token on pump and hold "
        "it') and get the exact starting point across the wired Solana programs: "
        "(program, instruction), the dependency-ordered derive plan with a "
        "provenance tag on every account (extracted / recovered / flagged — the "
        "source-recovered roots and IDL-hidden accounts included), the DECLARED "
        "landing preludes, the honest flagged gaps, and the Orquestra /build "
        "execute pointer. Unwired catalog programs come back as comprehend-first "
        "pointers. Honest below the floor: 'no start found' + closest GUESSES, "
        "never a fabricated match. Returns plans and pointers only — never signs "
        "or broadcasts."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "program": {"type": "string", "description": "optional program hint"},
        },
        "required": ["intent"],
        "additionalProperties": False,
    },
}

_LIST_PROGRAMS_TOOL = {
    "name": "list_programs",
    "description": (
        "List the wired, agent-ready program surfaces (with their intents and "
        "derivable PDAs) plus one page of the Orquestra program catalog "
        "(paginated; catalog entries are not yet comprehended)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"page": {"type": "integer", "minimum": 1}},
        "additionalProperties": False,
    },
}

_COMPREHEND_TOOL = {
    "name": "comprehend_program",
    "description": (
        "Auto-comprehend an unwired catalog program (the D-A path): fetch its "
        "Orquestra surface and generate the Gecko program config, with per-PDA "
        "provenance (extracted/recovered/manual) and honest FLAGGED gaps for "
        "anything the surface alone cannot give. Surface-only here (no program "
        "source, no overlay)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "the catalog slug"},
            "api_id": {"type": "string"},
        },
        "required": ["project"],
        "additionalProperties": False,
    },
}
