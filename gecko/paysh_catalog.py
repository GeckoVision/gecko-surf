"""pay.sh catalog comprehension — aggregate pay.sh's Solana-DeFi x402 catalog into
first-call-correct agent surfaces, WITHOUT re-listing or replacing pay.sh.

pay.sh ships NO OpenAPI; its human-shaped catalog advertises endpoints that DRIFT
(CoinGecko's path 404s, Perplexity's host 302s to a dashboard). Gecko comprehends
the surface: it pulls the live catalog for names/prices/free-tier, merges the
CORRECT call shapes where verified, and emits a best-effort tool FLAGGED
``pending`` where it hasn't confirmed the real endpoint (never over-claiming
first-call-correct).

Multi-host by design: each provider advertises a different ``service_url`` host, and
``caller.build_request`` is single-host, so aggregation is a REGISTRY of one pinned
:class:`~gecko.client.AgentApiClient` per provider (base_url = the verified host, which
pins the trust anchor for the auth-injection guard). The aggregated MCP surface lives in
:mod:`gecko.catalog_mcp`.

Freshness (two tiers, both $0 / control-plane-only):
  * Tier 1 — ``refresh``: sha-diff the live catalog; re-comprehend ONLY changed/new
    providers, drop removed ones. Don't snapshot-and-forget.
  * Tier 2 — ``drift_watch``: re-probe each RESOLVED endpoint challenge-only (expect
    402, browser UA, NO payment). A previously-402 endpoint that stops answering 402 is
    flipped to ``broken`` so the agent won't blind-pay a dead endpoint. This verified
    overlay is the point — Gecko stays fresher than pay.sh's own catalog.

Control plane only: we store the surface + tool defs + correctness metadata (sha,
last-verified, status) — NEVER response payloads, user data, or secrets.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlsplit

from .access import public_session
from .caller import PreparedRequest
from .client import AgentApiClient
from .netguard import safe_get, validate_public_url
from .sanitize import sanitize_text
from .surfaces import safe_surface_id

CATALOG_URL = "https://pay.sh/api/catalog"

# A challenge-only probe fires with a browser UA (the reverse-engineering showed the x402
# challenge only surfaces to a browser-shaped client) and NO payment — it reads the 402.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Verification state of a provider's comprehended call shape. ``verified`` = a live 402 was
# confirmed; ``pending`` = best-effort from catalog metadata, unconfirmed; ``broken`` = a
# once-verified endpoint stopped answering 402 (drift) — do NOT blind-pay it.
Status = Literal["verified", "pending", "broken"]

# A drift probe: first-call-correct request -> HTTP status (402 = live paywall), or None.
ProbeFn = Callable[[PreparedRequest], int | None]
# A catalog fetcher: url -> raw JSON text. Injectable so tests never hit the network.
Fetcher = Callable[[str], str]


class CatalogError(Exception):
    """Raised when the pay.sh catalog can't be fetched or parsed."""


@dataclass(frozen=True)
class CatalogEntry:
    """One pay.sh provider's control-plane metadata (never a response payload)."""

    fqn: str
    title: str
    service_url: str
    description: str
    use_case: str
    category: str
    endpoint_count: int
    min_price_usd: float
    max_price_usd: float
    has_free_tier: bool
    has_metering: bool
    sha: str  # UNIQUE per-provider content hash — the freshness/cache key


@dataclass(frozen=True)
class VerifiedShape:
    """A reverse-engineered, live-verified first-call-correct call shape for one provider.

    ``host`` is the CORRECT host (may differ from the drifted catalog ``service_url``) and
    pins the trust anchor. ``probe_args`` are the args that build the first-call-correct
    request the drift-watch re-probes challenge-only.
    """

    fqn: str
    host: str
    op_id: str
    method: str
    path: str
    summary: str
    advertised: str
    verified_ts: str
    params: tuple[dict[str, Any], ...] = ()
    request_body: dict[str, Any] | None = None
    probe_args: dict[str, Any] = field(default_factory=dict)


