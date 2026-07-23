"""AgentApiClient — the one object that makes an API agent-usable.

Ties the layers together: ingest -> catalog (find) -> tools (comprehend) ->
caller (correct request) -> access (auth) -> response. Three modes (the canonical
``gecko.modes.CallMode``):
  - "recorded": synthesize the response from the spec (no network, no spend) — for demos/CI.
  - "live": actually call the upstream API with the session's auth.
  - "probe": the offline sandbox — a malformed call answers with the API's OWN
    synthetic error + remediation (see ``gecko.sandbox``); never reaches the wire.

Security seam (Priority 1/2): auth is only ever injected toward a host on the surface's
OUT-OF-BAND trust anchor (``surfaces.anchor_for``), never toward the spec's own (poison-
able) ``servers[]``. A quarantined/unverified surface fails closed — it degrades to
recorded/no-auth rather than leaking the customer's secret.
"""

from __future__ import annotations

import logging
import time
import urllib.error
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from . import search as searchmod
from .access import AuthError, AuthSession, is_refreshable, stub_session
from .caller import CallError, LiveTransport, PreparedRequest, build_request, execute
from .capture import capture_outcome
from .catalog import Catalog
from .events import emit_surf_event
from .fusion import RRF_K
from .graph import SurfaceGraph, build_graph
from .hints import declared_entity_hints
from .ingest import extract_operations, load_spec
from .modes import CallMode
from .planner import plan_for_query
from .sample import error_schema, example_from_schema, success_schema
from .sanitize import sanitize_schema
from .scale import should_surface_all
from .search import FusedHit, ScoredHit
from .surfaces import _host_of, anchor_for, spec_is_quarantined, surface_rev, tools_rev
from .tools import auth_location_is_safe, build_tools, to_tool

if TYPE_CHECKING:
    from .dense import DenseIndex

logger = logging.getLogger("gecko.client")

# Back-compat aliases: the response-schema pickers moved to ``gecko.sample`` (shared
# with the probe sandbox — the move dissolved the old client<->sandbox import cycle),
# but external callers/tests historically import them from here.
_success_schema = success_schema
_error_schema = error_schema

__all__ = [
    "AgentApiClient",
    "AmbiguousServerError",
    "FusedHit",
    "IntegrityError",
    "ScoredHit",
    "ToolNotFound",
    "ambiguous_server_message",
]


class IntegrityError(Exception):
    """Raised when the shipped tool set no longer matches the pinned spec (tamper)."""


class ToolNotFound(CallError):
    """Raised by ``get_tool`` for a name that is unknown OR auth-gated-unavailable.

    A ``CallError`` subtype so the whole get→prepare→call path shares one error family.
    The two cases collapse on purpose: an auth-gated tool the session can't satisfy is
    hidden from the usable set, so it is indistinguishable from "unknown" to the agent —
    we never leak that a callable-but-unauthed op exists (invariant #4)."""


class AmbiguousServerError(CallError):
    """A live call on a >1-server spec with no explicit ``base_url`` — fail closed.

    The money-API footgun: many specs list production FIRST and sandbox second
    (e.g. Woovi), so silently defaulting to ``servers[0]`` sends a live call to
    production. When the caller never chose a server, refusing loudly — with the
    full server list and the exact fix — beats guessing. Recorded/probe are
    untouched: they never reach the wire, so the ``servers[0]`` default stays a
    harmless synthesis template there."""


#: Server descriptions are untrusted spec text surfaced in an agent-facing error —
#: collapse whitespace and cap the length so a poisoned description can't smuggle
#: an instruction-sized payload into the message.
_SERVER_DESC_CAP = 80


def ambiguous_server_message(servers: list[Any]) -> str:
    """The one multi-server fail-closed message (client raise + ``gecko add`` refusal).

    Lists every server as ``[index] url (description)`` — the description only when
    the spec provides one; we never guess which server is the sandbox — and names the
    remediation for both the SDK (``base_url=``) and the CLI (``--base-url``)."""
    parts: list[str] = []
    for index, server in enumerate(servers):
        url = server.get("url", "") if isinstance(server, dict) else ""
        entry = f"[{index}] {url}"
        desc = server.get("description") if isinstance(server, dict) else None
        if isinstance(desc, str) and desc.strip():
            entry += f" ({' '.join(desc.split())[:_SERVER_DESC_CAP]})"
        parts.append(entry)
    return (
        f"live mode needs an explicit base_url: this spec declares {len(servers)} "
        f"servers — {', '.join(parts)} — pass base_url/--base-url to choose one."
    )


