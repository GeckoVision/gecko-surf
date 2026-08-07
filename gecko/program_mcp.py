"""Serve a program's instruction↔PDA graph as an agent-facing MCP surface.

Phase 3 of the Program Surface. Phases 0–2 recover the seed recipes an IDL/llms.txt
loses and assemble the derivation graph; this projects that graph into
question-shaped, first-call-correct MCP tools an agent (or an AI-SDK playground) calls
directly — the paybox-style, no-friction UX ("what do I derive, and how?").

``ProgramGraphSurface`` is a duck-typed surface (``list_tools`` + ``call_tool``), so it
serves over the existing Streamable-HTTP and stdio transports unchanged
(:func:`gecko.http_server.serve_http` / the stdio path) — Berkay's orchestrator, or an
agent, connects and reads the context clearly. Keyless and control-plane only: PDA
derivation needs no secret and no on-chain write; the surface reads a data model and
computes addresses, it never signs.

Tools:
  - ``get_program_graph`` — the full structured graph (all instructions + PDA recipes).
  - ``plan_instruction`` — the dependency-ordered derive plan for one instruction.
  - ``derive_pda`` — derive one PDA's address + bump from bindings (the actionable step).
"""

from __future__ import annotations

from typing import Any

from .pda import PdaDerivationError, derive_pda
from .program_graph import ProgramGraph, build_program_graph

__all__ = ["ProgramGraphSurface", "build_program_surface", "main"]


_GET_GRAPH_TOOL = {
    "name": "get_program_graph",
    "description": (
        "Return the full instruction↔PDA derivation graph for this Solana program: "
        "every instruction, which of its accounts are program-derived (PDAs), and how "
        "each PDA is derived (its seeds and what they come from). Structured JSON an "
        "orchestrator can ingest — the join a raw IDL/llms.txt does not carry."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}

_PLAN_TOOL = {
    "name": "plan_instruction",
    "description": (
        "Return the friction-free build plan for one instruction: which PDA accounts to "
        "derive, in dependency order, and what each is derived from (which accounts / "
        "args). Ask this before building a transaction — it tells you exactly what to "
        "derive and supply, with no guessing. If no order exists (a seed-dependency "
        "cycle), the affected accounts are listed under 'cycle' and each is marked "
        "'unorderable' — do not derive those in the order shown."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "The instruction name, e.g. 'open_round'.",
            }
        },
        "required": ["instruction"],
        "additionalProperties": False,
    },
}

_DERIVE_TOOL = {
    "name": "derive_pda",
    "description": (
        "Derive a program-derived address (PDA) for this program. Give the account name "
        "(e.g. 'miner') and any seed bindings it needs — accounts as base58 pubkeys, "
        "integer args as numbers, string args as text. Returns the on-chain address and "
        "bump. If the recipe needs a binding you didn't supply, the response says which; "
        "if a seed can't be resolved statically, it says so instead of guessing."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "account": {
                "type": "string",
                "description": "The PDA account name, e.g. 'config' or 'miner'.",
            },
            "bindings": {
                "type": "object",
                "description": (
                    "seed name → value: a base58 pubkey for an account seed, a number for "
                    "an integer arg seed, text for a string seed. Omit for a PDA with only "
                    "constant seeds."
                ),
            },
        },
        "required": ["account"],
        "additionalProperties": False,
    },
}


