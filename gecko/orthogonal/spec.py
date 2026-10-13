"""Orthogonal as DATA: a synthesized three-operation spec, the auth pin, the allowlist.

Why synthesized: Orthogonal's published spec declares no security scheme and lists the
payment rails (``x402.``/``mpp.``/``np.orthogonal.com``) as its servers, while the key
goes to ``api.orthogonal.com``. Trusting that spec would either send no auth or send the
key to the wrong host. So the three operations this job needs are written here, with a
bearer scheme, and run through the ordinary engine (``ingest`` -> ``tools`` ->
``caller``) — the engine itself learns nothing about Orthogonal.

Why an allowlist: one Orthogonal key opens every one of its ~900 paths (email send,
paid enrichment). Orthogonal has no scoped keys and no spend limits, so the only scope
this key has is the one enforced here, before a request is built.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..ingest import extract_operations
from ..tools import build_tools
from .errors import NotAllowedError

BASE_URL = "https://api.orthogonal.com"
#: Out-of-band trust anchor: the ONLY host the key may be sent to. Never read from a spec.
AUTH_HOSTS: frozenset[str] = frozenset({"api.orthogonal.com"})
#: The x402 host (pay per call, no key). A trust anchor: never read from a spec, a
#: catalog or a reply. Its routes are ``{host}/{api}{path}`` (measured 9 Oct).
X402_HOST = "https://x402.orthogonal.com"

RUN_OP = "orthogonal_run"
DETAILS_OP = "orthogonal_details"
BALANCE_OP = "orthogonal_balance"

# One DNS label, then at least one more: no slash, no "..", lowercase only. A path
# placeholder is only ever filled with a domain the job has already normalized.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
#: A query or path value may be a short string or an int, never an object.
MAX_VALUE_LEN = 300


@dataclass(frozen=True)
class Target:
    """One upstream call this key may make, with the only inputs it may carry.

    ``path`` is the template ``/v1/details`` names; a ``/v1/run`` call must name the
    same template with each ``{placeholder}`` filled by a domain.
    """

    api: str
    path: str
    #: Price read from ``/v1/details`` on 2026-10-08, in whole cents rounded UP. A live
    #: quote that comes back higher wins; this is the floor the cap reasons with.
    pinned_cents: int
    query_keys: frozenset[str] = frozenset()
    body_keys: frozenset[str] = frozenset()
    #: True when later pages may ride on ``parentRequestId`` for free.
    paginates: bool = False

    @property
    def label(self) -> str:
        return f"{self.api} {self.path}"

    def fill(self, **values: str) -> str:
        """The concrete path; refuses any value that is not a normalized domain."""
        path = self.path
        for name in _PLACEHOLDER.findall(self.path):
            value = values.get(name, "")
            if not DOMAIN_RE.match(value):
                raise NotAllowedError(f"{name} must be a plain domain like example.com")
            path = path.replace("{" + name + "}", value)
        return path

    def matches(self, path: str) -> bool:
        if path == self.path:
            return True
        pattern = re.escape(self.path)
        for name in _PLACEHOLDER.findall(self.path):
            pattern = pattern.replace(
                re.escape("{" + name + "}"), f"{_LABEL}(?:\\.{_LABEL})+"
            )
        return re.fullmatch(pattern, path) is not None


COMMENTS_API = "scrapecreators"
COMMENTS_PATH = "/v2/instagram/post/comments"
COMMENTS = Target(
    COMMENTS_API,
    COMMENTS_PATH,
    2,
    query_keys=frozenset({"url", "cursor"}),
    paginates=True,
)
#: Kept as a name: the commenter job and its tests read it.
PINNED_PRICE_CENTS = COMMENTS.pinned_cents

# --- the supplier watch, plus the failover sources ---------------------------------
CRUST_IDENTIFY = Target(
    "crustdata",
    "/company/identify",
    0,
    body_keys=frozenset({"names", "domains", "fields", "exact_match"}),
)
PL_COMPANY = Target("predictleads", "/v3/companies/{id_or_domain}", 4)
PL_NEWS = Target(
    "predictleads",
    "/v3/companies/{company_id_or_domain}/news_events",
    4,
    query_keys=frozenset({"found_at_from", "found_at_until", "limit", "page"}),
    paginates=True,
)
PL_WEBSITE = Target(
    "predictleads",
    "/v3/companies/{company_id_or_domain}/website_evolution",
    4,
    query_keys=frozenset(
        {"first_seen_at_from", "first_seen_at_until", "limit", "page"}
    ),
    paginates=True,
)
AKTA_HIERARCHY = Target(
    "akta-pro", "/v1/company/enrichment/company_hierarchy/{company}", 3
)
AKTA_TRUST = Target("akta-pro", "/v1/company/enrichment/trust_signal/{company}", 3)
#: Listed at $0.025: 2.5 cents, counted as 3 so the cap never under-counts.
FD_NEWS = Target(
    "financial-datasets", "/news", 3, query_keys=frozenset({"ticker", "limit"})
)

#: Listed at $0.042 (``/v1/details``, read from the local copy 2026-10-08): 5 cents.
#: Reached only when a field fails over to it (``supplier/sources.py``).
CORESIGNAL_CLEAN = Target(
    "coresignal", "/v2/company_clean/enrich", 5, query_keys=frozenset({"website"})
)

SUPPLIER_TARGETS: tuple[Target, ...] = (
    CRUST_IDENTIFY,
    PL_COMPANY,
    CORESIGNAL_CLEAN,
    PL_NEWS,
    PL_WEBSITE,
    AKTA_HIERARCHY,
    AKTA_TRUST,
    FD_NEWS,
)
# --- the living file per tokenized stock (``gecko/livingfile``) ---------------------
#: The real share price. Listed at $0.025 (``/v1/details``, local copy 2026-10-08), so
#: counted as 3 cents. ``responseParams`` is empty there: the reply shape is unconfirmed.
FD_PRICE_SNAPSHOT = Target(
    "financial-datasets", "/prices/snapshot", 3, query_keys=frozenset({"ticker"})
)
XSTOCK_TARGETS: tuple[Target, ...] = (FD_PRICE_SNAPSHOT,)

#: Every upstream call the key may make. Nothing else is reachable through this module.
TARGETS: tuple[Target, ...] = (COMMENTS, *SUPPLIER_TARGETS, *XSTOCK_TARGETS)
#: (upstream api, path template) pairs, for anyone who wants the plain list.
ALLOWED_TARGETS: frozenset[tuple[str, str]] = frozenset(
    (t.api, t.path) for t in TARGETS
)


def target_for(
    api: Any, path: Any, targets: tuple[Target, ...] = TARGETS
) -> Target | None:
    if not isinstance(api, str) or not isinstance(path, str):
        return None
    for target in targets:
        if target.api == api and target.matches(path):
            return target
    return None


_TARGET_BODY = {
    "type": "object",
    "required": ["api", "path"],
    "properties": {
        "api": {"type": "string"},
        "path": {"type": "string"},
        "query": {"type": "object"},
        "body": {"type": "object"},
        "parentRequestId": {"type": "string"},
    },
}


def _post(op_id: str, summary: str) -> dict[str, Any]:
    return {
        "operationId": op_id,
        "summary": summary,
        "security": [{"bearer": []}],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _TARGET_BODY}},
        },
        "responses": {"200": {"description": "ok"}},
    }


SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Orthogonal (lists subset)", "version": "2026-10-08"},
    "servers": [{"url": BASE_URL}],
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    "paths": {
        "/v1/run": {"post": _post(RUN_OP, "Run one upstream API call")},
        "/v1/details": {"post": _post(DETAILS_OP, "Price and parameters of a call")},
        "/v1/credits/balance": {
            "get": {
                "operationId": BALANCE_OP,
                "summary": "Credit balance",
                "security": [{"bearer": []}],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def tools_by_name() -> dict[str, dict[str, Any]]:
    """The engine's tool defs for the three operations, keyed by operationId."""
    tools = build_tools(extract_operations(SPEC))
    return {t["name"]: t for t in tools}


