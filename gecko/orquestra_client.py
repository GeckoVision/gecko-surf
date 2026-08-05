"""A thin, typed client for Orquestra's machine surface (the program catalog).

Orquestra (``api.orquestra.dev``) exposes per-project *program metadata*: the
catalog (``/projects``), and per project ``/instructions``, ``/pda`` (seed
schemas), ``/accounts`` (struct layouts), ``/types``, and ``/idl``. This module
is transport only — fetch, cap, validate shape, return typed data. The
comprehension (surface → config) lives in :mod:`gecko.orquestra_comprehend`.

Posture (mirrors :mod:`gecko.rpc`):

- **Injected HTTP seam** (``http_get``) so every path is offline-testable.
- **http/https only**; the base URL is provider config, never spec content.
- The honest ``gecko-surf/<version>`` User-Agent (Cloudflare 1010s the stdlib
  default).
- **Every response is UNTRUSTED input**: sizes are capped, JSON shapes are
  validated, ids are pattern-checked, nothing is ever eval'd or executed.
- **Control plane only** (invariant #1): everything fetched is program METADATA
  (ids, names, seed schemas, layouts) — no account data, no user data, no
  secrets exist on this surface.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from .rpc import user_agent

__all__ = [
    "HttpGet",
    "OrquestraClientError",
    "ProjectInfo",
    "ProjectCatalogPage",
    "ProjectSurface",
    "OrquestraClient",
]

# A GET caller: url -> raw response bytes. The one seam tests inject.
HttpGet = Callable[[str], bytes]

# Hard cap on any single response body. The largest real payload today (Meteora's
# IDL) is ~105 KiB; 8 MiB leaves generous headroom while bounding a hostile or
# broken endpoint (untrusted-input posture).
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# A project slug is an opaque id (alnum or uuid-with-dashes). Anything else —
# slashes, dots, spaces — is rejected before it can splice into the URL path.
_SLUG_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

# A base58 pubkey at Solana address length.
_PROGRAM_ID_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class OrquestraClientError(Exception):
    """A transport or shape failure against the Orquestra surface. Messages carry
    only public metadata (URLs, slugs, HTTP codes) — this surface has no secrets."""


@dataclass(frozen=True)
class ProjectInfo:
    """One catalog entry: the listable identity of an Orquestra project."""

    id: str
    name: str
    program_id: str
    description: str = ""


@dataclass(frozen=True)
class ProjectCatalogPage:
    """One page of the (paginated) project catalog."""

    projects: tuple[ProjectInfo, ...]
    page: int
    total_pages: int
    total: int


@dataclass(frozen=True)
class ProjectSurface:
    """What auto-comprehend consumes for one project: its id, its program id, the
    IDL payload, and Orquestra's own per-instruction PDA schemas."""

    slug: str
    program_id: str
    idl: dict[str, Any]
    pda_accounts: tuple[dict[str, Any], ...]