class ProgramGraphSurface:
    """A duck-typed MCP surface over one program's :class:`ProgramGraph`."""

    def __init__(self, graph: ProgramGraph) -> None:
        self.graph = graph
        # a stable id for the serve layer's correlation events (control-plane only)
        self.surface_id = f"program:{graph.program_id or 'unknown'}"

    def list_tools(self, **_kwargs: Any) -> list[dict[str, Any]]:
        """The MCP ``tools/list`` view — three question-shaped tools, full defs."""
        return [_GET_GRAPH_TOOL, _PLAN_TOOL, _DERIVE_TOOL]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        args = arguments or {}
        if name == "get_program_graph":
            return self.graph.to_json()
        if name == "plan_instruction":
            return self._plan(str(args.get("instruction", "")))
        if name == "derive_pda":
            return self._derive(
                str(args.get("account", "")), args.get("bindings") or {}
            )
        return {
            "error": f"unknown tool {name!r}",
            "available": [t["name"] for t in self.list_tools()],
        }

    # -- tool bodies --------------------------------------------------------

    def _plan(self, instruction: str) -> dict[str, Any]:
        ix = next((i for i in self.graph.instructions if i.name == instruction), None)
        if ix is None:
            return {
                "error": f"no instruction {instruction!r}",
                "available": [i.name for i in self.graph.instructions],
            }
        payload = ix.to_json()
        # a compact, human-legible plan alongside the structured accounts
        steps = []
        by_name = {a["name"]: a for a in payload["accounts"]}
        cycle = set(payload["cycle"])
        for account in payload["derivation_order"]:
            a = by_name.get(account, {})
            steps.append(
                {
                    "account": account,
                    "resolvable": a.get("resolvable", True),
                    # its position in this list is arbitrary — a seed-dependency
                    # cycle means there is no order that works
                    "unorderable": account in cycle,
                    "derive_from": a.get("derive_from", []),
                }
            )
        payload["plan"] = steps
        return payload

    def _derive(self, account: str, bindings: dict[str, Any]) -> dict[str, Any]:
        node = self.graph.pdas.get(account)
        if node is None:
            return {
                "error": f"no PDA account {account!r}",
                "available": sorted(self.graph.pdas),
            }
        if not node.resolvable:
            return {
                "account": account,
                "resolvable": False,
                "unresolved": [
                    {
                        "name": s.name,
                        "depends_on": list(s.depends_on),
                        "reason": s.reason,
                    }
                    for s in node.unresolved_seeds
                ],
                "error": "recipe has unresolved seed(s); Gecko will not fabricate them",
            }
        try:
            result = derive_pda(node, bindings)
        except PdaDerivationError as exc:
            return {
                "account": account,
                "error": str(exc),
                # every operand to bind — variable-seed names plus ordered-pair operands
                "needs": list(node.required_bindings),
            }
        return {"account": account, "address": result.address, "bump": result.bump}


def build_program_surface(
    idl: dict[str, Any] | None = None,
    source: str | None = None,
    *,
    program_id: str | None = None,
) -> ProgramGraphSurface:
    """Build a servable surface from an Anchor IDL and/or program source."""
    graph = build_program_graph(idl=idl, source=source, program_id=program_id)
    return ProgramGraphSurface(graph)


def main(argv: list[str] | None = None) -> int:
    """CLI entry: serve a program's PDA graph over MCP from local IDL/source files.

    Point it at an Anchor IDL (JSON) and/or the program's Rust source; it comprehends
    the instruction↔PDA graph and serves the three tools over Streamable-HTTP (default)
    or stdio. Keyless — PDA derivation needs no credential.

        program-mcp --idl ore_idl.json --source state_mod.rs           # HTTP on :8000
        program-mcp --idl ore_idl.json --stdio                         # stdio for an agent

    Serving needs the ``[serve]`` extra:  uvx --from "gecko-surf[serve]" program-mcp ...
    """
    import argparse
    import json
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="program-mcp", description=main.__doc__)
    parser.add_argument("--idl", help="path to an Anchor IDL JSON file")
    parser.add_argument(
        "--source", help="path to the program's Rust source (fills IDL gaps)"
    )
    parser.add_argument("--program-id", help="override the program id (base58)")
    parser.add_argument(
        "--stdio", action="store_true", help="serve over stdio instead of HTTP"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--public-url", default=None, help="external URL when behind a tunnel/ALB"
    )
    args = parser.parse_args(argv)

    if not args.idl and not args.source:
        parser.error("give --idl and/or --source")

    idl = json.loads(Path(args.idl).read_text()) if args.idl else None
    source = Path(args.source).read_text() if args.source else None
    surface = build_program_surface(idl=idl, source=source, program_id=args.program_id)

    n_pdas = len(surface.graph.pdas)
    n_ix = len(surface.graph.instructions)
    # human output on stderr only — stdout is the stdio JSON-RPC channel
    print(
        f"program-mcp: {n_pdas} PDA recipe(s), {n_ix} instruction(s)", file=sys.stderr
    )

    if args.stdio:
        from .mcp_server import serve_stdio

        serve_stdio(surface, server_name="program-mcp")
    else:
        from .http_server import serve_http

        serve_http(
            surface,
            host=args.host,
            port=args.port,
            mode="recorded",
            server_name="program-mcp",
            public_url=args.public_url,
        )
    return 0