# The verified subset (live 402 confirmed 2026-07-11, challenge-only). Everything else is
# comprehended best-effort and flagged ``pending`` — Gecko flags, it does NOT guess.
VERIFIED: dict[str, VerifiedShape] = {
    "paysponge/coingecko": VerifiedShape(
        fqn="paysponge/coingecko",
        host="https://pro-api.coingecko.com",
        op_id="searchOnchainPools",
        method="GET",
        path="/api/v3/x402/onchain/search/pools",
        summary="Search onchain DEX pools by token or protocol name (x402 pay-per-call).",
        advertised="GET /api/v3/x402/onchain -> 404 (catalog templates a :solana_address "
        "placeholder on a path that 404s)",
        verified_ts="2026-07-11",
        params=(
            {
                "name": "query",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
                "description": "Token symbol or protocol name, e.g. 'jupiter'.",
            },
            {
                "name": "network",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "default": "solana"},
                "description": "Chain to search; pinned to solana for this vertical.",
            },
        ),
        probe_args={"query": "jupiter", "network": "solana"},
    ),
    "paysponge/perplexity": VerifiedShape(
        fqn="paysponge/perplexity",
        host="https://pplx.x402.paysponge.com",
        op_id="sonarQuery",
        method="POST",
        path="/v1/sonar",
        summary="Grounded web answer with citations (x402 pay-per-call).",
        advertised="GET / -> 302 (bare host redirects to an HTML dashboard, not x402)",
        verified_ts="2026-07-11",
        request_body={
            "type": "object",
            "required": ["model", "messages"],
            "properties": {
                "model": {"type": "string", "default": "sonar"},
                "messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                },
                "max_tokens": {"type": "integer", "default": 1500},
            },
        },
        probe_args={
            "body": {
                "model": "sonar",
                "messages": [{"role": "user", "content": "what is the jupiter dex"}],
                "max_tokens": 64,
            }
        },
    ),
}


@dataclass(frozen=True)
class ProviderSurface:
    """One provider comprehended into a single pinned client + correctness metadata."""

    entry: CatalogEntry
    client: AgentApiClient
    tool_name: str
    host: str
    status: Status
    advertised: str | None = None
    last_verified_ts: int | None = None
    last_probe_status: int | None = None


@dataclass(frozen=True)
class RefreshDiff:
    """The result of a Tier-1 sha-diff refresh."""

    added: list[str]
    changed: list[str]
    removed: list[str]
    unchanged: list[str]


@dataclass(frozen=True)
class DriftResult:
    """The result of one Tier-2 challenge-only re-probe."""

    fqn: str
    tool_name: str
    status: Status
    probe_status: int | None
    changed: bool


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _is_http_url(url: str) -> bool:
    """A shape-only guard (no DNS) so parsing untrusted entries stays network-free; the
    full SSRF check (``validate_public_url``) runs at actual fetch/probe time."""
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _entry_from(raw: Any) -> CatalogEntry | None:
    """Parse ONE untrusted catalog item defensively; drop anything malformed."""
    if not isinstance(raw, dict):
        return None
    fqn = _str(raw.get("fqn"))
    service_url = _str(raw.get("service_url"))
    if not fqn or not _is_http_url(service_url):
        return None
    return CatalogEntry(
        fqn=fqn[:128],
        title=_str(raw.get("title"))[:200] or fqn,
        service_url=service_url,
        description=_str(raw.get("description"))[:2000],
        use_case=_str(raw.get("use_case"))[:2000],
        category=_str(raw.get("category"))[:64],
        endpoint_count=_int(raw.get("endpoint_count")),
        min_price_usd=_float(raw.get("min_price_usd")),
        max_price_usd=_float(raw.get("max_price_usd")),
        has_free_tier=bool(raw.get("has_free_tier")),
        has_metering=bool(raw.get("has_metering")),
        sha=_str(raw.get("sha"))[:64],
    )


def _safe_fetch(url: str) -> str:
    validate_public_url(url)
    return safe_get(url, headers={"User-Agent": _BROWSER_UA})


def fetch_catalog(
    url: str = CATALOG_URL, *, fetcher: Fetcher | None = None
) -> list[CatalogEntry]:
    """Fetch + parse the live pay.sh catalog into entries (untrusted input; SSRF-safe).

    ``fetcher`` is injectable (a fake catalog string) so unit tests never touch the network.
    """
    raw = fetcher(url) if fetcher is not None else _safe_fetch(url)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CatalogError("pay.sh catalog is not valid JSON") from exc
    items = data.get("providers") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise CatalogError("pay.sh catalog has no provider list")
    out: list[CatalogEntry] = []
    seen: set[str] = set()
    for item in items:
        entry = _entry_from(item)
        if entry is not None and entry.fqn not in seen:
            seen.add(entry.fqn)
            out.append(entry)
    return out


def _price_note(entry: CatalogEntry) -> str:
    lo, hi = entry.min_price_usd, entry.max_price_usd
    return f"${lo}" if lo == hi else f"${lo}-${hi}"


