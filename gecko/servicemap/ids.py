"""Stable ids for registry rows, derived from what the row is, never assigned.

``op_id`` keys a route by its service SLUG plus path, not by its host: hosts drift
(pay.sh's catalog lists hosts that 302 to a dashboard), slugs are what every catalog
names a service by, and an op id that changed with the host would split one route's
history in two. Placeholder names are dropped (``{company}`` and ``:id`` are both
``{}``) because two catalogs naming a path parameter differently still list one route.
"""

from __future__ import annotations

import hashlib
import re

from .types import Equivalence

__all__ = [
    "equivalence_id",
    "field_id",
    "normalize_path",
    "offer_id",
    "op_id",
    "sighting_id",
    "url_template",
]

_PLACEHOLDER = re.compile(r"\{[^/{}]*\}|(?<=/):[A-Za-z_][A-Za-z0-9_]*")
_SLASHES = re.compile(r"/{2,}")


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def normalize_path(path: str) -> str:
    """No query string, no repeated or trailing slash, every placeholder ``{}``."""
    bare = path.split("?", 1)[0].split("#", 1)[0].strip()
    bare = _PLACEHOLDER.sub("{}", bare)
    bare = _SLASHES.sub("/", "/" + bare.lstrip("/"))
    return bare.rstrip("/") or "/"


def url_template(service_id: str, path: str) -> str:
    return f"{service_id.strip().lower()}{normalize_path(path)}"


def op_id(method: str, template: str) -> str:
    """sha256 of the upper-cased method and the normalized URL template."""
    return _sha(method.strip().upper(), template)


def sighting_id(catalog: str, api: str, method: str, path: str) -> str:
    # The catalog's own spelling: two listings that normalize to one op stay two rows.
    return _sha(catalog, api, method.upper(), path)


def field_id(op: str, path: str) -> str:
    return _sha(op, path)


def offer_id(op: str, network: str, asset: str, pay_to: str) -> str:
    return _sha(op, network, asset, pay_to)


def equivalence_id(equivalence: Equivalence) -> str:
    members = sorted(f"{source}\x1e{path}" for source, path in equivalence.paths)
    return _sha(equivalence.meaning, *members)