class AgentApiClient:
    def __init__(
        self,
        spec: str | dict,
        base_url: str | None = None,
        session: AuthSession | None = None,
        *,
        corpus_path: str | Path | None = None,
        surface_id: str | None = None,
        blurbs: Mapping[str, str] | None = None,
        live_transport: LiveTransport | None = None,
        declared_hints: Mapping[str, str] | None = None,
    ):
        """Make an API agent-usable from its OpenAPI spec.

        Live mode targets ``servers[0].url`` from the spec unless an explicit
        ``base_url`` is given — but ONLY when that choice is unambiguous: a live
        call on a spec that declares >1 servers with no explicit ``base_url``
        raises ``AmbiguousServerError`` instead of silently picking ``servers[0]``
        (the money-API footgun: production is often listed first, sandbox second).
        Recorded/probe never reach the wire, so they keep the ``servers[0]``
        default as a synthesis template. An explicit ``base_url`` also pins the
        trust anchor to that one host (see ``self.anchor``).

        ``corpus_path`` (opt-in, off by default) enables Phase-0 correctness-corpus
        capture on ``call()``: one control-plane-safe metadata record per call via the
        same narrow ``corpus.outcome_from`` boundary the HTTP server uses (never a body).
        """
        spec_is_url = isinstance(spec, str) and spec.startswith(("http://", "https://"))
        self.spec = load_spec(spec) if isinstance(spec, str) else spec
        # The raw spec servers list, exposed so callers can choose a non-default
        # server explicitly (e.g. a sandbox) instead of silently using servers[0].
        self.servers = self.spec.get("servers") or []
        servers = self.servers or [{}]
        self.base_url = base_url or servers[0].get("url", "")
        # Whether the caller CHOSE the target host. The live-call seam fails closed on
        # a multi-server spec when this is False — construction itself stays permissive
        # so recorded/probe use on the same spec keeps working with the servers[0]
        # default as a synthesis template.
        self._base_url_explicit = base_url is not None

        self.operations = extract_operations(self.spec)
        # S0 enrich (optional): pre-generated, already-sanitized blurbs (keyed by tool_name)
        # folded into the lexical overlap haystack. Pure data — no LLM/SDK reaches the
        # ranker (invariant #2). Absent -> the unchanged plain lexical baseline.
        self.catalog = Catalog(self.operations, blurbs)
        self.tools = build_tools(self.operations)
        self._tool_by_name = {t["name"]: t for t in self.tools}
        self._op_by_name = {to_tool(o)["name"]: o for o in self.operations}
        # Serve-time integrity anchor: re-derived and re-asserted before every request
        # so an in-memory tamper of the shipped tool list is caught, not served.
        self.tools_rev = tools_rev(self.tools)

        # Out-of-band trust anchor — the WHOLE exfil fix. The allowlist of hosts auth may
        # reach comes from provenance, NEVER from the served spec's servers[]:
        #   * explicit base_url  -> pinned to that host
        #   * a spec URL         -> pinned to the ingest host (servers[] ignored)
        #   * a local spec file / in-memory dict -> unverified (no host) -> no auth leaves
        # A file on disk is NOT dev-vouched provenance (registry download / vendored-spec
        # PR / "save this spec"); its servers[0] is attacker-controlled, so it fails closed
        # exactly like a dict. Any from-docs / low-confidence / poisoned spec is quarantined.
        spec_url = spec if (isinstance(spec, str) and spec_is_url) else None
        # Poison can enter PER-OP through the REQUEST side (tool x-poison-flag, from the
        # input schema/description) OR the RESPONSE side: recorded mode ($0, the default)
        # echoes the success-response schema's example/default/enum straight to the agent,
        # so a poisoned response schema is an agent-facing channel request-only defenses
        # miss. Response-schema poison is scanned with route_to_arg=False: its values do
        # NOT route into a request arg, so address SHAPES (a benign base58 pubkey in a
        # response example) don't false-flag, while real secrets and injected instructions
        # still do.
        #
        # BLAST RADIUS (per-TOOL, not per-SURFACE): a flagged tool is restricted
        # INDIVIDUALLY — recorded-only, NO auth injection for that tool (fail closed) —
        # while the rest of the surface stays live. This replaces the old rule where any
        # ONE poisoned tool disabled auth for EVERY tool (which false-disabled the whole
        # Birdeye surface over two FPs). The set is the single source of truth consulted by
        # ``_may_inject_auth_for`` (the auth gate) and ``_effective_mode`` (live->recorded).
        self._poisoned_tool_names: set[str] = {
            name
            for name, op in self._op_by_name.items()
            if self._tool_by_name[name].get("x-poison-flag")
            or sanitize_schema(success_schema(op), route_to_arg=False)[1]
        }
        # WHOLE-SURFACE quarantine (auth off for EVERY tool) stays reserved for a
        # whole-SPEC compromise signal (from-docs / x-review / low-confidence, via
        # ``spec_is_quarantined``) — OR the degenerate case where EVERY tool is
        # individually poisoned, so nothing is safe to serve live and the per-tool and
        # per-surface outcomes coincide. A PARTIAL poisoning (e.g. Birdeye: 2 of 88) no
        # longer quarantines the surface; only the flagged tools are restricted.
        all_tools_poisoned = bool(
            self._op_by_name
        ) and self._poisoned_tool_names == set(self._op_by_name)
        quarantined = spec_is_quarantined(self.spec) or all_tools_poisoned
        if self._poisoned_tool_names:
            logger.warning(
                "per-tool quarantine: %d of %d tools tripped the anti-poisoning sanitizer "
                "(auth injection disabled + recorded-only for those tools; the rest of the "
                "surface stays live)",
                len(self._poisoned_tool_names),
                len(self._op_by_name),
            )
        self.anchor = anchor_for(
            base_url=base_url,
            spec_url=spec_url,
            quarantined=quarantined,
        )
        # Back-compat surface: the set of hosts auth may reach (== the anchor's hosts).
        self._auth_allowed_hosts: set[str] = set(self.anchor.trusted_hosts)

        self.session = session or stub_session()
        # Injectable live-execution seam (default: real stdlib in ``caller.execute``).
        # A light fake is injected in tests so the live + self-heal path is falsifiable
        # offline; a real run leaves it None and hits the network exactly as before.
        self._live_transport = live_transport
        # An empty auth-header dict means the session can't satisfy auth-gated ops,
        # so we hide them from the agent (it would only mis-call them). A session
        # WITH auth (e.g. TxODDS) surfaces everything, unchanged.
        self._session_has_auth = bool(self.session.auth_headers())
        self._usable_tool_names = {
            t["name"]
            for t in self.tools
            if self._session_has_auth or not t.get("requires_auth")
        }
        # Below-scale rule (P0): when the usable surface is small enough to show in full, the
        # agent-facing ``search`` surfaces EVERY usable tool (no top-k truncation) — so Gecko
        # is strictly >= the raw OpenAPI dump and can't drop a zero-overlap paraphrase op the
        # lexical catalog structurally can't rank. Above the threshold, top-k stays on. See
        # ``gecko.scale`` for the single-source-of-truth threshold.
        self._surface_all = should_surface_all(
            [t for t in self.tools if t["name"] in self._usable_tool_names]
        )

        # Corpus capture context (opt-in). Metadata only; never the response body.
        self._corpus_path = corpus_path
        self.surface_rev = surface_rev(self.spec)
        self.surface_id = surface_id or _host_of(self.base_url) or "surface"
        # Lazily-built surface graph (§4) — the correlations/planning index over this
        # surface's operations. Built once on first plan request (pure, deterministic),
        # kept out of construction so a client that never plans pays nothing.
        self._surface_graph: SurfaceGraph | None = None
        # Injected customer-confirmed DECLARED hints (§12 confirm loop) — merged over
        # the spec's own x-gecko vocabulary at graph build. Injection (not disk I/O
        # here) keeps the client pure; the CLI/serve edge loads and passes them.
        self._declared_hints = dict(declared_hints or {})

    @property
    def surface_all(self) -> bool:
        """True when the usable surface is small enough to show in full (below scale).

        The single-source-of-truth scale gate (``gecko.scale.should_surface_all``), computed
        once at construction over the auth-filtered usable tools. The MCP surface reads this
        to decide list_tools projection — below scale it emits full defs (byte-identical to
        today), above scale lightweight refs — so there is never a second threshold."""
        return self._surface_all

    def add_declared_hints(self, hints: Mapping[str, str]) -> None:
        """Merge customer-confirmed DECLARED hints (§12 confirm loop) into this
        client's vocabulary — the serve-edge wiring for ``gecko graph confirm``.

        Invalidates the lazily-built graph so the next plan sees the upgraded
        ladder; a no-op merge is free (the graph is only rebuilt on next access)."""
        if not hints:
            return
        self._declared_hints.update(hints)
        self._surface_graph = None

    @property
    def surface_graph(self) -> SurfaceGraph:
        """The deterministic surface graph over this client's operations (§4), built and
        cached on first access. Pure/surface-only (invariants #1/#2) — operations, params,
        fields, and INFERRED ``feeds`` edges, never payloads. Powers ``plan_for``."""
        if self._surface_graph is None:
            # DECLARED vocabulary: the spec's own x-gecko hints (provider-authored,
            # §14), with injected customer confirmations winning on conflict — a
            # customer's local correction outranks the shipped hint (§13.2).
            declared = {**declared_entity_hints(self.spec), **self._declared_hints}
            self._surface_graph = build_graph(
                self.operations, surface_id=self.surface_id, declared=declared
            )
        return self._surface_graph

    def plan_for(self, query: str, tool_name: str) -> dict[str, Any] | None:
        """A supplier-chain plan (§5) for the operation behind ``tool_name`` under
        ``query`` — or ``None`` when the top op's required inputs are already satisfiable
        from the intent and flat search stands (no chain needed).

        This is the seam that carries ``graph.plan()`` to an agent: the MCP surface calls
        it on the TOP search hit and, when non-None, attaches the returned dict — ordered
        steps + provenance-carrying ``explain`` — to that hit. The plan is a
        suggestion-with-provenance; the agent still makes every call itself (Gecko never
        becomes the data plane). Returns a control-plane-safe dict (no auth, no payloads).
        """
        # A quarantined tool must not emit a steering plan. `call`/`prepare` already
        # refuse a poisoned tool; the plan block is an agent-facing advisory that could
        # walk the agent INTO the poisoned op (or use it as a chain step), so it is
        # gated on the same per-tool quarantine — fail-closed, no plan.
        if tool_name in self._poisoned_tool_names:
            return None
        op = self._op_by_name.get(tool_name)
        if op is None:
            return None
        return plan_for_query(self.surface_graph, op, query)

    # --- retrieval: thin, signature-stable delegators into ``gecko.search`` (the
    # split keeps the graph-extending call path and the ranking logic in separate
    # files; every retrieval WHY lives next to the logic it explains). -------------

    def search_scored(self, query: str, limit: int = 5) -> list[ScoredHit]:
        """The pure ranked retrieval substrate — ``score``/``is_fallback`` provenance for
        the retrieval eval + the out-of-scope confidence floor. A strict top-k ranker even
        below scale (see ``gecko.search.search_scored``)."""
        return searchmod.search_scored(
            self.catalog, self._usable_tool_names, query, limit
        )

    def search_ranked(self, query: str, limit: int = 5) -> list[ScoredHit]:
        """The provenance-carrying substrate of ``search``: surface-all below scale,
        strict top-k above (see ``gecko.search.ranked_hits``). The MCP surface reads the
        top hit's ``is_fallback`` from HERE to gate plan attachment on a genuine hit."""
        return searchmod.ranked_hits(
            self.catalog,
            self._op_by_name,
            self._usable_tool_names,
            self._surface_all,
            query,
            limit,
        )

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Agent-facing capability search (frozen dict shape ``{name, summary, path,
        method}``). A pure projection of ``search_ranked`` — the frozen shape and the
        scored view can never disagree on order. Below scale this returns every usable
        tool (never worse than the raw OpenAPI dump); above scale, the top-k ranker."""
        return searchmod.project_hits(self.search_ranked(query, limit))

    def search_hybrid_scored(
        self,
        query: str,
        limit: int = 5,
        *,
        dense_index: DenseIndex,
        k: int = RRF_K,
    ) -> list[FusedHit]:
        """Hybrid lexical+dense retrieval fused via RRF, with fusion provenance.
        ``is_fallback`` stays LEXICAL-ANCHORED — the OOS confidence floor (see
        ``gecko.search.hybrid_scored`` for the measured why)."""
        return searchmod.hybrid_scored(
            self.catalog,
            self._op_by_name,
            self._usable_tool_names,
            query,
            limit,
            dense_index=dense_index,
            k=k,
        )

    def search_hybrid(
        self,
        query: str,
        limit: int = 5,
        *,
        dense_index: DenseIndex,
        k: int = RRF_K,
    ) -> list[dict[str, Any]]:
        """Hybrid lexical+dense search. Returns the SAME frozen dict shape as ``search``
        (``{name, summary, path, method}``) — the agent-facing contract is unchanged; the
        dense arm only adds semantic reach behind it."""
        return searchmod.project_hits(
            self.search_hybrid_scored(query, limit, dense_index=dense_index, k=k)
        )

    def list_tools(self) -> list[dict[str, Any]]:
        return [t for t in self.tools if t["name"] in self._usable_tool_names]

    def get_tool(self, name: str) -> dict[str, Any]:
        """Fetch ONE usable tool's full callable def by name — the explicit
        fetch-one-in-full step (progressive disclosure) that completes the
        ref→resolve→call loop the above-scale ``to_lightweight_ref`` projection opens.

        Pure lookup: no network, no new comprehension, control-plane safe by construction
        (tool defs already hide auth headers, invariant #4). Resolves against the
        auth-filtered usable set, so an unknown OR auth-gated-unavailable name raises a
        typed ``ToolNotFound`` — never a bare ``KeyError``, never a leaked uncallable def.
        """
        if name not in self._usable_tool_names:
            raise ToolNotFound(f"no usable tool named {name!r}")
        return self._tool_by_name[name]

    def _assert_tools_integrity(self) -> None:
        """Fail closed if the shipped tools drifted from the pinned-spec revision."""
        if tools_rev(self.tools) != self.tools_rev:
            raise IntegrityError(
                "tool set changed since comprehension — refusing to serve (possible tamper)"
            )

    def _may_inject_auth_for(self, tool_name: str) -> bool:
        """Auth is injected for this tool ONLY if it is NOT per-tool quarantined, the
        session carries it, the surface is a pinned trust anchor, and the op's
        securityScheme keeps the secret in a header (not a loggable query/path). Any 'no'
        fails closed to no-auth.

        The per-tool poison gate is FIRST and unconditional: a flagged tool NEVER reaches
        the live auth path, independent of anchor state. This is the fail-closed guarantee
        for the narrowed (per-tool, not per-surface) blast radius — a poisoned tool cannot
        get the customer's secret even though its clean siblings on the same surface do."""
        if tool_name in self._poisoned_tool_names:
            return False
        op = self._op_by_name[tool_name]
        return (
            self._session_has_auth
            and self.anchor.may_inject_auth
            and auth_location_is_safe(self.spec, op)
        )

    def prepare(
        self, tool_name: str, args: dict[str, Any], *, inject_auth: bool = True
    ) -> PreparedRequest:
        self._assert_tools_integrity()
        tool = self._tool_by_name[tool_name]
        if tool.get("requires_auth") and not self._session_has_auth:
            raise CallError(
                f"tool '{tool_name}' requires authentication the current session "
                f"cannot provide (schemes: {tool.get('auth_schemes')})"
            )
        # Fail closed: only pass the secret when the anchor + location allow it. Otherwise
        # auth is None and build_request proceeds in no-auth mode (never leaks the token).
        # ``inject_auth=False`` (recorded mode) skips injection entirely: recorded never
        # hits the wire, so auth — and its host guard — is moot. Without this, a spec
        # fetched from a URL (anchor pinned to that host) with no ``base_url`` fails the
        # host guard on the empty synthesized host, so `gecko test` recorded shows spurious
        # auth failures on every gated op (a bad first-run for a new user).
        auth = (
            self.session.auth_headers()
            if (inject_auth and self._may_inject_auth_for(tool_name))
            else None
        )
        req = build_request(
            tool,
            args,
            self.base_url,
            auth,
            allowed_auth_hosts=self._auth_allowed_hosts,
        )
        emit_surf_event(
            "surf.prepare",
            surface_id=self.surface_id,
            tool_name=tool_name,
            plane="engine",
        )
        return req

    def _effective_mode(self, tool_name: str, mode: CallMode) -> CallMode:
        """Degrade live -> recorded when the surface can't be safely called live: a
        quarantined (poisoned-until-proven) surface, or one whose auth-expecting call
        can't inject its secret (would otherwise fire un-authenticated to an unpinned
        host). ``probe`` (like ``recorded``) passes through untouched — it never
        reaches the wire, so the auth/host guard is moot and even a quarantined
        surface may be probed."""
        if mode != "live":
            return mode
        if self.anchor.state == "quarantined":
            return "recorded"
        # Per-tool quarantine: a flagged tool is recorded-only regardless of the session —
        # unconditional so even a no-auth session can't fire a poisoned tool live.
        if tool_name in self._poisoned_tool_names:
            return "recorded"
        if self._session_has_auth and not self._may_inject_auth_for(tool_name):
            return "recorded"
        return mode

    # 401/403 = the auth-failure signals the reactive self-heal reacts to; every other
    # status flows straight back to the agent (the lifecycle never engages).
    _AUTH_FAIL_STATUSES = frozenset({401, 403})

    def _run_live(self, req: PreparedRequest) -> tuple[int, Any]:
        """Execute one live request, normalizing a raised ``HTTPError`` into a returned
        ``(status, body)`` so the self-heal hook inspects 401/403 uniformly whether the
        injected transport returned it or the stdlib path raised it. This normalization
        is local to the live path — ``caller.execute`` itself is byte-identical."""
        try:
            return execute(req, transport=self._live_transport)
        except urllib.error.HTTPError as exc:
            try:
                body: Any = exc.read().decode("utf-8")
            except Exception:  # noqa: BLE001 - a body we can't read is not fatal here
                body = ""
            return exc.code, body

    def _call_live(
        self, tool_name: str, args: dict[str, Any], req: PreparedRequest
    ) -> tuple[int, Any]:
        """The live call with a bounded-once reactive self-heal. On 401/403 from a
        refreshable session: invalidate, re-resolve fresh headers (the proactive refresh
        fires inside ``auth_headers()``), retry the identical call ONCE. A second failure
        raises a redacted ``AuthError`` — never an unbounded re-auth loop. A plain
        (non-refreshable) session skips the hook entirely (seam identity)."""
        status, body = self._run_live(req)
        if status in self._AUTH_FAIL_STATUSES and is_refreshable(self.session):
            self.session.invalidate()  # type: ignore[attr-defined]
            req = self.prepare(tool_name, args)
            status, body = self._run_live(req)
            if status in self._AUTH_FAIL_STATUSES:
                # redact-before-raise: only the host + status, never the token.
                host = (urlsplit(req.url).hostname or "").lower()
                raise AuthError(
                    f"auth rejected after one re-auth (status {status}) for host {host}"
                )
        return status, body

    def _call_probe(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """The probe branch: recorded's transport edge swapped for the offline sandbox.

        Deliberately does NOT run ``prepare``: the whole point of probing is that a
        malformed call comes BACK as the API's own synthetic error the agent can
        self-heal from — not a raised pre-flight ``CallError``. No wire is ever
        reached, so auth injection and its host guard are skipped exactly like
        recorded (invariant #3: the modes diverge only at the transport edge).

        ``session_id`` will key the per-session ``SimWorld`` (the stateful gate);
        it is accepted now so the agent-facing contract is stable, and never leaves
        the process.
        """
        del session_id  # consumed by the SimWorld state gate when it lands
        from . import (
            sandbox,
        )  # lazy: keeps the sandbox machinery off the import hot path

        self._assert_tools_integrity()
        if tool_name not in self._op_by_name:
            raise ToolNotFound(f"no usable tool named {tool_name!r}")
        op = self._op_by_name[tool_name]
        sim = sandbox.evaluate(op, args)
        self._capture(tool_name, sim.status, None, args, None, "probe")
        return {
            "status": sim.status,
            "method": op.method,
            # The TEMPLATED path only — never a filled URL (control plane): a
            # malformed probe may lack the very params a URL would interpolate.
            "path": op.path,
            "data": sim.data,
            "mode": "probe",
            "mode_note": sim.mode_note,
            "signals": sim.signals,
            "remediation": sim.remediation,
        }

    def call(
        self,
        tool_name: str,
        args: dict[str, Any],
        mode: CallMode = "recorded",
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Invoke a tool in one of the three modes (see ``gecko.modes.CallMode``).

        ``session_id`` is an opaque correlation token consumed ONLY by probe mode —
        it will key the per-session sandbox world (SimWorld) so concurrent agents
        never see each other's synthetic state. It is never sent upstream and is
        ignored by recorded/live."""
        effective = self._effective_mode(tool_name, mode)
        if effective == "probe":
            return self._call_probe(tool_name, args, session_id=session_id)
        # Recorded synthesizes from the schema and never hits the wire, so don't inject
        # auth (or run its host guard) — that's a live-only concern.
        _inject = effective == "live"
        start = time.perf_counter()
        try:
            # The multi-server fail-closed guard sits exactly where a call would leave
            # the machine (EFFECTIVE live, after any degrade-to-recorded), so recorded,
            # probe, and the quarantine degradation — the $0 flows — never gain friction.
            if _inject and not self._base_url_explicit and len(self.servers) > 1:
                raise AmbiguousServerError(ambiguous_server_message(self.servers))
            req = self.prepare(tool_name, args, inject_auth=_inject)
        except CallError as exc:
            # A pre-flight failure (missing param / auth-host refusal) is itself a
            # first-call outcome worth capturing; record it, then propagate unchanged.
            self._capture(tool_name, None, exc, args, None, effective)
            raise
        if effective == "live":
            status, body = self._call_live(tool_name, args, req)
            self._capture(
                tool_name,
                status,
                None,
                args,
                int((time.perf_counter() - start) * 1000),
                effective,
            )
            return {
                "status": status,
                "request": req.url,
                "method": req.method,
                "data": body,
                "mode": "live",
            }
        schema = success_schema(self._op_by_name[tool_name])
        # Scrub the response schema before synthesizing agent-visible data: drop any
        # secret-looking or instruction-shaped example/default/enum so a poisoned response
        # schema can't surface a prompt-injection string / attacker address / leaked key.
        # route_to_arg=False: a response value isn't a tool arg, so address shapes aren't
        # dropped here (a benign pubkey in a response example is legitimate output).
        clean, _ = sanitize_schema(schema, route_to_arg=False)
        self._capture(tool_name, 200, None, args, None, effective)
        return {
            "status": 200,
            "request": req.url,
            "method": req.method,
            "data": example_from_schema(clean),
            "mode": "recorded",
            # Make demo-mode unmistakable to the agent: values are synthesized from the
            # response schema, NOT live upstream data. Without this an agent can't tell a
            # zeroed placeholder apart from "fixture not found" (external report #3).
            "mode_note": (
                "Values are synthesized from the API's response schema for a $0 offline "
                "demo — they are placeholders, not live data. Point Gecko at your own "
                "subscription for real responses."
            ),
        }

    def _capture(
        self,
        tool_name: str,
        status: int | None,
        exc: BaseException | None,
        args: dict[str, Any],
        latency_ms: int | None,
        mode: str,
    ) -> None:
        """One control-plane-safe outcome capture per call — metadata only, never the
        body or filled URL. Thin delegator into ``gecko.capture.capture_outcome`` (the
        telemetry + opt-in corpus edge); the client only resolves its own state (the
        tool def, whether auth was injectable for this op)."""
        op = self._op_by_name.get(tool_name)
        capture_outcome(
            tool_name=tool_name,
            status=status,
            exc=exc,
            args=args,
            latency_ms=latency_ms,
            mode=mode,
            surface_id=self.surface_id,
            surface_rev=self.surface_rev,
            corpus_path=self._corpus_path,
            tool=self._tool_by_name.get(tool_name),
            auth_injected=bool(op is not None and self._may_inject_auth_for(tool_name)),
        )
