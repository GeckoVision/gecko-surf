"""The catalogs, as :class:`CandidateEndpoint` rows the planner may choose from.

Two inputs, both untrusted: Orthogonal's ``/v1/details`` records (pulled to disk; one
JSON file per endpoint) and the pay.sh catalog (:mod:`gecko.paysh_catalog`). Every
description goes through ``untrusted_summary``; every response path must match a plain
field-name grammar or it is dropped, so a field name cannot smuggle prose to the model.

What is kept out of the pool, and why (each is counted in :class:`PoolReport`):

* methods other than GET and POST, and endpoints whose path or description says they
  ACT (send, reply, sign up, start, cancel, exec, webhook, checkout). A data plan reads;
  it never sends an email or starts a machine on the user's money.
* ``hasDynamicPricing``: the listed price is not the price, so no honest whole-run quote
  can include it.
* endpoints with no response field, declared or observed: a field cannot be read from a
  reply nobody has described.

Response paths are written the way ``supplier.readers.field_names`` reports them
(``data[].attributes.title``). Orthogonal declares list items as ``.0.``; that is folded
to ``[]``. Paths observed in a live field ledger are merged in, because Orthogonal
declares none for some of the most useful endpoints (financial-datasets, akta-pro).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from gecko.orthogonal.supplier.text import untrusted_summary

from ..paysh_catalog import VERIFIED
from ..paysh_catalog import CatalogEntry as PayshEntry
from .types import Catalog, CandidateEndpoint, CandidateSet, Storage

__all__ = [
    "ObservedPaths",
    "PoolReport",
    "candidate_id",
    "candidate_set",
    "confirmed_pairs",
    "fold_path",
    "load_details_dir",
    "load_observed_paths",
    "orthogonal_candidates",
    "paysh_candidates",
    "snapshot_sha",
    "usd_to_cents",
]

#: (api, path template) -> response paths seen in a live reply. Field NAMES only.
ObservedPaths = Mapping[tuple[str, str], frozenset[str]]

_MAX_FILE_BYTES = 512_000
_MAX_PATHS = 200
_DESC_CAP = 300
_NOTE_CAP = 160
_MAX_NOTES = 8
_PATH_RE = re.compile(r"^[A-Za-z0-9_\-\[\]\.]{1,129}$")
_LIST_INDEX = re.compile(r"\.(\d+)(?=\.|$)")
#: Orthogonal's own envelope and billing keys. True of every reply, so never a fact.
_ENVELOPE = frozenset(
    {
        "billing",
        "chargeinfo",
        "quotedpricecents",
        "pricecents",
        "credits_remaining",
        "credits_consumed",
        "creditscharged",
        "requestid",
        "parentrequestid",
        "success",
    }
)
#: Words in a path or description that mean the call DOES something. Whole tokens only.
_ACTION_WORDS = frozenset(
    {
        "send",
        "reply",
        "forward",
        "signup",
        "sign-up",
        "checkout",
        "checkout-link",
        "cancel",
        "stop",
        "stop_run",
        "pause",
        "resume",
        "start",
        "exec",
        "execute",
        "webhook",
        "poll",
        "polling",
        "run-async",
        "verify",
        "drafts",
        "inboxes",
        "calls",
        "messages",
        "machines",
        "voices",
        "numbers",
        "agents",
    }
)
#: The first word of a description, when it is one of these, says the call acts.
_ACTION_VERBS = frozenset(
    {
        "send",
        "reply",
        "forward",
        "cancel",
        "stop",
        "pause",
        "resume",
        "start",
        "create",
        "delete",
        "update",
        "publish",
        "mint",
        "add",
        "remove",
        "purchase",
        "buy",
        "order",
        "book",
    }
)
_PATH_TOKEN = re.compile(r"[a-z0-9_\-]+")


def candidate_id(catalog: Catalog, api: str, method: str, path: str) -> str:
    return f"{catalog}:{api}:{method}:{path}"


@dataclass
class PoolReport:
    """How many endpoints each rule kept out. Counts only; for the eval and the log."""

    kept: int = 0
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, why: str) -> None:
        self.dropped[why] = self.dropped.get(why, 0) + 1


def load_details_dir(directory: Path) -> list[dict[str, Any]]:
    """Every ``*.json`` details record under ``directory``, oversized or broken files
    skipped. The records are untrusted; :func:`orthogonal_candidates` parses them."""
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.stat().st_size > _MAX_FILE_BYTES:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def load_observed_paths(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], frozenset[str]]:
    """Field-ledger rows -> the response paths each Orthogonal endpoint has really sent."""
    seen: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        if row.get("catalog") != "orthogonal":
            continue
        api, path = row.get("api"), row.get("path")
        names = row.get("output_fields")
        if not isinstance(api, str) or not isinstance(path, str):
            continue
        if not isinstance(names, list):
            continue
        bucket = seen.setdefault((api, path), set())
        bucket.update(n for n in names if isinstance(n, str) and _PATH_RE.match(n))
    return {key: frozenset(v) for key, v in seen.items() if v}


def fold_path(name: str) -> str | None:
    """A response path in the ``field_names`` grammar, or ``None`` when it is not one or
    is Orthogonal's own envelope. Public because the service registry keys fields by it."""
    folded = _LIST_INDEX.sub("[]", name).replace(".[]", "[]")
    if not _PATH_RE.match(folded):
        return None
    head = folded.split(".", 1)[0].split("[", 1)[0].lower()
    return None if head in _ENVELOPE else folded


