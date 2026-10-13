"""Catalog and ledger readers: files in, registry drafts out. No network, no store.

Each catalog reader returns ``(Sighting, OperationDraft)`` pairs: the sighting is what
that catalog says; the draft is the route it names plus the response paths it claims.
Claims are recorded at ``CLAIMED`` and nothing more.

The field-ledger reader returns :class:`ObservedFields`: the response paths a real
reply carried, keyed by the catalog listing that was called. It reads ``catalog``,
``api``, ``path``, ``output_fields`` and ``ts`` from each row and nothing else; the
ledgers also hold subjects and counts, which never reach the registry (founder ruling,
9 Oct: names and types only).

Parsing reuses :mod:`gecko.anyquery.candidates` (``fold_path``, ``usd_to_cents``) and
:func:`gecko.paysh_catalog.fetch_catalog` with a file-backed fetcher, so a field or a
price is read here exactly as the planner reads it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from gecko.anyquery.candidates import fold_path, usd_to_cents
from gecko.paysh_catalog import CATALOG_URL, VERIFIED, CatalogEntry, fetch_catalog

from .ids import op_id, sighting_id, url_template
from .types import Catalog, Claim, FieldObs, ParamClaim, ParamIn, Sighting

__all__ = [
    "ObservedFields",
    "OperationDraft",
    "ReadReport",
    "observed_fields",
    "orthogonal_sightings",
    "paysh_entries_from_text",
    "paysh_sightings",
]

_TEXT_CAP = 2000
_NAME_CAP = 64
_KIND_CAP = 20
_NOTE_CAP = 200
_MAX_PATHS = 400


@dataclass(frozen=True)
class OperationDraft:
    op_id: str
    service_id: str
    method: str
    url_template: str
    claimed_paths: tuple[
        str, ...
    ]  # folded; recorded as CLAIMED by the sighting's catalog

    def claimed_fields(self, catalog: Catalog, now: str) -> list[FieldObs]:
        return [
            FieldObs(self.op_id, p, None, "CLAIMED", (catalog,), now, now, None)
            for p in self.claimed_paths
        ]


@dataclass(frozen=True)
class ObservedFields:
    catalog: str
    api: str
    path: str
    paths: frozenset[str]
    first_seen: str | None
    last_seen: str | None


@dataclass
class ReadReport:
    read: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def _text(value: Any, cap: int = _TEXT_CAP) -> str:
    return value[:cap] if isinstance(value, str) else ""


def _params(raw: Any, location: ParamIn) -> list[ParamClaim]:
    if not isinstance(raw, list):
        return []
    out: list[ParamClaim] = []
    for p in raw:
        name = p.get("name") if isinstance(p, dict) else None
        if isinstance(name, str) and name.strip():
            out.append(
                ParamClaim(
                    name[:_NAME_CAP],
                    location,
                    bool(p.get("required")),
                    kind=_text(p.get("type"), _KIND_CAP),
                    note=_text(p.get("description"), _NOTE_CAP),
                )
            )
    return out


def _folded(names: Iterable[Any]) -> tuple[str, ...]:
    kept = {f for f in (fold_path(str(n)) for n in names) if f}
    return tuple(sorted(kept))[:_MAX_PATHS]


def orthogonal_sightings(
    records: Iterable[Mapping[str, Any]],
    *,
    now: str,
    found_url: str,
    report: ReadReport | None = None,
) -> list[tuple[Sighting, OperationDraft]]:
    """``/v1/details`` records (``load_details_dir``) -> sightings. Every route is kept,
    including ones the planner refuses (acts, dynamic price, no fields): the registry
    maps services, and the planner's admission rules run when a snapshot is taken."""
    tally = report if report is not None else ReadReport()
    out: list[tuple[Sighting, OperationDraft]] = []
    for record in records:
        details = record.get("details")
        endpoint = details.get("endpoint") if isinstance(details, dict) else None
        api = record.get("api")
        path = endpoint.get("path") if isinstance(endpoint, dict) else None
        method = endpoint.get("method") if isinstance(endpoint, dict) else None
        if not (
            isinstance(endpoint, dict)
            and isinstance(api, str)
            and api.strip()
            and isinstance(path, str)
            and isinstance(method, str)
            and method.strip()
        ):
            tally.skip("malformed")
            continue
        method = method.strip().upper()
        template = url_template(api, path)
        op = op_id(method, template)
        declared = [
            p.get("name")
            for p in endpoint.get("responseParams") or []
            if isinstance(p, dict)
        ]
        params = (
            _params(endpoint.get("queryParams"), "query")
            + _params(endpoint.get("bodyParams"), "body")
            + _params(endpoint.get("pathParams"), "path")
        )
        claim = Claim(
            title="",
            description=_text(endpoint.get("description")),
            use_case="",
            price_cents=usd_to_cents(endpoint.get("price")),
            min_price_cents=None,
            dynamic_price=bool(endpoint.get("hasDynamicPricing")),
            params=tuple(params),
            service_url=None,
        )
        sighting = Sighting(
            sighting_id=sighting_id("orthogonal", api, method, path),
            op_id=op,
            service_id=api,
            catalog="orthogonal",
            api=api,
            method=method,
            path=path,
            found_url=found_url,
            license=None,
            claimed=claim,
            first_seen=now,
            last_seen=now,
        )
        out.append(
            (sighting, OperationDraft(op, api, method, template, _folded(declared)))
        )
        tally.read += 1
    return out


