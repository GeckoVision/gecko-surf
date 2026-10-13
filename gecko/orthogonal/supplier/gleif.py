"""GLEIF, the public LEI register: a free parent-company source outside every catalog.

Two GETs, no key: find the supplier's LEI record by legal name, then ask for its
direct parent. A 404 on the parent step is GLEIF's answer "no parent reported", not a
failure. The same-entity check is here: the record found must carry the legal name we
searched for (normalized), or the reading is refused — a near-name match is a
different company until a person says otherwise.

NOT FETCHED FROM HERE. The base URL is config (``SupplierDeps.gleif_base_url``); the
default is GLEIF's documented public API root, and the two paths and the response
fields (``data[].id``, ``data[].attributes.entity.legalName.name``) follow GLEIF's API
documentation as recalled, not as measured. The recorded fixture below follows that
shape. The first live call is the founder's run; until then treat the shape as a claim.
Every live request goes through ``netguard`` (``validate_public_url`` +
``safe_get_status``) and must be https.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ...netguard import Resolver, UnsafeUrlError, safe_get_status, validate_public_url
from ..errors import ListJobError
from ..steps import PublicStep
from .field_readers import Reading
from .fields import NO_PARENT
from .readers import _dict, _text, field_names
from .text import normalize_company, untrusted_summary

DEFAULT_GLEIF_BASE_URL = "https://api.gleif.org/api/v1"
SEARCH_PATH = "/lei-records"
PARENT_PATH = "/lei-records/{lei}/direct-parent"
#: A record is a few KB; a page of one is far under this.
MAX_GLEIF_BYTES = 2 * 1024 * 1024
_LEI = re.compile(r"^[A-Z0-9]{20}$")

#: url -> (HTTP status, body). Live: ``gleif_live_fetch``. Recorded: the made-up one.
PublicFetch = Callable[[str], tuple[int, str]]


class GleifConfigError(ListJobError):
    code = "gleif_config"


def check_base_url(base: str) -> str:
    """https, no query, no fragment. DNS is checked per request, by ``netguard``."""
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise GleifConfigError(
            "The GLEIF base URL must be a plain https URL, like "
            f"{DEFAULT_GLEIF_BASE_URL}."
        )
    return base.rstrip("/")


def search_url(base: str, legal_name: str) -> str:
    name = quote(legal_name[:200], safe="")
    return (
        f"{check_base_url(base)}{SEARCH_PATH}"
        f"?filter%5Bentity.legalName%5D={name}&page%5Bsize%5D=1"
    )


def parent_url(base: str, lei: str) -> str:
    if not _LEI.match(lei):
        raise GleifConfigError("GLEIF returned an identifier that is not an LEI.")
    return f"{check_base_url(base)}{PARENT_PATH.replace('{lei}', lei)}"


def gleif_live_fetch(url: str, *, resolver: Resolver | None = None) -> tuple[int, str]:
    """The SSRF-guarded GET (wire edge). Validated here and on every redirect."""
    if urlsplit(url).scheme != "https":
        raise UnsafeUrlError("GLEIF is fetched over https only")
    validate_public_url(url, resolver=resolver)
    return safe_get_status(
        url, max_bytes=MAX_GLEIF_BYTES, timeout=30, resolver=resolver
    )


#: One GET, as the field ledger keeps it. The shared shape every public read uses.
GleifStep = PublicStep


@dataclass
class GleifResult:
    reading: Reading | None
    #: Our words: why there is no reading, or what was found.
    reason: str
    steps: list[GleifStep] = field(default_factory=list)


def _get(
    fetch: PublicFetch, url: str, path: str, inputs: tuple[str, ...]
) -> tuple[Any, GleifStep]:
    try:
        status, text = fetch(url)
    except (UnsafeUrlError, OSError) as exc:
        kind = "refused" if isinstance(exc, UnsafeUrlError) else "unreachable"
        return None, GleifStep(path, inputs, None, (), 0, kind)
    try:
        body = json.loads(text) if text else None
    except ValueError:
        return None, GleifStep(path, inputs, status, (), 0, "not json")
    data = _dict(body).get("data")
    rows = len(data) if isinstance(data, list) else (1 if data else 0)
    error = None if 200 <= status < 300 else "upstream_error"
    return body, GleifStep(path, inputs, status, tuple(field_names(body)), rows, error)


def _legal_name(record: Any) -> str | None:
    entity = _dict(_dict(_dict(record).get("attributes")).get("entity"))
    return _text(_dict(entity.get("legalName")).get("name"))


def lookup_parent(
    entity_name: str, *, base_url: str, fetch: PublicFetch
) -> GleifResult:
    search_inputs = ("filter[entity.legalName]", "page[size]")
    body, step = _get(
        fetch, search_url(base_url, entity_name), SEARCH_PATH, search_inputs
    )
    result = GleifResult(None, "", [step])
    records = _dict(body).get("data")
    if step.error or not isinstance(records, list) or not records:
        result.reason = "GLEIF had no LEI record for that name"
        return result
    record = _dict(records[0])
    found = _legal_name(record)
    if not found or normalize_company(found) != normalize_company(entity_name):
        result.reason = "the LEI record GLEIF matched names a different entity"
        return result
    lei = _text(record.get("id")) or ""
    try:
        url = parent_url(base_url, lei)
    except GleifConfigError as exc:
        result.reason = exc.message
        return result
    body, step = _get(fetch, url, PARENT_PATH, ("lei",))
    result.steps.append(step)
    if step.http_status == 404:
        result.reading, result.reason = Reading(NO_PARENT), "GLEIF reports no parent"
        return result
    parent = _legal_name(_dict(body).get("data"))
    if step.error or not parent:
        result.reason = "GLEIF's parent reply could not be read"
        return result
    result.reading = Reading(untrusted_summary(parent) or None)
    result.reason = "GLEIF names a direct parent"
    return result


# --- recorded mode: a made-up register (``.example`` companies only) ---------------

_MADE_UP_LEI: dict[str, tuple[str, str]] = {
    # normalized legal name -> (LEI, legal name)
    "invented assembly": ("MADEUP00000000000001", "Invented Assembly Ltd"),
    "made up circuits": ("MADEUP00000000000002", "Made Up Circuits Inc."),
}
_MADE_UP_PARENTS = {"MADEUP00000000000001": "Fictional Holdings Ltd"}


def _record(lei: str, legal: str) -> dict[str, Any]:
    return {
        "type": "lei-records",
        "id": lei,
        "attributes": {"lei": lei, "entity": {"legalName": {"name": legal}}},
    }


def made_up_gleif_fetch(url: str) -> tuple[int, str]:
    """Answers like GLEIF (as documented) for two made-up companies; 200 + no data
    for anything else."""
    parts = urlsplit(url)
    if parts.path.endswith("/direct-parent"):
        lei = parts.path.split("/")[-2]
        parent = _MADE_UP_PARENTS.get(lei)
        if parent is None:
            return 404, json.dumps({"errors": [{"status": "404"}]})
        return 200, json.dumps({"data": _record("MADEUP00000000000009", parent)})
    query = dict(pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair)
    wanted = normalize_company(unquote(query.get("filter%5Bentity.legalName%5D", "")))
    hit = _MADE_UP_LEI.get(wanted)
    data = [_record(hit[0], hit[1])] if hit else []
    return 200, json.dumps({"data": data, "meta": {"pagination": {"total": len(data)}}})
