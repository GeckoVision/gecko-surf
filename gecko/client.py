"""AgentApiClient — the one object that makes an API agent-usable.

Ties the layers together: ingest -> catalog (find) -> tools (comprehend) ->
caller (correct request) -> access (auth) -> response. Two modes:
  - "recorded": synthesize the response from the spec (no network, no spend) — for demos/CI.
  - "live": actually call the upstream API with the session's auth.

Security seam (Priority 1/2): auth is only ever injected toward a host on the surface's
OUT-OF-BAND trust anchor (``surfaces.anchor_for``), never toward the spec's own (poison-
able) ``servers[]``. A quarantined/unverified surface fails closed — it degrades to
recorded/no-auth rather than leaking the customer's secret.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import corpus
from .access import AuthSession, stub_session
from .caller import CallError, PreparedRequest, build_request, execute
from .catalog import Catalog
from .fusion import RRF_K, rrf_fuse
from .ingest import Operation, extract_operations, load_spec
from .sample import example_from_schema
from .sanitize import sanitize_schema
from .surfaces import _host_of, anchor_for, spec_is_quarantined, surface_rev, tools_rev
from .tools import auth_location_is_safe, build_tools, to_tool

if TYPE_CHECKING:
    from .dense import DenseIndex

logger = logging.getLogger("gecko.client")


class IntegrityError(Exception):
    """Raised when the shipped tool set no longer matches the pinned spec (tamper)."""


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
    ):
        """Make an API agent-usable from its OpenAPI spec.

        Live mode targets ``servers[0].url`` from the spec unless an explicit
        ``base_url`` is given. This is a money-API footgun: if the spec lists a
        production server first, a live call hits production — pass the sandbox
        server's URL explicitly for live tests. An explicit ``base_url`` also pins
        the trust anchor to that one host (see ``self.anchor``).

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
            sanitize_schema(self._success_schema(op), route_to_arg=False)[1]
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
        # An empty auth-header dict means the session can't satisfy auth-gated ops,
        # so we hide them from the agent (it would only mis-call them). A session
        # WITH auth (e.g. TxODDS) surfaces everything, unchanged.
        self._session_has_auth = bool(self.session.auth_headers())
        self._usable_tool_names = {
            t["name"]
            for t in self.tools
            if self._session_has_auth or not t.get("requires_auth")
        }

        # Corpus capture context (opt-in). Metadata only; never the response body.
        self._corpus_path = corpus_path
        self.surface_rev = surface_rev(self.spec)
        self.surface_id = surface_id or _host_of(self.base_url) or "surface"

    def search_scored(self, query: str, limit: int = 5) -> list[ScoredHit]:
        """Like ``search`` but carries ``score``/``is_fallback`` (retrieval eval + the
        out-of-scope confidence floor). Applies the SAME auth filter and over-fetch, so
        ``search`` is a pure projection of this — the two can never disagree on ranking."""
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

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return [
            {"name": h.name, "summary": h.summary, "path": h.path, "method": h.method}
            for h in self.search_scored(query, limit)
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

    def prepare(self, tool_name: str, args: dict[str, Any]) -> PreparedRequest:
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
        inject_auth = (
            self.session.auth_headers() if self._may_inject_auth_for(op) else None
        )
        return build_request(
            tool,
            args,
            self.base_url,
            inject_auth,
            allowed_auth_hosts=self._auth_allowed_hosts,
        )

    def _effective_mode(self, tool_name: str, mode: str) -> str:
        """Degrade live -> recorded when the surface can't be safely called live: a
        quarantined (poisoned-until-proven) surface, or one whose auth-expecting call
        can't inject its secret (would otherwise fire un-authenticated to an unpinned host)."""
        if mode != "live":
            return mode
        if self.anchor.state == "quarantined":
            return "recorded"
        op = self._op_by_name[tool_name]
        if self._session_has_auth and not self._may_inject_auth_for(op):
            return "recorded"
        return mode

    def call(
        self, tool_name: str, args: dict[str, Any], mode: str = "recorded"
    ) -> dict[str, Any]:
        effective = self._effective_mode(tool_name, mode)
        start = time.perf_counter()
        try:
            req = self.prepare(tool_name, args)
        except CallError as exc:
            # A pre-flight failure (missing param / auth-host refusal) is itself a
            # first-call outcome worth capturing; record it, then propagate unchanged.
            self._capture(tool_name, None, exc, args, None, effective)
            raise
        if effective == "live":
            status, body = execute(req)
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
        schema = self._success_schema(self._op_by_name[tool_name])
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
                error_class=corpus.error_class_for(status, exc),
                latency_ms=latency_ms,
                mode=mode,
                auth_injected=bool(op is not None and self._may_inject_auth_for(op)),
                ts=int(time.time() * 1000),
                surface_id=self.surface_id,
                surface_rev=self.surface_rev,
            ),
            self._corpus_path,
        )

    @staticmethod
    def _success_schema(op) -> dict[str, Any]:
        for code in ("200", "201", "default"):
            r = op.responses.get(code)
            if not isinstance(r, dict):
                continue
            content = r.get("content", {}) or {}
            media = content.get("application/json") or next(
                iter(content.values()), None
            )
            if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                return media["schema"]
        return {}