def _default_http_get(url: str) -> bytes:
    """Stdlib GET with the shared UA; reads at most ``MAX_RESPONSE_BYTES + 1``."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - scheme validated
        return resp.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[no-any-return]


class OrquestraClient:
    """Typed access to the Orquestra project catalog + per-project surfaces."""

    def __init__(
        self, base_url: str | None = None, http_get: HttpGet | None = None
    ) -> None:
        if base_url is None:
            # The provider's packaged base URL — data, not a hardcoded endpoint.
            from .provider_config import load_packaged_provider_base_url

            base_url = load_packaged_provider_base_url("orquestra")
        scheme = urlparse(base_url).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise OrquestraClientError(
                f"base url must be http/https, got scheme {scheme!r}"
            )
        self._base_url = base_url.rstrip("/")
        self._http_get = http_get or _default_http_get

    # -- raw (validated) fetch ----------------------------------------------

    def _get_json(self, path: str) -> dict[str, Any]:
        url = f"{self._base_url}/{path}"
        try:
            raw = self._http_get(url)
        except OrquestraClientError:
            raise
        except Exception as exc:  # urllib raises several unrelated types
            raise OrquestraClientError(f"GET {url} failed: {exc}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise OrquestraClientError(
                f"GET {url}: response exceeds {MAX_RESPONSE_BYTES} bytes — refusing "
                "(untrusted input cap)"
            )
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise OrquestraClientError(f"GET {url}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise OrquestraClientError(
                f"GET {url}: expected a JSON object, got {type(data).__name__}"
            )
        return data

    @staticmethod
    def _check_slug(slug: str) -> str:
        if not _SLUG_RE.match(slug):
            raise OrquestraClientError(
                f"invalid project slug {slug!r} (alnum/dash only)"
            )
        return slug

    def project_endpoint(self, slug: str, endpoint: str) -> dict[str, Any]:
        """GET one per-project endpoint (``instructions``/``pda``/``accounts``/
        ``types``/``idl``) as a validated JSON object."""
        if endpoint not in ("instructions", "pda", "accounts", "types", "idl"):
            raise OrquestraClientError(f"unknown project endpoint {endpoint!r}")
        return self._get_json(f"{self._check_slug(slug)}/{endpoint}")

    # -- typed surface ------------------------------------------------------

    def list_projects(self, page: int = 1) -> ProjectCatalogPage:
        """One page of the project catalog (``GET /projects?page=N``)."""
        if page < 1:
            raise OrquestraClientError(f"page must be >= 1, got {page}")
        data = self._get_json(f"projects?page={page}")
        raw_projects = data.get("projects")
        if not isinstance(raw_projects, list):
            raise OrquestraClientError("catalog response has no 'projects' list")
        projects: list[ProjectInfo] = []
        for entry in raw_projects:
            if not isinstance(entry, dict):
                continue  # tolerate junk entries; never a whole junk payload
            pid = str(entry.get("id", ""))
            program_id = str(entry.get("program_id", ""))
            if not _SLUG_RE.match(pid) or not _PROGRAM_ID_RE.match(program_id):
                continue  # a poisoned/garbled row must not enter the catalog
            projects.append(
                ProjectInfo(
                    id=pid,
                    name=str(entry.get("name", "")),
                    program_id=program_id,
                    description=str(entry.get("description") or ""),
                )
            )
        pagination = data.get("pagination")
        pagination = pagination if isinstance(pagination, dict) else {}
        return ProjectCatalogPage(
            projects=tuple(projects),
            page=int(pagination.get("page", page)),
            total_pages=int(pagination.get("totalPages", 1)),
            total=int(pagination.get("total", len(projects))),
        )

    def fetch_surface(self, slug: str) -> ProjectSurface:
        """Fetch what auto-comprehend needs: ``/pda`` + ``/idl``, cross-checked.

        The program id is asserted consistent between the two endpoints (and with
        the IDL's own address where it carries one) — a mismatch means a garbled
        or poisoned surface and is refused, never papered over.
        """
        pda_payload = self.project_endpoint(slug, "pda")
        idl_payload = self.project_endpoint(slug, "idl")

        program_id = str(pda_payload.get("programId", ""))
        if not _PROGRAM_ID_RE.match(program_id):
            raise OrquestraClientError(
                f"project {slug!r}: /pda carries no valid programId"
            )
        idl_program = str(idl_payload.get("programId", ""))
        if idl_program and idl_program != program_id:
            raise OrquestraClientError(
                f"project {slug!r}: /pda programId {program_id} != /idl programId "
                f"{idl_program} — refusing an inconsistent surface"
            )

        idl = idl_payload.get("idl")
        if not isinstance(idl, dict):
            raise OrquestraClientError(f"project {slug!r}: /idl payload has no IDL")
        idl_address = idl.get("address") or (idl.get("metadata") or {}).get("address")
        if idl_address and str(idl_address) != program_id:
            raise OrquestraClientError(
                f"project {slug!r}: IDL address {idl_address} != programId "
                f"{program_id} — refusing an inconsistent surface"
            )

        raw_accounts = pda_payload.get("pdaAccounts")
        pda_accounts = (
            tuple(e for e in raw_accounts if isinstance(e, dict))
            if isinstance(raw_accounts, list)
            else ()
        )

        return ProjectSurface(
            slug=slug,
            program_id=program_id,
            idl=idl,
            pda_accounts=pda_accounts,
        )
