"""The hosted dataset endpoint: ``/d/<dataset_id>/mcp`` and its two downloads. Transport.

One MCP app serves every dataset. :class:`DatasetRouter` fronts it: it reads the
dataset from the PATH, resolves the presented key with
:func:`~gecko.anyquery.reader_keys.resolve_dataset_access` (the owner's ``gecko_sk_``
key, or a ``gecko_rk_`` reader key), and hands both down in the request scope
(``keyauth.PRINCIPAL_SCOPE_KEY``). The tools, the masking and the refusals are
:mod:`gecko.anyquery.dataset_surface`; nothing here decides what a key may see.

* ``POST /d/<id>/mcp``: the four read-only tools.
* ``GET /d/<id>.json`` and ``GET /d/<id>.parquet``: the current table, masked; add
  ``?reveal_pii=true`` with the OWNER's key for contact details whole. A reader key gets
  masked data whatever it asks (founder ruling, 12 Oct 2026).

Not behind the account gate: a reader key is no account, so ``GeckoKeyResolver`` would
refuse it. A missing key is not a 401 here: the tool answers ``no_account`` with the way
in, and a download answers 401. This mount never spends and imports nothing that pays.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..keyauth import PRINCIPAL_SCOPE_KEY, bearer_from_scope
from ..keyregistry import GeckoKeyResolver, KeyRegistry, RegistryAllowlist
from .dataset_download import Download, dataset_download
from .dataset_read import DatasetCaller
from .dataset_surface import call_dataset_tool
from .dataset_tools import (
    DATASET_INSTRUCTIONS,
    DATASET_MOUNT,
    OWNER_TOOLS_ELSEWHERE,
    dataset_tools,
)
from .reader_keys import (
    READER_COLLECTION,
    DatasetAccess,
    MongoReaderKeyRegistry,
    ReaderKeyRegistry,
    resolve_dataset_access,
)
from .store import Store

__all__ = [
    "DatasetRouter",
    "DatasetSurface",
    "dataset_mount",
    "reader_registry_for",
]

logger = logging.getLogger(__name__)

#: ``/<dataset_id>/mcp``, ``/<dataset_id>.json`` or ``/<dataset_id>.parquet``, below /d.
_ROUTE = re.compile(r"^/([a-z0-9][a-z0-9-]{0,39})(/mcp|\.json|\.parquet)$")
_STATUS = {
    "no_account": 401,
    "dataset_not_found": 404,
    "no_version_yet": 404,
    "parquet_unavailable": 501,
    "store_unavailable": 503,
}
_TRUE = {"1", "true", "yes"}


@dataclass(frozen=True)
class DatasetEndpointDeps:
    store: Store
    #: A presented key -> what it may read, or None. Fail-closed.
    resolve_access: Callable[[str], DatasetAccess | None]


class DatasetSurface:
    """Duck-typed for ``http_server``: ``principal`` comes from :class:`DatasetRouter`."""

    surface_id = "gecko:dataset"
    instructions = DATASET_INSTRUCTIONS
    #: The owner's tools, asked for here, get a pointer to /anyquery/mcp, not a -32602.
    hidden_tools = OWNER_TOOLS_ELSEWHERE

    def __init__(self, deps: DatasetEndpointDeps) -> None:
        self.deps = deps

    def list_tools(self, principal: Any = None) -> list[dict[str, Any]]:
        caller = _caller(principal)
        access = None if caller is None else caller.access
        return dataset_tools(may_reveal=access is not None and access.may_reveal_pii)

    def call_tool(
        self, name: str, arguments: dict[str, Any], principal: Any = None
    ) -> dict[str, Any]:
        return call_dataset_tool(
            self.deps.store, _caller(principal), name, dict(arguments or {})
        )

    def wrap_mount(self, inner: Any) -> DatasetRouter:
        return DatasetRouter(inner, self.deps)


def _caller(principal: Any) -> DatasetCaller | None:
    return principal if isinstance(principal, DatasetCaller) else None


def _query_flag(scope: Any, name: str) -> bool:
    from urllib.parse import parse_qs

    raw = scope.get("query_string") or b""
    values = parse_qs(bytes(raw).decode("latin-1")).get(name, [])
    return any(v.strip().lower() in _TRUE for v in values)


class DatasetRouter:
    """ASGI: route ``/<id>/mcp`` to the MCP app with the caller in scope; serve the
    downloads itself. Every other path below the mount is a 404."""

    def __init__(self, inner: Any, deps: DatasetEndpointDeps) -> None:
        self._inner = inner
        self._deps = deps

    def _caller(self, scope: Any, dataset_id: str) -> DatasetCaller:
        token = bearer_from_scope(scope)
        access = self._deps.resolve_access(token) if token else None
        return DatasetCaller(dataset_id=dataset_id, access=access)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._inner(scope, receive, send)
            return
        from starlette._utils import get_route_path
        from starlette.responses import JSONResponse, Response

        found = _ROUTE.match(get_route_path(scope))
        if found is None:
            await JSONResponse(
                {"error": "not found", "code": "not_found"}, status_code=404
            )(scope, receive, send)
            return
        dataset_id, tail = found.group(1), found.group(2)
        caller = self._caller(scope, dataset_id)
        if tail == "/mcp":
            child = dict(scope)
            # The MCP app is mounted once; it sees /mcp below /d/<id>.
            child["root_path"] = f"{scope.get('root_path', '')}/{dataset_id}"
            # Written unconditionally: only this edge may say who is asking.
            child[PRINCIPAL_SCOPE_KEY] = caller
            await self._inner(child, receive, send)
            return
        if scope.get("method") != "GET":
            await JSONResponse(
                {"error": "downloads are GET", "code": "method_not_allowed"},
                status_code=405,
            )(scope, receive, send)
            return
        result = dataset_download(
            self._deps.store,
            caller,
            "json" if tail == ".json" else "parquet",
            reveal=_query_flag(scope, "reveal_pii"),
        )
        if isinstance(result, Download):
            await Response(
                result.body,
                media_type=result.media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{result.filename}"',
                    "Cache-Control": "no-store",
                },
            )(scope, receive, send)
            return
        status = _STATUS.get(str(result.get("code")), 400)
        await JSONResponse(result, status_code=status)(scope, receive, send)


def reader_registry_for(keys: KeyRegistry) -> ReaderKeyRegistry | None:
    """Reader keys live beside the account keys, in the registry's own database."""
    collection = getattr(keys, "collection", None)
    database = getattr(collection, "database", None)
    if database is None:
        return None
    return MongoReaderKeyRegistry(database[READER_COLLECTION])


def dataset_mount(
    store: Store | None,
    *,
    keys: KeyRegistry | None,
    readers: ReaderKeyRegistry | None,
) -> tuple[str, DatasetSurface] | None:
    """The ``/d`` mount, or ``None`` when there is no store or no key registry: without
    them every call would answer "no account", a broken deploy that looks working."""
    if store is None:
        return None
    if keys is None or readers is None:
        logger.warning("dataset endpoint not served: no key registry")
        return None
    owner_of = GeckoKeyResolver(keys)
    enabled = RegistryAllowlist(keys).is_enabled

    def resolve(token: str) -> DatasetAccess | None:
        return resolve_dataset_access(
            token, readers=readers, resolve_owner=owner_of, owner_enabled=enabled
        )

    return DATASET_MOUNT, DatasetSurface(DatasetEndpointDeps(store, resolve))
