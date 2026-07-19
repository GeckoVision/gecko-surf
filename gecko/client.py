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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from . import corpus
from .access import AuthError, AuthSession, is_refreshable, stub_session
from .caller import CallError, LiveTransport, PreparedRequest, build_request, execute
from .catalog import Catalog
from .fusion import RRF_K, rrf_fuse
from .events import emit_surf_event
from .graph import SurfaceGraph, build_graph
from .ingest import Operation, extract_operations, load_spec
from .modes import CallMode
from .planner import plan_for_query
from .sample import example_from_schema
from .sanitize import sanitize_schema
from .scale import should_surface_all
from .surfaces import _host_of, anchor_for, spec_is_quarantined, surface_rev, tools_rev
from .tools import auth_location_is_safe, build_tools, to_tool

if TYPE_CHECKING:
    from .dense import DenseIndex

logger = logging.getLogger("gecko.client")


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


@dataclass(frozen=True)
class ScoredHit:
    """A search result enriched with retrieval provenance — the introspection sibling
    of the frozen ``search`` dict shape. ``score``/``is_fallback`` power retrieval
    evaluation and the out-of-scope confidence floor; the agent-facing ``search`` never
    exposes them (its contract stays ``{name, summary, path, method}``)."""

    name: str
    summary: str
    path: str
    method: str
    score: int
    is_fallback: bool


