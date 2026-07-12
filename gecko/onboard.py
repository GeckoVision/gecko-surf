"""`gecko add` onboarding — glue over the engine. Thin, control-plane only."""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import docs_reader
from .netguard import UnsafeUrlError, validate_public_url

Fetcher = Callable[[str], str]


class OnboardError(Exception):
    """A recoverable onboarding failure (bad spec, unreachable source, etc.)."""


def _default_fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=20) as r:  # nosec - validated below
        return r.read().decode("utf-8", "replace")


def resolve_spec(
    ref: str, *, fetch: Fetcher | None = None, resolver: Any = None
) -> dict[str, Any]:
    """Resolve an API reference to an OpenAPI dict.

    ``ref`` may be an http(s) OpenAPI URL, an http(s) docs page (recovered via
    from-docs), or a local path (dev). http(s) inputs are SSRF-validated first.
    """
    fetch = fetch or _default_fetch
    if ref.startswith(("http://", "https://")):
        try:
            validate_public_url(ref, resolver=resolver)
        except UnsafeUrlError as exc:
            raise OnboardError(f"refusing unsafe URL: {exc}") from exc
        body = fetch(ref)
        try:
            spec = json.loads(body)
            if isinstance(spec, dict) and spec.get("openapi"):
                return spec
        except json.JSONDecodeError:
            pass
        # Not a JSON spec — try docs recovery.
        result = docs_reader.from_docs(ref)
        return result.draft
    # Local path (dev convenience).
    try:
        with open(ref, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise OnboardError(f"could not read spec at {ref}: {exc}") from exc


def safe_name(ref: str) -> str:
    """A filesystem/name-safe surface id derived from a ref (host or slug)."""
    base = ref
    if ref.startswith(("http://", "https://")):
        from urllib.parse import urlsplit

        base = urlsplit(ref).netloc or ref
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return slug or "surface"


def cache_spec(name: str, spec: dict[str, Any], *, home: Path | None = None) -> Path:
    """Persist the comprehended spec (surface metadata only — no payloads)."""
    root = (home or Path.home()) / ".gecko" / "surfaces"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{safe_name(name)}.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path