def _x402_responses(entry: CatalogEntry) -> dict[str, Any]:
    return {
        "402": {
            "description": (
                f"Payment Required — x402 challenge (pay-per-call, {_price_note(entry)}). "
                "The x402 rail settles the payment directly; Gecko never handles the wallet."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "error": {"type": "string"},
                            "message": {"type": "string"},
                        },
                    }
                }
            },
        },
        "200": {"description": "OK — resource payload after settlement."},
    }


def _verified_spec(
    slug: str, entry: CatalogEntry, shape: VerifiedShape
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "operationId": slug,
        "summary": shape.summary,
        "tags": [entry.title],
        "x-gecko-comprehension": f"verified first-call-correct (402 live {shape.verified_ts})",
        "x-gecko-advertised": shape.advertised,
        "responses": _x402_responses(entry),
    }
    if shape.params:
        op["parameters"] = [dict(p) for p in shape.params]
    if shape.request_body is not None:
        op["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": shape.request_body}},
        }
    return {
        "openapi": "3.0.1",
        "info": {"title": entry.title, "version": shape.verified_ts},
        "servers": [{"url": shape.host}],
        "paths": {shape.path: {shape.method.lower(): op}},
    }


def _pending_spec(slug: str, entry: CatalogEntry) -> dict[str, Any]:
    op: dict[str, Any] = {
        "operationId": slug,
        "summary": (
            f"{entry.title}: {entry.category or 'x402'} capability, pay-per-call. "
            "Endpoint shape not yet verified by Gecko — confirm the call before paying."
        ),
        "tags": [entry.title],
        "x-gecko-comprehension": "pending verification",
        "responses": _x402_responses(entry),
    }
    return {
        "openapi": "3.0.1",
        "info": {"title": entry.title, "version": "0"},
        "servers": [{"url": entry.service_url}],
        "paths": {"/": {"get": op}},
    }


def _blurb(entry: CatalogEntry) -> str:
    """The lexical situating text (sanitized — catalog content is UNTRUSTED). Folded into
    the client's search haystack ONLY; never shown as a tool def, so it can't poison the
    surface or leak into a request."""
    text = f"{entry.title} {entry.description} {entry.use_case} {entry.category}"
    clean, _poisoned = sanitize_text(text[:2400])
    return clean if isinstance(clean, str) else ""


def _comprehend(
    entry: CatalogEntry, verified: dict[str, VerifiedShape]
) -> ProviderSurface:
    """Comprehend ONE provider into a pinned single-host client + correctness metadata."""
    slug = safe_surface_id(entry.fqn)
    shape = verified.get(entry.fqn)
    if shape is not None:
        host, spec = shape.host, _verified_spec(slug, entry, shape)
        status: Status = "verified"
        advertised: str | None = shape.advertised
        last_verified: int | None = _epoch_ms(shape.verified_ts)
    else:
        host, spec = entry.service_url, _pending_spec(slug, entry)
        status, advertised, last_verified = "pending", None, None
    client = AgentApiClient(
        spec,
        base_url=host,  # pins the trust anchor to this host (the auth-injection guard)
        session=public_session(),
        surface_id=slug,
        blurbs={slug: _blurb(entry)},
    )
    return ProviderSurface(
        entry=entry,
        client=client,
        tool_name=slug,
        host=host,
        status=status,
        advertised=advertised,
        last_verified_ts=last_verified,
    )


def _epoch_ms(day: str) -> int | None:
    try:
        return int(time.mktime(time.strptime(day, "%Y-%m-%d")) * 1000)
    except (ValueError, OverflowError):
        return None


