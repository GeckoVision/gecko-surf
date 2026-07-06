"""MCP surface — what an agent actually installs.

`McpSurface` is a framework-agnostic, fully testable view (list_tools / call_tool)
over an AgentApiClient. It adds one synthetic tool — `search_capabilities` — so an
agent can go from natural-language intent to the right endpoint, then call it.

The optional `serve_stdio()` wraps it with the `mcp` SDK for a real server; it's
import-guarded so the surface (and its tests) work without the SDK installed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from .client import AgentApiClient
from .comprehend_service import (
    ComprehendError,
    comprehend_submission,
    ensure_submittable,
)
from .enforce import (
    FAIL_CLOSED_SIGNAL,
    EnforceMode,
    apply_gate,
    attach_warning,
    blocked_signals,
    enforce_mode_from_env,
    fail_closed_refusal,
    is_write_method,
    refusal_payload,
)
from .events import emit_surf_event
from .honeypot import (
    HONEYPOT_DECISION,
    HONEYPOT_REASON,
    decoy_tool_defs,
    honeypot_refusal,
    honeypots_from_env,
    is_decoy,
)
from .risk import RiskAssessment, RiskPolicy, assess_from_client, policy_from_client

logger = logging.getLogger("gecko.mcp_server")

_SEARCH_TOOL = {
    "name": "search_capabilities",
    "description": "Find which endpoint/tool fits a natural-language intent. Returns ranked tool names you can then call.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to do, in plain language.",
            }
        },
        "required": ["query"],
    },
}


class McpSurface:
    def __init__(
        self,
        client: AgentApiClient,
        mode: str = "recorded",
        *,
        enforce: EnforceMode | None = None,
        policy: RiskPolicy | None = None,
        honeypots: bool | None = None,
    ):
        """``enforce`` sets the call-time risk gate stance (block | warn | off); ``None``
        resolves ``GECKO_ENFORCE`` (default: warn — a bare surface only observes). The
        HOSTED builders inject ``block`` explicitly. ``policy`` is the auto-derived
        allowed-tools + trusted-hosts set; ``None`` derives it lazily from the client's
        comprehension on first assessment (the operator only tunes thresholds).

        ``honeypots`` opts IN to the decoy tripwire (``None`` resolves ``GECKO_HONEYPOTS``,
        default OFF). It is a DETECTION layer, not a moat — off by default so a real
        surface never shows fake tools unless the operator asks; when off, ``list_tools``
        is byte-identical to a surface with no honeypot layer."""
        self.client = client
        self.mode = mode
        self.enforce: EnforceMode = (
            enforce if enforce is not None else enforce_mode_from_env()
        )
        self._policy = policy
        self.honeypots: bool = (
            honeypots if honeypots is not None else honeypots_from_env()
        )

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [_SEARCH_TOOL]
        for t in self.client.list_tools():
            tools.append({k: t[k] for k in ("name", "description", "inputSchema")})
        # Opt-in only: expose the decoys so a PROBING agent enumerating the surface sees
        # a tempting target. Off by default -> tools stay byte-identical to no honeypots.
        if self.honeypots:
            tools.extend(decoy_tool_defs())
        return tools

    def _assess(self, name: str, arguments: dict[str, Any]) -> RiskAssessment | None:
        """Score a call, FAILING OPEN on a scorer bug. Returns the assessment, or ``None``
        (→ treat as allow) if scoring itself raised — a scoring bug must never break the
        product. A *decided* block is still a real block; fail-open only covers the
        "we couldn't score it" case, never a "we scored it dangerous" case."""
        if self._policy is None:
            try:
                self._policy = policy_from_client(self.client)
            except Exception:  # noqa: BLE001 - fail open: can't derive policy -> allow
                logger.warning("risk policy derivation failed; failing open (allow)")
                return None
        try:
            return assess_from_client(self.client, name, arguments, policy=self._policy)
        except Exception:  # noqa: BLE001 - fail open on a scoring bug, never break a call
            logger.warning("risk assessment failed; failing open (allow)")
            return None

    def _is_write_op(self, name: str) -> bool:
        """True iff the named op mutates upstream state — read from the client's OWN
        comprehension, NOT the (possibly-crashed) policy. Used only on the fail-closed
        path (G1/G4): when scoring/policy-derivation raised, a state-changing op is refused
        rather than waved through. An op we can't resolve degrades to read (fail-open) so a
        bare fake client with no operations keeps working — a real hosted client always
        carries its operations, so a real write is always caught."""
        from .tools import tool_name

        for op in getattr(self.client, "operations", None) or []:
            if tool_name(op) == name:
                return is_write_method(getattr(op, "method", "get"))
        return False

    def call_tool(
        self, name: str, arguments: dict[str, Any], session_id: str | None = None
    ) -> Any:
        """Invoke a tool. ``session_id`` (the MCP transport session, when the caller
        is the HTTP surface) is threaded onto the usage event ONLY as an opaque
        correlation token — it joins connect->call for the retention funnel and is
        sanitized by ``emit_surf_event``; it never touches the upstream call."""
        if name == "search_capabilities":
            hits = self.client.search(arguments.get("query", ""))
            # Observe, never mutate: usage metadata only (result breadth k), never the query.
            emit_surf_event(
                "surf.search",
                surface_id=self.client.surface_id,
                k=len(hits),
                session_id=session_id,
            )
            return hits

        # --- The honeypot tripwire (opt-in): a decoy has no originating operation, so a
        # CALL of one is definitionally hostile probing. Trip BEFORE the normal gate; there
        # is no upstream to invoke (it is a decoy), so nothing is called and no payload is
        # synthesized. Record ONLY the control-plane-safe fingerprint — the sanitized
        # session correlation + the decoy NAME (a code constant) + the code-constant
        # signal — never the args, never a fake output. Detection, not a moat. ----------
        if self.honeypots and is_decoy(name):
            emit_surf_event(
                "surf.blocked",
                surface_id=self.client.surface_id,
                tool_name=name,  # the decoy name is spec-derived, a code constant
                mode=self.mode,
                decision=HONEYPOT_DECISION,
                reasons=[HONEYPOT_REASON],
                session_id=session_id,
            )
            return honeypot_refusal()

        # --- The enforcement gate: score BEFORE the upstream call, then dispatch through
        # the ONE shared gate (enforce.apply_gate) — no inline block/warn logic here. ----
        assessment = self._assess(name, arguments) if self.enforce != "off" else None
        # Fail CLOSED on a WRITE we could not score (G1/G4): a scorer or policy-derivation
        # crash returns ``None`` (fail-open for reads — a scoring bug must not break a
        # harmless GET), but a state-changing op is refused rather than waved through. A
        # crash-input can no longer turn the fail-open robustness feature into a bypass.
        if assessment is None and self.enforce == "block" and self._is_write_op(name):
            emit_surf_event(
                "surf.blocked",
                surface_id=self.client.surface_id,
                tool_name=name,
                mode=self.mode,
                decision="block",
                reasons=[FAIL_CLOSED_SIGNAL],
                session_id=session_id,
            )
            return fail_closed_refusal()
        outcome = apply_gate(assessment, self.enforce)
        if outcome.blocked and assessment is not None:
            # Hard block: the upstream API is NEVER called. Emit the countable event
            # (signal NAMES only — never the value-bearing human message) and return a
            # structured refusal the agent can read.
            emit_surf_event(
                "surf.blocked",
                surface_id=self.client.surface_id,
                tool_name=name,
                mode=self.mode,
                score=assessment.score,
                decision="block",
                reasons=blocked_signals(assessment),
                session_id=session_id,
            )
            return refusal_payload(assessment)

        result = self.client.call(name, arguments, mode=self.mode)
        emit_surf_event(
            "surf.call",
            surface_id=self.client.surface_id,
            tool_name=name,
            mode=self.mode,
            session_id=session_id,
        )
        # A step_up (or a warn-mode would-be block) executed — flag it, don't hide it.
        if outcome.warn and assessment is not None:
            return attach_warning(result, assessment)
        return result


_COMPREHEND_TOOL = {
    "name": "comprehend_api",
    "description": (
        "Submit an API's OpenAPI URL (or a human docs page URL with from_docs=true) and "
        "get it comprehended into first-call-correct agent tools — no integration code. "
        "Returns the API name, its usable tools, agent-native artifacts (llms.txt / "
        "gecko.json / tools.md), and self-host next steps. Comprehends and returns to YOU "
        "only: it does not host, publicly list, or register your API."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The API's OpenAPI spec URL (or a docs page URL if from_docs).",
            },
            "from_docs": {
                "type": "boolean",
                "description": (
                    "Recover the surface from a human docs page instead of an OpenAPI "
                    "spec. Results are quarantined pending review."
                ),
                "default": False,
            },
        },
        "required": ["url"],
    },
}


class MetaComprehendSurface:
    """A minimal synthetic MCP surface with ONE tool: ``comprehend_api``.

    The agent-facing door to the same core the HTTP ``POST /comprehend`` route calls
    (one engine, two front doors). An agent submits an API URL and gets first-call-correct
    tools back — comprehended FOR THE CALLER ONLY.

    MVP scope — comprehend-and-return only. It deliberately does NOT host, publicly list,
    or register the submitted API: ephemeral hosting is an explicit later tier and public
    listing is a hard non-goal (no public catalog). It carries no ``AgentApiClient``, so
    it is not wrapped in :class:`McpSurface`; the HTTP layer duck-types it as a surface.
    """

    surface_id = "gecko-meta"

    def list_tools(self) -> list[dict[str, Any]]:
        return [_COMPREHEND_TOOL]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name != "comprehend_api":
            raise ComprehendError(f"unknown tool: {name}")
        url = arguments.get("url", "")
        if not isinstance(url, str) or not url:
            raise ComprehendError("comprehend_api requires a 'url' argument")
        ensure_submittable(url)  # remote door: http(s) only, no local file read
        result = comprehend_submission(
            url, from_docs=bool(arguments.get("from_docs", False))
        )
        return asdict(result)


def serve_stdio(
    spec: str, base_url: str | None = None, mode: str = "recorded"
) -> None:  # pragma: no cover
    """Run a real MCP stdio server (requires the `mcp` package)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Install the `mcp` package to run the stdio server: uv add mcp"
        ) from exc

    surface = McpSurface(AgentApiClient(spec, base_url=base_url), mode=mode)
    server = FastMCP("gecko")
    for tool in surface.list_tools():

        def _make(tool_name):
            def _handler(**kwargs):
                return surface.call_tool(tool_name, kwargs)

            return _handler

        server.add_tool(
            _make(tool["name"]), name=tool["name"], description=tool["description"]
        )
    server.run()