def _params(raw: Any) -> list[dict[str, Any]]:
    return [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []


def _input_notes(params: list[dict[str, Any]]) -> tuple[str, ...]:
    """``name (type): description`` for each described input, at most
    :data:`_MAX_NOTES`. A description is untrusted catalog text, summarised and capped."""
    notes: list[str] = []
    for p in params:
        name, desc = p.get("name"), p.get("description")
        if not isinstance(name, str) or not name.strip() or not isinstance(desc, str):
            continue
        text = untrusted_summary(desc, _NOTE_CAP)
        if not text:
            continue
        kind = p.get("type")
        typed = f" ({kind[:20]})" if isinstance(kind, str) and kind else ""
        notes.append(f"{name[:64]}{typed}: {text}")
    return tuple(notes[:_MAX_NOTES])


def _list_inputs(params: list[dict[str, Any]]) -> frozenset[str]:
    return _names([p for p in params if p.get("type") == "array"])


def _names(params: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(
        str(p["name"])[:64]
        for p in params
        if isinstance(p.get("name"), str) and p["name"].strip()
    )


def _acts(path: str, description: str) -> bool:
    if set(_PATH_TOKEN.findall(path.lower())) & _ACTION_WORDS:
        return True
    words = description.lower().split()
    return bool(words) and words[0].strip(".,:;") in _ACTION_VERBS


def usd_to_cents(price: Any) -> int | None:
    """Dollars -> whole cents, rounded UP so a quote never under-counts."""
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
        return None
    return math.ceil(round(float(price) * 100, 6))


def orthogonal_candidates(
    records: Iterable[Mapping[str, Any]],
    *,
    observed: ObservedPaths | None = None,
    storage: Mapping[str, Storage] | None = None,
    report: PoolReport | None = None,
) -> list[CandidateEndpoint]:
    """``/v1/details`` records -> candidates. ``storage`` maps an api slug to what its
    terms say, as METADATA: anything unmapped is ``"unknown"``, and no label stops a
    value being stored for the customer who asked (founder ruling, 8 Oct)."""
    seen_paths = observed or {}
    terms = storage or {}
    tally = report if report is not None else PoolReport()
    out: dict[str, CandidateEndpoint] = {}
    for record in records:
        details = record.get("details")
        endpoint = details.get("endpoint") if isinstance(details, dict) else None
        api = record.get("api")
        if not isinstance(endpoint, dict) or not isinstance(api, str):
            tally.drop("malformed")
            continue
        method = str(endpoint.get("method", "")).upper()
        path = endpoint.get("path")
        if not isinstance(path, str) or method not in ("GET", "POST"):
            tally.drop("not GET or POST")
            continue
        raw_desc = endpoint.get("description")
        description = untrusted_summary(raw_desc, _DESC_CAP)
        if _acts(path, raw_desc if isinstance(raw_desc, str) else ""):
            tally.drop("acts rather than reads")
            continue
        if endpoint.get("hasDynamicPricing"):
            tally.drop("dynamic price")
            continue
        cents = usd_to_cents(endpoint.get("price"))
        if cents is None:
            tally.drop("no price")
            continue
        declared = (
            fold_path(str(p.get("name", "")))
            for p in _params(endpoint.get("responseParams"))
        )
        observed_here = (fold_path(o) for o in seen_paths.get((api, path), ()))
        paths = {p for p in declared if p} | {o for o in observed_here if o}
        if not paths:
            tally.drop("no response field declared or observed")
            continue
        query = _params(endpoint.get("queryParams"))
        body = _params(endpoint.get("bodyParams"))
        path_params = _params(endpoint.get("pathParams"))
        required = _names([p for p in query + body if p.get("required")])
        # A path placeholder is always required; the validator lets only a subject fill it.
        required |= _names(path_params)
        cid = candidate_id("orthogonal", api, method, path)
        out[cid] = CandidateEndpoint(
            candidate_id=cid,
            catalog="orthogonal",
            api=api,
            method="GET" if method == "GET" else "POST",
            path=path,
            description=description,
            price_cents=cents,
            price_from="pinned",
            query_keys=_names(query),
            body_keys=_names(body),
            required_keys=required,
            response_paths=tuple(
                sorted(paths, key=lambda p: (p.count("."), p))[:_MAX_PATHS]
            ),
            wired=True,
            storage=terms.get(api, "unknown"),
            input_notes=_input_notes(query + body),
            list_inputs=_list_inputs(query + body),
        )
    tally.kept += len(out)
    return list(out.values())


def paysh_candidates(entries: Iterable[PayshEntry]) -> list[CandidateEndpoint]:
    """pay.sh providers as candidates. ``wired=False`` until lane 5 pays a pay.sh call,
    so the validator turns any pick of one into an honest ``unplanned`` row.

    pay.sh lists providers, not endpoints, and declares no response fields; a verified
    shape (:data:`gecko.paysh_catalog.VERIFIED`) supplies the method and path when known.
    """
    out: list[CandidateEndpoint] = []
    for entry in entries:
        shape = VERIFIED.get(entry.fqn)
        method: Literal["GET", "POST"] = (
            "POST" if shape is not None and shape.method == "POST" else "GET"
        )
        path = shape.path if shape is not None else "/"
        cents = usd_to_cents(entry.max_price_usd) or 0
        cid = candidate_id("paysh", entry.fqn, method, path)
        text = " ".join(
            x for x in (entry.title, entry.description, entry.use_case) if x
        )
        out.append(
            CandidateEndpoint(
                candidate_id=cid,
                catalog="paysh",
                api=entry.fqn,
                method=method,
                path=path,
                description=untrusted_summary(text, _DESC_CAP),
                price_cents=cents,
                price_from="pinned",
                query_keys=frozenset(),
                body_keys=frozenset(),
                required_keys=frozenset(),
                response_paths=(),
                wired=False,
                storage="unknown",
            )
        )
    return out


def snapshot_sha(candidates: Iterable[CandidateEndpoint]) -> str:
    """sha256 over the sorted candidate ids and their prices; order-independent."""
    digest = hashlib.sha256()
    for line in sorted(f"{c.candidate_id}\t{c.price_cents}\n" for c in candidates):
        digest.update(line.encode())
    return digest.hexdigest()


def confirmed_pairs(observed: ObservedPaths) -> frozenset[tuple[str, str]]:
    """``(candidate_id, field_path)`` for every path a live reply has really carried."""
    return frozenset(
        (candidate_id("orthogonal", api, method, path), name)
        for (api, path), names in observed.items()
        for method in ("GET", "POST")
        for name in names
    )


def candidate_set(candidates: Iterable[CandidateEndpoint]) -> CandidateSet:
    """Sorted by id, deduplicated, with the sha the plan records as ``catalog_sha``."""
    unique = {c.candidate_id: c for c in candidates}
    ordered = tuple(unique[k] for k in sorted(unique))
    return CandidateSet(candidates=ordered, snapshot_sha=snapshot_sha(ordered))
