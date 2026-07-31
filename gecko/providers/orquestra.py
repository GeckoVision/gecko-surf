"""The Orquestra provider surface — the agent front door that points at his builder.

The settled integration model (docs/specs/2026-07-31-orquestra-provider-integration.md):
the agent connects to a GECKO surface, which translates a plain intention into the right
instruction, **derives the PDAs Orquestra can't** (the helper-seeded roots its IDL drops),
and hands back a plan that **points at Orquestra's own ``/instructions/:name/build``** to
execute. Gecko is the metadata/control plane; Orquestra runs the tx. We never proxy his
builder and never sign.

This module is the generic surface; a provider *instance* (e.g. ``gecko.providers.meteora``)
supplies the program id, the recovered PDA recipes, the Orquestra project base URL, and the
intent → (instruction, derivation) map. One engine, parameterized by program — not a mount
each.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..pda import PdaDerivationError, PdaNode, derive_pda

__all__ = ["Intent", "OrquestraProgramSurface"]


@dataclass(frozen=True)
class Intent:
    """A plain-English capability mapped to an Orquestra instruction + how to derive its
    accounts. ``inputs`` are what the agent supplies (mints, params); ``plan`` derives the
    PDA account set from those inputs (chaining through the root, e.g. lb_pair → reserves).
    """

    name: str
    instruction: str  # the Orquestra instruction to /build
    description: str
    inputs: tuple[str, ...]
    plan: Callable[["OrquestraProgramSurface", Mapping[str, Any]], dict[str, str]]


@dataclass(frozen=True)
class OrquestraProgramSurface:
    """A duck-typed MCP surface (``list_tools`` + ``call_tool``) for one Orquestra program."""

    program_id: str
    project_base_url: str  # https://api.orquestra.dev/api/<project>
    pdas: dict[str, PdaNode]
    intents: dict[str, Intent] = field(default_factory=dict)

    @property
    def surface_id(self) -> str:
        return f"orquestra:{self.program_id}"

    # -- derivation (the depth Orquestra lacks) -----------------------------

    def derive(self, account: str, bindings: Mapping[str, Any]) -> str:
        """Derive one of this program's PDAs (raises PdaDerivationError with context)."""
        node = self.pdas.get(account)
        if node is None:
            raise PdaDerivationError(f"no PDA {account!r}; known: {sorted(self.pdas)}")
        return derive_pda(node, bindings).address

    def build_url(self, instruction: str) -> str:
        return f"{self.project_base_url.rstrip('/')}/instructions/{instruction}/build"

    # -- MCP surface --------------------------------------------------------

    def list_tools(self, **_kwargs: Any) -> list[dict[str, Any]]:
        tools = [_GRAPH_TOOL, _DERIVE_TOOL]
        for intent in self.intents.values():
            tools.append(_intent_tool(intent))
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        args = arguments or {}
        if name == "get_program_graph":
            return {
                "program_id": self.program_id,
                "executes_via": self.project_base_url,
                "pdas": {n: _pda_summary(p) for n, p in self.pdas.items()},
                "intents": [i.name for i in self.intents.values()],
            }
        if name == "derive_pda":
            return self._derive_tool(
                str(args.get("account", "")), args.get("bindings") or {}
            )
        intent = self.intents.get(name)
        if intent is not None:
            return self._plan(intent, args)
        return {"error": f"unknown tool {name!r}"}

    def _derive_tool(self, account: str, bindings: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"account": account, "address": self.derive(account, bindings)}
        except PdaDerivationError as exc:
            return {"account": account, "error": str(exc)}

    def _plan(self, intent: Intent, args: dict[str, Any]) -> dict[str, Any]:
        missing = [k for k in intent.inputs if k not in args]
        if missing:
            return {
                "error": f"intent {intent.name!r} needs: {missing}",
                "inputs": list(intent.inputs),
            }
        try:
            derived = intent.plan(self, args)
        except PdaDerivationError as exc:
            return {"intent": intent.name, "error": str(exc)}
        # The plan: Gecko derived the PDAs (incl. the root Orquestra can't); the agent now
        # calls Orquestra's builder to make the tx. We point; we do not proxy.
        return {
            "intent": intent.name,
            "instruction": intent.instruction,
            "derived": derived,
            "execute": {
                "method": "POST",
                "url": self.build_url(intent.instruction),
                "note": "supply these derived accounts + your own token accounts to Orquestra's builder",
            },
        }


def _pda_summary(node: PdaNode) -> dict[str, Any]:
    return {"resolvable": node.resolvable, "needs": list(node.required_bindings)}


_GRAPH_TOOL = {
    "name": "get_program_graph",
    "description": (
        "Return this program's derivable PDAs (with what each needs) and the intents you "
        "can plan. Execution runs on Orquestra's builder — this surface tells you what to "
        "call and how to derive the accounts first-call-correct."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}

_DERIVE_TOOL = {
    "name": "derive_pda",
    "description": (
        "Derive one of this program's PDAs — including the helper-seeded roots an IDL "
        "drops. Give the account name and its bindings (mints as base58, ints as numbers)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "account": {"type": "string"},
            "bindings": {"type": "object"},
        },
        "required": ["account"],
        "additionalProperties": False,
    },
}


def _intent_tool(intent: Intent) -> dict[str, Any]:
    return {
        "name": intent.name,
        "description": intent.description,
        "inputSchema": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in intent.inputs},
            "required": list(intent.inputs),
            "additionalProperties": False,
        },
    }
