"""Surfaces a partner control plane says to mount — fetched at boot, or not at all.

WHY THIS EXISTS. The built-in surface list is a literal in ``serve_mcp``, so adding a
provider means an image rebuild. Provider self-service needs a provider who signed up at
22:00 to be mountable at the next restart, which means the list has to come from somewhere
the app can write. It reads a control plane that owns provider records; this module is the
reading half, and it holds every rule that makes reading a REMOTE list safe to serve
PUBLICLY.

THREE PROPERTIES, and each exists because of a specific way this could go wrong.

**Fail soft.** Any failure — unreachable, slow, 401, malformed, wrong shape — yields an
EMPTY list and a logged warning. It never raises. A host that will not boot because a
sign-up service hiccuped is a far worse outage than a provider whose surface appears one
restart late: the second costs one provider a delay, the first takes every existing mount
offline. Same stance as ``build_keystore_from_env``, which disables issuance rather than
killing the process.

**Re-validate, do not trust the flag.** The control plane promises to send only
non-quarantined surfaces. We check anyway, with the same ``spec_is_quarantined`` the
ingest path uses. Not distrust — defence in depth. These specs are SERVED PUBLICLY and
UNAUTHENTICATED, so a bug in somebody else's status column must not be able to put a
poisoned surface on the internet under our hostname.

**Built-in names win, always.** A remote row naming an existing surface is REFUSED, never
merged and never allowed to override. Shadowing ``jito`` or ``orquestra`` would let a
control-plane bug — or anyone who reached it — replace a live surface's tools with their
own, which is the whole attack in one line. The built-in list is the authority on its own
names.

Everything arriving here is UNTRUSTED INPUT and is treated like ingested spec content:
counts capped, sizes capped, names normalized through ``safe_surface_id``, duplicates
dropped, every field type-checked before use.

Control plane only: names and API surfaces, never payloads or secrets.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable

from .netguard import UnsafeUrlError, safe_get
from .surfaces import SurfaceError, safe_surface_id, spec_is_quarantined

logger = logging.getLogger("gecko.provider_sync")

__all__ = [
    "PROVIDER_SYNC_TOKEN_ENV",
    "PROVIDER_SYNC_URL_ENV",
    "fetch_provider_surfaces",
]

#: Where the provider control plane serves its active surface list.
PROVIDER_SYNC_URL_ENV = "GECKO_PROVIDER_SYNC_URL"
#: Shared secret sent as ``X-Provider-Host-Token``. Unset => no fetch is attempted.
PROVIDER_SYNC_TOKEN_ENV = "GECKO_PROVIDER_SYNC_TOKEN"
PROVIDER_SYNC_HEADER = "X-Provider-Host-Token"

#: The SSM boot sentinel (infra/push-ssm-params.sh) — a literal in a PUBLIC repo, so it is
#: read as "unset" rather than as a value, exactly as every other consumer does.
_UNSET_SENTINEL = "__unset__"

#: Bounds on an untrusted response. Generous enough for a real catalogue, small enough that
#: a hostile or broken control plane cannot exhaust this process at boot.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SURFACES = 200
MAX_SPEC_BYTES = 2 * 1024 * 1024
#: Boot must not hang on a slow dependency; a late surface is cheaper than a late host.
FETCH_TIMEOUT_SECONDS = 10


def _configured() -> tuple[str, str] | None:
    url = os.environ.get(PROVIDER_SYNC_URL_ENV, "").strip()
    token = os.environ.get(PROVIDER_SYNC_TOKEN_ENV, "").strip()
    if not url or url == _UNSET_SENTINEL:
        return None
    if not token or token == _UNSET_SENTINEL:
        # A URL without a token is a misconfiguration, not a request to fetch anonymously:
        # the endpoint is gated, so an unauthenticated call would 401 every boot.
        logger.warning(
            "%s is set but %s is not — skipping provider sync",
            PROVIDER_SYNC_URL_ENV,
            PROVIDER_SYNC_TOKEN_ENV,
        )
        return None
    return url, token


def _rows(payload: Any) -> Iterable[Any]:
    """Accept ``{"surfaces": [...]}`` or a bare list, and nothing else."""
    if isinstance(payload, dict):
        rows = payload.get("surfaces")
        return rows if isinstance(rows, list) else ()
    return payload if isinstance(payload, list) else ()


def _accept(
    row: Any, *, reserved: frozenset[str], seen: set[str]
) -> tuple[str, dict[str, Any]] | None:
    """One row -> ``(slug, spec)``, or None with the reason logged.

    Every rejection is logged by NAME and reason. A surface silently missing from a host
    is the hardest kind of problem to diagnose from the outside — the provider sees a 404
    and has no way to learn why.
    """
    if not isinstance(row, dict):
        logger.warning("provider sync: skipping a row that is not an object")
        return None
    raw_name = row.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        logger.warning("provider sync: skipping a row with no usable name")
        return None
    if row.get("status") != "active":
        logger.info("provider sync: %r is not active — skipping", raw_name)
        return None
    try:
        name = safe_surface_id(raw_name)
    except SurfaceError:
        logger.warning("provider sync: %r does not normalize to a slug", raw_name)
        return None
    if name in reserved:
        # The built-in list owns its names. Overriding one would swap a live surface's
        # tools for somebody else's under the same URL.
        logger.error(
            "provider sync: REFUSING %r — it collides with a built-in surface", name
        )
        return None
    if name in seen:
        logger.warning("provider sync: duplicate name %r — keeping the first", name)
        return None
    spec = row.get("spec")
    if not isinstance(spec, dict) or not spec:
        logger.warning("provider sync: %r carries no spec object — skipping", name)
        return None
    if len(json.dumps(spec, default=str)) > MAX_SPEC_BYTES:
        logger.warning("provider sync: %r exceeds the spec size cap — skipping", name)
        return None
    if spec_is_quarantined(spec):
        # The control plane says it only sends clean surfaces. We check anyway — this
        # mount is public and unauthenticated.
        logger.error(
            "provider sync: REFUSING %r — the spec is quarantined on OUR check, "
            "whatever its status said",
            name,
        )
        return None
    seen.add(name)
    return name, spec


def fetch_provider_surfaces(
    *, reserved_names: Iterable[str] = ()
) -> list[tuple[str, dict[str, Any]]]:
    """``[(slug, spec)]`` the control plane says to mount. NEVER raises.

    Returns an empty list when unconfigured or on any failure — see the module docstring
    on why boot must survive a broken dependency.
    """
    configured = _configured()
    if configured is None:
        return []
    url, token = configured
    reserved = frozenset(reserved_names)

    try:
        body = safe_get(
            url,
            max_bytes=MAX_RESPONSE_BYTES,
            timeout=FETCH_TIMEOUT_SECONDS,
            headers={PROVIDER_SYNC_HEADER: token},
        )
    except UnsafeUrlError as exc:
        logger.error("provider sync: refusing an unsafe sync URL: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001 - boot must survive ANY transport failure
        logger.warning("provider sync: fetch failed (%s) — serving built-ins only", exc)
        return []

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("provider sync: response is not JSON (%s) — ignoring", exc)
        return []

    rows = list(_rows(payload))
    if len(rows) > MAX_SURFACES:
        logger.warning(
            "provider sync: %d rows exceeds the cap of %d — taking the first %d",
            len(rows),
            MAX_SURFACES,
            MAX_SURFACES,
        )
        rows = rows[:MAX_SURFACES]

    seen: set[str] = set()
    accepted: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        entry = _accept(row, reserved=reserved, seen=seen)
        if entry is not None:
            accepted.append(entry)

    # Say what was dropped. A count that silently shrinks reads as "they sent fewer".
    if len(accepted) != len(rows):
        logger.warning(
            "provider sync: mounting %d of %d offered surfaces",
            len(accepted),
            len(rows),
        )
    else:
        logger.info("provider sync: mounting %d surface(s)", len(accepted))
    return accepted
