"""Runner-side registry fetch: TLS fetch -> local cache -> offline fallback.

The runner prefers the registry when reachable; a network failure degrades to
the cached copy with ``stale=True`` (the runner banner should say so). The
Gecko key travels only here — to OUR registry — never in MCP traffic.
"""

from __future__ import annotations

import json
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
    return 200, safe_get(url, headers=headers)


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
            m = json.loads(cache.read_text("utf-8"))
            return FetchedSurface(
                name=m["name"],
                surface_rev=m["surface_rev"],
                tier=m["tier"],
                spec=m["spec"],
                stale=True,
            )
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
    m = json.loads(body)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(m), "utf-8")
    return FetchedSurface(
        name=m["name"],
        surface_rev=m["surface_rev"],
        tier=m["tier"],
        spec=m["spec"],
        stale=False,
    )