@dataclass(frozen=True)
class FusedHit:
    """A hybrid (lexical+dense) search result with fusion provenance — the scored sibling of
    the frozen ``search_hybrid`` dict shape. ``score`` is the RRF score (drives order/recall).
    ``is_fallback`` is the OOS confidence floor and is LEXICAL-ANCHORED: True unless the
    lexical arm genuinely corroborated the hit (``score > 0``). The dense arm improves the
    RANKING but never sets confidence on its own — measured on ``voyage-4-lite``, its cosine
    scores are too compressed to separate an out-of-scope intent from a real paraphrase, so
    tying confidence to lexical corroboration guarantees OOS pass-rate >= the lexical baseline
    by construction, while dense still lifts paraphrase recall via rank."""

    name: str
    summary: str
    path: str
    method: str
    score: float
    is_fallback: bool


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
        # Poison can enter through the REQUEST side (tool x-poison-flag, from the input
        # schema/description) OR the RESPONSE side: recorded mode ($0, the default) echoes
        # the success-response schema's example/default/enum straight to the agent, so a
        # poisoned response schema is an agent-facing channel request-only defenses miss.
        # Response-schema poison quarantines too, but its values do NOT route into a
        # request arg, so scan it with route_to_arg=False: address SHAPES (a benign
        # base58 pubkey in a response example) don't false-quarantine, while real secrets
        # and injected instructions still do.
        poisoned = any(t.get("x-poison-flag") for t in self.tools) or any(
            sanitize_schema(_success_schema(op), route_to_arg=False)[1]
            for op in self.operations
        )
        quarantined = spec_is_quarantined(self.spec) or poisoned
        if poisoned:
            logger.warning(
                "surface quarantined: spec text tripped the anti-poisoning sanitizer "
                "(auth injection disabled, recorded-mode only until reviewed)"
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

    @property
    def surface_all(self) -> bool:
        """True when the usable surface is small enough to show in full (below scale).

        The single-source-of-truth scale gate (``gecko.scale.should_surface_all``), computed
        once at construction over the auth-filtered usable tools. The MCP surface reads this
        to decide list_tools projection — below scale it emits full defs (byte-identical to
        today), above scale lightweight refs — so there is never a second threshold."""
        return self._surface_all

    @property
    def surface_graph(self) -> SurfaceGraph:
        """The deterministic surface graph over this client's operations (§4), built and
        cached on first access. Pure/surface-only (invariants #1/#2) — operations, params,
        fields, and INFERRED ``feeds`` edges, never payloads. Powers ``plan_for``."""
        if self._surface_graph is None:
            self._surface_graph = build_graph(self.operations)
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
        op = self._op_by_name.get(tool_name)
        if op is None:
            return None
        return plan_for_query(self.surface_graph, op, query)

    def search_scored(self, query: str, limit: int = 5) -> list[ScoredHit]:
        """The pure ranked retrieval substrate — carries ``score``/``is_fallback`` (retrieval
        eval + the out-of-scope confidence floor). Applies the auth filter and top-k over-
        fetch. This is what the retrieval benchmark measures (recall@k / MRR), so it stays a
        strict top-k ranker even below scale; the agent-facing ``search`` layers the below-
        scale surface-all rule on TOP of it (see ``_surface_all_scored``)."""
        out: list[ScoredHit] = []
        for s in self.catalog.search_scored(query, limit + 20):
            if s.entry.tool_name not in self._usable_tool_names:
                continue
            out.append(
                ScoredHit(
                    name=s.entry.tool_name,
                    summary=s.entry.operation.summary,
                    path=s.entry.operation.path,
                    method=s.entry.operation.method,
                    score=s.score,
                    is_fallback=s.is_fallback,
                )
            )
            if len(out) >= limit:
                break
        return out

    def _surface_all_scored(self, query: str) -> list[ScoredHit]:
        """Below-scale: surface EVERY usable tool (no top-k truncation) so Gecko is never
        worse than the raw OpenAPI dump. Genuine lexical hits keep their relevance order and
        score; every remaining usable op is APPENDED as a score-0 fallback (GET-first then
        path — the catalog's query-independent prior), so a zero-overlap paraphrase op the
        lexical catalog structurally drops is still visible and pickable. ``is_fallback``
        stays truthful (appended ops are not genuine lexical matches), so any confidence-floor
        reader is unchanged and relevance never sinks below a manufactured candidate."""
        hits: list[ScoredHit] = []
        seen: set[str] = set()
        # Genuine lexical hits first, over the full usable pool (depth = #entries so nothing
        # is censored). Skip fallbacks here — we append the not-yet-seen ops ourselves below.
        for s in self.catalog.search_scored(query, len(self.catalog.entries)):
            name = s.entry.tool_name
            if name not in self._usable_tool_names or s.is_fallback:
                continue
            seen.add(name)
            op = s.entry.operation
            hits.append(ScoredHit(name, op.summary, op.path, op.method, s.score, False))
        remaining = [
            (name, op)
            for name, op in self._op_by_name.items()
            if name in self._usable_tool_names and name not in seen
        ]
        remaining.sort(key=lambda no: (0 if no[1].method == "GET" else 1, no[1].path))
        for name, op in remaining:
            hits.append(ScoredHit(name, op.summary, op.path, op.method, 0, True))
        return hits

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Agent-facing capability search (frozen dict shape ``{name, summary, path, method}``).

        Below scale (``_surface_all``) this returns every usable tool — the full surface,
        relevance-ordered — so a small/clean API is never worse than its raw OpenAPI dump.
        Above scale it is a pure projection of the top-k ``search_scored`` ranker."""
        hits = (
            self._surface_all_scored(query)
            if self._surface_all
            else self.search_scored(query, limit)
        )
        return [
            {"name": h.name, "summary": h.summary, "path": h.path, "method": h.method}
            for h in hits
        ]

    def search_hybrid_scored(
        self,
        query: str,
        limit: int = 5,
        *,
        dense_index: DenseIndex,
        k: int = RRF_K,
    ) -> list[FusedHit]:
        """Fuse the lexical arm (``catalog.search_scored``) with the injected dense arm via
        RRF, joined on ``tool_name``. Over-fetches both arms, fuses, applies the auth filter
        AFTER fusion, then truncates to ``limit`` (so reranking/hiding can't starve the top-k).
        ``search_hybrid`` is a pure projection of this — the two can never disagree on order.

        ``is_fallback`` is LEXICAL-ANCHORED (genuine iff the lexical arm scored the op > 0),
        the out-of-scope confidence floor: an OOS intent has no lexical overlap so nothing is
        promoted -> OOS pass-rate >= the lexical baseline by construction. The dense arm still
        lifts paraphrase recall because RANK (not the flag) drives recall.
        """
        depth = limit + 20
        lex = self.catalog.search_scored(query, depth)
        lex_names = [s.entry.tool_name for s in lex]
        lex_genuine = {s.entry.tool_name for s in lex if not s.is_fallback}

        dense_names = [n for n, _ in dense_index.search(query, depth)]

        fused = rrf_fuse([lex_names, dense_names], k)
        # Deterministic order: RRF score desc, then tool_name for stable ties.
        ranked = sorted(fused.items(), key=lambda ns: (-ns[1], ns[0]))

        out: list[FusedHit] = []
        for name, score in ranked:
            if name not in self._usable_tool_names:  # auth filter AFTER fusion
                continue
            op = self._op_by_name.get(name)
            if op is None:  # a stale dense doc for an op no longer on the surface
                continue
            out.append(
                FusedHit(
                    name=name,
                    summary=op.summary,
                    path=op.path,
                    method=op.method,
                    score=score,
                    is_fallback=name not in lex_genuine,
                )
            )
            if len(out) >= limit:
                break
        return out

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
        return [
            {"name": h.name, "summary": h.summary, "path": h.path, "method": h.method}
            for h in self.search_hybrid_scored(
                query, limit, dense_index=dense_index, k=k
            )
        ]

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

    def _may_inject_auth_for(self, op: Operation) -> bool:
        """Auth is injected for this op ONLY if the session carries it, the surface is a
        pinned trust anchor, and the op's securityScheme keeps the secret in a header
        (not a loggable query/path). Any 'no' fails closed to no-auth."""
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
        op = self._op_by_name[tool_name]
        # Fail closed: only pass the secret when the anchor + location allow it. Otherwise
        # auth is None and build_request proceeds in no-auth mode (never leaks the token).
        # ``inject_auth=False`` (recorded mode) skips injection entirely: recorded never
        # hits the wire, so auth — and its host guard — is moot. Without this, a spec
        # fetched from a URL (anchor pinned to that host) with no ``base_url`` fails the
        # host guard on the empty synthesized host, so `gecko test` recorded shows spurious
        # auth failures on every gated op (a bad first-run for a new user).
        auth = (
            self.session.auth_headers()
            if (inject_auth and self._may_inject_auth_for(op))
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
        op = self._op_by_name[tool_name]
        if self._session_has_auth and not self._may_inject_auth_for(op):
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
        from . import sandbox  # lazy: sandbox imports this module's schema helpers

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
        schema = _success_schema(self._op_by_name[tool_name])
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
        """Append one control-plane-safe correctness record — metadata only, never the
        body or filled URL. Uses the SAME narrow ``corpus.outcome_from`` boundary the
        HTTP server uses (it structurally cannot receive a payload). Opt-in via
        ``corpus_path``; a capture failure must never break the agent's call."""
        # Usage instrumentation (independent of opt-in corpus capture): one
        # control-plane-safe outcome event — the ok-bool + error CLASS, never a body.
        # ``source`` carries the SAME provenance the corpus record derives (recorded ->
        # synthetic, live -> observed), so the adoption FCC rate can filter observed-only
        # and a faked recorded 200 never inflates it.
        error_class = corpus.error_class_for(status, exc)
        source = corpus.source_for_mode(mode)
        # plane="engine": this fires on EVERY client call outcome — local $0 flows
        # (demo, `gecko test`, recorded) included — whereas surf.call is a SURFACE
        # event; see events.CallPlane for why all-time fcc > call is expected.
        emit_surf_event(
            "surf.first_call_correct",
            surface_id=self.surface_id,
            tool_name=tool_name,
            mode=mode,
            ok=status is not None and 200 <= status < 400,
            error_class=error_class,
            latency_ms=latency_ms,
            source=source,
            plane="engine",
        )
        if self._corpus_path is None:
            return
        tool = self._tool_by_name.get(tool_name)
        invoke = tool.get("_invoke") if isinstance(tool, dict) else None
        if not isinstance(invoke, dict):
            return
        op = self._op_by_name.get(tool_name)
        corpus.record(
            corpus.outcome_from(
                operation_id=tool_name,
                tool_invoke=invoke,
                args=args,
                status=status,
                error_class=error_class,
                latency_ms=latency_ms,
                mode=mode,
                auth_injected=bool(op is not None and self._may_inject_auth_for(op)),
                ts=int(time.time() * 1000),
                surface_id=self.surface_id,
                surface_rev=self.surface_rev,
            ),
            self._corpus_path,
        )


def _response_schema(op: Any, codes: tuple[str, ...]) -> dict[str, Any]:
    """The first declared JSON response schema among ``codes`` (in order)."""
    for code in codes:
        r = op.responses.get(code)
        if not isinstance(r, dict):
            continue
        content = r.get("content", {}) or {}
        media = content.get("application/json") or next(iter(content.values()), None)
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            return media["schema"]
    return {}


def _success_schema(op: Any) -> dict[str, Any]:
    """The op's declared success-response schema — powers recorded/probe synthesis."""
    return _response_schema(op, ("200", "201", "default"))


def _error_schema(op: Any) -> dict[str, Any]:
    """The op's OWN declared error-response schema (sibling of ``_success_schema``).

    The comprehension-native differentiator for probe mode: a malformed offline call
    answers with a body shaped like THIS API's error, not a generic Gecko message.
    ``422`` is scanned first so the body shape aligns with the synthetic 422 status
    the sandbox returns; then the other validation-adjacent codes, then ``default``.
    """
    return _response_schema(op, ("422", "400", "409", "default"))