def _scalar_ok(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and len(value) <= MAX_VALUE_LEN


def check_allowed(
    op: str, body: dict[str, Any] | None, targets: tuple[Target, ...] = TARGETS
) -> None:
    """Raise ``NotAllowedError`` unless ``op`` + ``body`` stay inside the allowlist.

    ``targets`` is the allowlist. The fixed jobs use :data:`TARGETS`; an any-query run
    passes exactly the targets of its validated plan, and nothing else, so one plan's
    key scope never includes another job's paths.
    """
    if op == BALANCE_OP:
        if body:
            raise NotAllowedError("the balance call takes no body")
        return
    if op not in (RUN_OP, DETAILS_OP):
        raise NotAllowedError(f"operation {op!r} is not allowed for this key")
    if not isinstance(body, dict):
        raise NotAllowedError("a run/details call needs a body")
    api, path = body.get("api"), body.get("path")
    target = target_for(api, path, targets)
    if target is None:
        raise NotAllowedError(
            f"{api!r} {path!r} is not allowed: this key may only read "
            + "; ".join(t.label for t in targets)
        )
    if op == DETAILS_OP:
        if set(body) != {"api", "path"} or path != target.path:
            raise NotAllowedError("details takes only api and the path template")
        return
    if path == target.path and "{" in path:
        raise NotAllowedError("fill the path placeholder before running the call")
    extra = set(body) - {"api", "path", "query", "body", "parentRequestId"}
    if extra:
        raise NotAllowedError(f"unexpected body field(s): {', '.join(sorted(extra))}")
    if "parentRequestId" in body and not target.paginates:
        raise NotAllowedError(f"{target.label} does not paginate")
    query = body.get("query") or {}
    if not isinstance(query, dict):
        raise NotAllowedError("query must be an object")
    bad = set(query) - target.query_keys
    if bad:
        raise NotAllowedError(f"query key(s) not allowed: {', '.join(sorted(bad))}")
    if not all(_scalar_ok(v) for v in query.values()):
        raise NotAllowedError("query values must be short strings or integers")
    upstream_body = body.get("body")
    if upstream_body is not None:
        if not target.body_keys or not isinstance(upstream_body, dict):
            raise NotAllowedError(f"{target.label} takes no request body")
        bad = set(upstream_body) - target.body_keys
        if bad:
            raise NotAllowedError(f"body key(s) not allowed: {', '.join(sorted(bad))}")
