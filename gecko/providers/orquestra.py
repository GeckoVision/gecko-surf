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

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from ..pda import PdaDerivationError, PdaNode, derive_pda
from ..rpc import RpcError

# Path A: this surface can also close a built plan into a Receipt. Imported at module
# level (not lazily) so the offline test can monkeypatch `gecko.providers.orquestra.simulate`
# and assert the tool routes to the engine without any network.
from ..simulate import SimulateError, simulate

__all__ = ["Intent", "OrquestraProgramSurface"]

# The honesty caveat stamped on every Receipt this surface returns.
_SIMULATE_NOTE = (
    "A Receipt says whether this tx would LAND against a state snapshot (fork/RPC — NOT "
    "mainnet, NOT a price/slippage prediction). Gecko simulates only; it never signs or "
    "broadcasts. Nothing is stored."
)


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
        tools.append(_SIMULATE_TOOL)
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
        if name == "simulate":
            return self._simulate_tool(args)
        intent = self.intents.get(name)
        if intent is not None:
            return self._plan(intent, args)
        return {"error": f"unknown tool {name!r}"}

    def _simulate_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        """Path A: build the supplied plan and simulate it into a Receipt.

        Generic — any program's built plan (``instruction``/``accounts``/``args``/
        ``feePayer``/``build_url``) can be simulated, not just pump. A supplied
        ``fee_recipient`` (pump's honest gap) is merged into ``accounts`` here; Gecko
        never guesses it. Uses the engine's default transport — never signs/broadcasts.
        """
        plan = args.get("plan")
        if not isinstance(plan, Mapping):
            return {"error": "simulate needs a built `plan` object"}
        rpc_url = args.get("rpc_url")
        if not isinstance(rpc_url, str) or not rpc_url:
            return {
                "error": "simulate needs an `rpc_url` (surfpool fork or mainnet RPC)"
            }

        accounts = dict(plan.get("accounts") or {})
        fee_recipient = args.get("fee_recipient")
        if fee_recipient:
            accounts["fee_recipient"] = fee_recipient
        merged_plan = {**plan, "accounts": accounts}
        track = list(args.get("track") or ())

        try:
            receipt = simulate(merged_plan, rpc_url=rpc_url, track=track)
        except (SimulateError, RpcError) as exc:
            return {"error": str(exc)}
        return {
            "instruction": plan.get("instruction"),
            "receipt": asdict(receipt),
            "note": _SIMULATE_NOTE,
        }

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


_SIMULATE_TOOL = {
    "name": "simulate",
    "description": (
        "Simulate a built plan into a Receipt: does this tx LAND against a state snapshot, "
        "with compute units + a categorical revert class if not. Give the plan (from a "
        "plan_* tool, with `fee_recipient` supplied), an `rpc_url` (surfpool fork or "
        "mainnet RPC), and optional `track` accounts for a SOL delta. Gecko simulates only "
        "— never signs or broadcasts; the Receipt is a fork/RPC snapshot, NOT mainnet or a "
        "price prediction, and is stored nowhere."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "plan": {"type": "object"},
            "rpc_url": {"type": "string"},
            "fee_recipient": {"type": "string"},
            "track": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["plan", "rpc_url"],
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