def paysh_entries_from_text(text: str) -> list[CatalogEntry]:
    """A saved copy of ``https://pay.sh/api/catalog`` -> entries, through the same
    parser the live fetch uses. Raises ``paysh_catalog.CatalogError`` on a broken file."""
    return fetch_catalog(CATALOG_URL, fetcher=lambda _url: text)


def paysh_sightings(
    entries: Iterable[CatalogEntry], *, now: str, found_url: str = CATALOG_URL
) -> list[tuple[Sighting, OperationDraft]]:
    """pay.sh lists providers, not routes. A provider with a live-verified shape
    (``paysh_catalog.VERIFIED``) names its route; any other is recorded at ``/``, its
    service root, exactly as the planner's candidate does. pay.sh declares no fields."""
    out: list[tuple[Sighting, OperationDraft]] = []
    for entry in entries:
        shape = VERIFIED.get(entry.fqn)
        method = "POST" if shape is not None and shape.method == "POST" else "GET"
        path = shape.path if shape is not None else "/"
        template = url_template(entry.fqn, path)
        op = op_id(method, template)
        claim = Claim(
            title=_text(entry.title),
            description=_text(entry.description),
            use_case=_text(entry.use_case),
            price_cents=usd_to_cents(entry.max_price_usd),
            min_price_cents=usd_to_cents(entry.min_price_usd),
            dynamic_price=False,
            params=(),
            service_url=entry.service_url,
        )
        sighting = Sighting(
            sighting_id=sighting_id("paysh", entry.fqn, method, path),
            op_id=op,
            service_id=entry.fqn,
            catalog="paysh",
            api=entry.fqn,
            method=method,
            path=path,
            found_url=found_url,
            license=None,
            claimed=claim,
            first_seen=now,
            last_seen=now,
        )
        out.append((sighting, OperationDraft(op, entry.fqn, method, template, ())))
    return out


def observed_fields(rows: Iterable[Mapping[str, Any]]) -> list[ObservedFields]:
    """Field-ledger rows -> the paths each called listing really returned, with the
    earliest and latest row time. Envelope keys and non-grammar names are dropped."""
    seen: dict[tuple[str, str, str], set[str]] = {}
    times: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        catalog, api, path = row.get("catalog"), row.get("api"), row.get("path")
        names = row.get("output_fields")
        if not (
            isinstance(catalog, str)
            and isinstance(api, str)
            and isinstance(path, str)
            and isinstance(names, list)
        ):
            continue
        key = (catalog, api, path)
        bucket = seen.setdefault(key, set())
        bucket.update(
            f for f in (fold_path(n) for n in names if isinstance(n, str)) if f
        )
        ts = row.get("ts")
        if isinstance(ts, str) and ts:
            times.setdefault(key, []).append(ts)
    out: list[ObservedFields] = []
    for key, paths in sorted(seen.items()):
        if not paths:
            continue
        stamps = times.get(key, [])
        out.append(
            ObservedFields(
                *key,
                frozenset(paths),
                min(stamps) if stamps else None,
                max(stamps) if stamps else None,
            )
        )
    return out