class CatalogRegistry:
    """In-memory control-plane store of comprehended providers (one client per host).

    Not a public catalog: there is no browsable "list every provider" agent route — this
    aggregates pay.sh for the operator who provisioned it, it never re-publishes it.
    """

    def __init__(self) -> None:
        self._providers: dict[str, ProviderSurface] = {}
        self._by_tool: dict[str, ProviderSurface] = {}

    @classmethod
    def build(
        cls,
        entries: list[CatalogEntry],
        *,
        verified: dict[str, VerifiedShape] | None = None,
    ) -> CatalogRegistry:
        reg = cls()
        table = VERIFIED if verified is None else verified
        for entry in entries:
            reg._install(_comprehend(entry, table))
        return reg

    def _install(self, ps: ProviderSurface) -> None:
        self._providers[ps.entry.fqn] = ps
        self._by_tool[ps.tool_name] = ps

    def _drop(self, fqn: str) -> None:
        ps = self._providers.pop(fqn, None)
        if ps is not None:
            self._by_tool.pop(ps.tool_name, None)

    def providers(self) -> list[ProviderSurface]:
        return list(self._providers.values())

    def get(self, fqn: str) -> ProviderSurface | None:
        return self._providers.get(fqn)

    def by_tool(self, tool_name: str) -> ProviderSurface | None:
        return self._by_tool.get(tool_name)

    def counts(self) -> dict[str, int]:
        c = {"verified": 0, "pending": 0, "broken": 0}
        for ps in self._providers.values():
            c[ps.status] += 1
        return c

    def refresh(
        self,
        entries: list[CatalogEntry],
        *,
        verified: dict[str, VerifiedShape] | None = None,
    ) -> RefreshDiff:
        """Tier-1 sha-diff: re-comprehend ONLY new/changed providers; drop removed ones.

        A provider whose ``sha`` is unchanged keeps its exact same comprehended client
        (identity preserved) — we don't snapshot-and-forget, and we don't needlessly
        re-comprehend the whole catalog on every poll.
        """
        table = VERIFIED if verified is None else verified
        new_by_fqn = {e.fqn: e for e in entries}
        added: list[str] = []
        changed: list[str] = []
        removed: list[str] = []
        unchanged: list[str] = []
        for fqn, entry in new_by_fqn.items():
            cur = self._providers.get(fqn)
            if cur is None:
                self._install(_comprehend(entry, table))
                added.append(fqn)
            elif cur.entry.sha != entry.sha:
                self._install(_comprehend(entry, table))
                changed.append(fqn)
            else:
                unchanged.append(fqn)
        for fqn in [f for f in self._providers if f not in new_by_fqn]:
            self._drop(fqn)
            removed.append(fqn)
        return RefreshDiff(
            sorted(added), sorted(changed), sorted(removed), sorted(unchanged)
        )

    def drift_watch(
        self,
        probe: ProbeFn,
        *,
        now: Callable[[], float] | None = None,
        verified: dict[str, VerifiedShape] | None = None,
    ) -> list[DriftResult]:
        """Tier-2 truth signal ($0): re-probe each RESOLVED endpoint challenge-only.

        Only ``verified``/``broken`` providers are probed (a ``pending`` one was never
        resolved, so there is nothing to keep honest). A 402 keeps/moves it to ``verified``
        and refreshes ``last_verified_ts``; ANY other status (or a probe failure) flips it to
        ``broken`` so the aggregated surface won't offer it as first-call-correct and the
        agent won't blind-pay a dead endpoint. A broken endpoint that answers 402 again
        recovers to ``verified``.
        """
        table = VERIFIED if verified is None else verified
        now_fn = now if now is not None else time.time
        results: list[DriftResult] = []
        for ps in list(self._providers.values()):
            if ps.status == "pending":
                continue
            shape = table.get(ps.entry.fqn)
            args = dict(shape.probe_args) if shape is not None else {}
            try:
                req = ps.client.prepare(ps.tool_name, args)
                code = probe(req)
            except Exception:  # noqa: BLE001 - a prepare/probe failure is a broken signal
                code = None
            new_status: Status = "verified" if code == 402 else "broken"
            changed = new_status != ps.status
            self._install(
                replace(
                    ps,
                    status=new_status,
                    last_probe_status=code,
                    last_verified_ts=(
                        int(now_fn() * 1000) if code == 402 else ps.last_verified_ts
                    ),
                )
            )
            results.append(
                DriftResult(ps.entry.fqn, ps.tool_name, new_status, code, changed)
            )
        return results


def challenge_probe(
    req: PreparedRequest, *, timeout: int = 15
) -> int | None:  # pragma: no cover - live network probe; falsified offline via ProbeFn
    """The default $0 drift probe: fire the first-call-correct request WITHOUT payment and
    return the HTTP status (402 = live paywall). Browser UA. Reads the status only — never
    the body (control plane). SSRF-guarded before the request."""
    validate_public_url(req.url)
    data = (
        json.dumps(req.json_body).encode("utf-8") if req.json_body is not None else None
    )
    headers = dict(req.headers)
    headers["User-Agent"] = _BROWSER_UA
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        req.url, data=data, headers=headers, method=req.method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return int(getattr(resp, "status", 0))
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:  # noqa: BLE001 - a network failure is an unknown -> treat as broken
        return None


__all__ = [
    "CATALOG_URL",
    "CatalogEntry",
    "CatalogError",
    "CatalogRegistry",
    "DriftResult",
    "ProbeFn",
    "ProviderSurface",
    "RefreshDiff",
    "Status",
    "VERIFIED",
    "VerifiedShape",
    "challenge_probe",
    "fetch_catalog",
]
