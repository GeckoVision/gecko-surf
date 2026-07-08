"""Runner-side registry fetch: TLS fetch -> local cache -> offline fallback.

The runner prefers the registry when reachable; a network failure degrades to
the cached copy with ``stale=True`` (the runner banner should say so). The
Gecko key travels only here — to OUR registry — never in MCP traffic.
"""

from __future__ import annotations

import json
import os
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gecko.netguard import safe_get, validate_public_url

Transport = Callable[[str, dict[str, str]], tuple[int, str]]

_DEFAULT_CACHE = Path.home() / ".gecko" / "surfaces"


class RegistryFetchError(Exception):
    """Raised when a surface can't be fetched and no cache exists."""


@dataclass(frozen=True)
class FetchedSurface:
    name: str
    surface_rev: str
    tier: str
    spec: dict[str, Any] = field(repr=False)
    stale: bool = False


def _default_transport(url: str, headers: dict[str, str]) -> tuple[int, str]:
    validate_public_url(url)
    try:
        return 200, safe_get(url, headers=headers)
    except urllib.error.HTTPError as exc:
        # A real HTTP status (402 entitlement, 404, ...) is an ANSWER, not an
        # outage — return it so fetch_surface can raise the typed error instead
        # of silently degrading to a stale cache.
        try:
            body = exc.read().decode("utf-8", errors="replace")[:2048]
        except Exception:
            # A truncated error body must not escape as an untyped exception;
            # the status code is the signal.
            body = ""
        return exc.code, body


def _parse_manifest(raw: str, *, stale: bool, name: str) -> FetchedSurface:
    """Parse a wire response or cache file into a ``FetchedSurface``.

    Both paths hand us untrusted (or corruptible-on-disk) text; a malformed
    payload must raise a typed ``RegistryFetchError``, never an unhandled
    ``KeyError``/``TypeError``/``json.JSONDecodeError``.
    """
    try:
        m = json.loads(raw)
        return FetchedSurface(
            name=m["name"],
            surface_rev=m["surface_rev"],
            tier=m["tier"],
            spec=m["spec"],
            stale=stale,
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        if stale:
            raise RegistryFetchError(
                f"corrupt cache for {name!r}; delete it and re-fetch"
            ) from exc
        raise RegistryFetchError("registry returned a malformed manifest") from exc


def fetch_surface(
    registry_url: str,
    name: str,
    *,
    key: str | None = None,
    cache_dir: Path | None = None,
    transport: Transport | None = None,
) -> FetchedSurface:
    cache = (cache_dir or _DEFAULT_CACHE) / f"{name}.json"
    url = registry_url.rstrip("/") + f"/registry/surfaces/{name}"
    headers = {"X-Gecko-Key": key} if key else {}
    send = transport or _default_transport
    try:
        status, body = send(url, headers)
    except OSError as exc:
        if cache.exists():
            return _parse_manifest(cache.read_text("utf-8"), stale=True, name=name)
        raise RegistryFetchError(
            f"registry unreachable and no cached copy of {name!r}"
        ) from exc
    if status != 200:
        detail = ""
        try:
            detail = json.loads(body).get("error", "")
        except (json.JSONDecodeError, AttributeError):
            pass
        raise RegistryFetchError(f"registry returned {status} {detail}".strip())
    surface = _parse_manifest(body, stale=False, name=name)
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a crash mid-write must never leave a half-written file that
    # the NEXT run's cache-fallback path would read as "corrupt cache".
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(body, "utf-8")
    os.replace(tmp, cache)
    return surface
