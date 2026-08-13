"""The Orquestra CATALOG surface — the router as an MCP front door.

Where a program surface (:mod:`gecko.providers.orquestra`) serves ONE program,
this surface serves the exploration problem itself: an agent that knows nothing
about 40+ instructions across N programs asks ``find_start`` with a plain intent
and gets the exact starting point (see :mod:`gecko.find_start`). Alongside it:
``list_programs`` (the wired programs + one validated catalog page) and
``comprehend_program`` (the D-A auto-comprehend path for anything unwired), and
``prepare_purchase`` (:mod:`gecko.prepare_purchase` — plan, verify, hand back the
UNSIGNED bytes; the caller signs).

Thin by design — parse arguments, call the package, format output. Duck-typed
``list_tools``/``call_tool`` like every other surface. Keyless, control plane
only: nothing here derives against a chain, signs, or broadcasts; everything
fetched from the catalog is UNTRUSTED input (validated + capped in
:mod:`gecko.orquestra_client`).

THIS SURFACE IS SERVED PUBLICLY AND UNAUTHENTICATED. Anything mounted here is
reachable by anyone on the internet, which is why no tool on it may hold a key,
produce a signature, or send a transaction — and why the one tool that touches
transaction bytes hands them back unsigned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..orquestra_client import OrquestraClient, OrquestraClientError
from ..prepare_purchase import prepare_purchase_result, prepare_purchase_tool
from ..store_directory import LIST_STORES_TOOL, list_stores_result
from ..rpc import RpcCall
from ..simulate import BuildCall
from ..wallet_binding import WalletDirectory

__all__ = ["OrquestraCatalogSurface"]

# How many catalog pages a single find_start call may pull for unwired
# candidates (each page = one upstream GET; the catalog is ~225 pages).
MAX_FIND_START_PAGES = 2


@dataclass
class OrquestraCatalogSurface:
    """Duck-typed MCP surface: ``list_programs`` + ``find_start`` +
    ``comprehend_program`` + ``prepare_purchase`` + ``list_stores``.

    ``client`` is injectable for offline tests; ``None`` builds the default
    catalog client lazily (so serving stays possible with no network until a
    tool actually needs the catalog). ``purchase_build_call`` and
    ``purchase_rpc_call`` are the same idea for the purchase pre-flight's two
    transports, so that path is falsifiable with no network at all.

    ``wallets`` is the ``account_id -> wallet`` directory. ``None`` (the default,
    and what the public unauthenticated mount serves) means the caller supplies
    the buyer, which is correct for unsigned bytes. Wired, the buyer of an
    account that has bound a wallet is LOOKED UP rather than accepted — see
    :mod:`gecko.prepare_purchase`.
    """

    client: OrquestraClient | None = None
    find_start_pages: int = 1  # catalog pages consulted per find_start call
    purchase_build_call: BuildCall | None = None
    purchase_rpc_call: RpcCall | None = None
    wallets: WalletDirectory | None = None

    surface_id = "orquestra:catalog"

    def _catalog_client(self) -> OrquestraClient:
        if self.client is None:
            self.client = OrquestraClient()
        return self.client

    # -- MCP surface --------------------------------------------------------

    def list_tools(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            _FIND_START_TOOL,
            _LIST_PROGRAMS_TOOL,
            _COMPREHEND_TOOL,
            # The schema states the buyer rule THIS mount enforces, so an agent does not
            # have to be refused to learn it.
            prepare_purchase_tool(buyer_bound=self.wallets is not None),
            LIST_STORES_TOOL,
        ]

    def call_tool(
        self, name: str, arguments: dict[str, Any], *, account: str | None = None
    ) -> Any:
        """``account`` is the AUTHENTICATED caller's id, supplied by the transport.

        Keyword-only and defaulted to ``None`` because it can only ever come from the
        gate that verified the caller — never from ``arguments``, which are the caller's
        own word. ``None`` is the honest state of the public mount today: it is
        unauthenticated, so it has no account to pass and every call is mode A.
        """
        args = arguments or {}
        if name == "find_start":
            return self._find_start(args)
        if name == "list_programs":
            return self._list_programs(args)
        if name == "comprehend_program":
            return self._comprehend_program(args)
        if name == "prepare_purchase":
            return self._prepare_purchase(args, account=account)
        if name == "list_stores":
            return list_stores_result(args, rpc_call=self.purchase_rpc_call)
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

    def _prepare_purchase(
        self, args: dict[str, Any], *, account: str | None = None
    ) -> dict[str, Any]:
        """Transport only: the whole pre-flight lives in :mod:`gecko.prepare_purchase`.

        Every refusal is already structured there, so there is nothing to decide here —
        which is the point: a public front door that made its own policy decisions would
        be a second place the boundary could be wrong. That includes the buyer binding:
        this hands over the account and the directory and judges neither.
        """
        return prepare_purchase_result(
            args,
            build_call=self.purchase_build_call,
            rpc_call=self.purchase_rpc_call,
            account=account,
            wallets=self.wallets,
        )

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
