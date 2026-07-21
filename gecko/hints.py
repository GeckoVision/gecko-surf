"""DECLARED entity hints — the top of the §13.2 trust ladder.

Two sources, one vocabulary (``name -> entity``):

1. **Provider-authored** (``x-gecko`` in the spec, §14 authored-enrichment):
   a root ``x-gecko: {entities: {name: entity}}`` block and/or inline
   ``x-gecko-entity: <entity>`` on a parameter object or schema property.
2. **Customer-confirmed** (``gecko graph confirm``, §12): per-surface JSON under
   ``~/.gecko/declared/`` recording name -> entity with an audit trail
   (when, and what basis it upgraded). The SAVED RELATIONSHIP, never traffic —
   surface-level metadata only (the §14 guardrail; not the retired corpus).

Spec content is UNTRUSTED input: every hint is sanitized (shape-gated names and
entities, capped counts/depth) and a bad hint is silently DROPPED — a hostile
spec must not be able to break ingest by declaring garbage, and can at worst
mint an auditable DECLARED edge whose basis says exactly where it came from.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .credentials import config_home

#: entity tokens after normalization: short, lowercase, no path/format tricks.
_ENTITY_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
#: hint NAMES (param/field names) — printable, short; normalized later by graph.
_NAME_RE = re.compile(r"^[\w.\-\[\]]{1,128}$")
#: surface ids used as file names — no separators, no traversal.
_SURFACE_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")
_MAX_HINTS = 256  # hint-bomb cap: a spec cannot flood the vocabulary
_MAX_WALK_DEPTH = 30
_MAX_WALK_NODES = 200_000  # bound the inline walk on a pathological spec


def _clean(name: object, entity: object) -> tuple[str, str] | None:
    """One sanitized hint, or None (dropped). Entity is lowercased here so the
    vocabulary is stable regardless of how the author cased it."""
    if not isinstance(name, str) or not isinstance(entity, str):
        return None
    ent = entity.strip().lower()
    if not (_NAME_RE.match(name) and _ENTITY_RE.match(ent)):
        return None
    return name, ent


def declared_entity_hints(spec: Any) -> dict[str, str]:
    """The provider-authored DECLARED vocabulary of a spec: ``{name: entity}``.

    Reads the root ``x-gecko.entities`` mapping first, then walks the document
    for inline ``x-gecko-entity`` markers on parameter objects (``{"name": ...,
    "x-gecko-entity": ...}``) and schema properties (``properties.<name>.
    x-gecko-entity``). Deterministic (document order), capped, fail-quiet."""
    out: dict[str, str] = {}
    if not isinstance(spec, dict):
        return out

    root = spec.get("x-gecko")
    if isinstance(root, dict):
        entities = root.get("entities")
        if isinstance(entities, dict):
            for name, entity in entities.items():
                hint = _clean(name, entity)
                if hint and len(out) < _MAX_HINTS:
                    out.setdefault(hint[0], hint[1])

    seen = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal seen
        if depth > _MAX_WALK_DEPTH or seen > _MAX_WALK_NODES or len(out) >= _MAX_HINTS:
            return
        seen += 1
        if isinstance(node, dict):
            # a parameter object carrying its own marker
            marker = node.get("x-gecko-entity")
            pname = node.get("name")
            if marker is not None and isinstance(pname, str):
                hint = _clean(pname, marker)
                if hint:
                    out.setdefault(hint[0], hint[1])
            # schema properties carrying markers
            props = node.get("properties")
            if isinstance(props, dict):
                for fname, fschema in props.items():
                    if isinstance(fschema, dict) and "x-gecko-entity" in fschema:
                        hint = _clean(fname, fschema.get("x-gecko-entity"))
                        if hint:
                            out.setdefault(hint[0], hint[1])
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(spec.get("paths"), 0)
    walk(spec.get("components"), 0)
    return out


# --- customer-confirmed hints (per-surface persistence, §12 confirm loop) --------
def _store_path(surface: str) -> Path:
    if not _SURFACE_RE.match(surface):
        raise ValueError("invalid surface id for a declared-hint store")
    return config_home() / "declared" / f"{surface}.json"


def _read_store(surface: str) -> list[dict[str, Any]]:
    path = _store_path(surface)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    hints = raw.get("hints") if isinstance(raw, dict) else None
    return [h for h in hints if isinstance(h, dict)] if isinstance(hints, list) else []


def _write_store(surface: str, hints: list[dict[str, Any]]) -> None:
    path = _store_path(surface)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"hints": hints}, indent=2, sort_keys=True)
    path.write_text(payload, encoding="utf-8")
    os.chmod(path, 0o600)


def load_confirmed(surface: str) -> dict[str, str]:
    """The confirmed ``{name: entity}`` vocabulary for a surface (empty when none
    or the surface id is malformed — loading never raises on user files)."""
    if not _SURFACE_RE.match(surface):
        return {}
    out: dict[str, str] = {}
    for h in _read_store(surface):
        hint = _clean(h.get("name"), h.get("entity"))
        if hint:
            out[hint[0]] = hint[1]
    return out


def confirm_entity(
    surface: str, name: str, entity: str, *, prior_basis: str = ""
) -> dict[str, Any]:
    """Record a confirmed name -> entity mapping with its audit trail and return
    the stored record. Idempotent per name (re-confirming replaces, keeping the
    original ``confirmed_at`` in ``history``). ``prior_basis`` records what this
    confirmation upgraded (e.g. the INFERRED edge's basis) — the §12 audit trail."""
    hint = _clean(name, entity)
    if hint is None:
        raise ValueError("invalid hint: name/entity failed the shape gate")
    record: dict[str, Any] = {
        "name": hint[0],
        "entity": hint[1],
        "prior_basis": prior_basis,
        "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    hints = _read_store(surface)
    history = [h for h in hints if h.get("name") == hint[0]]
    if history:
        record["history"] = [
            {k: h.get(k) for k in ("entity", "confirmed_at", "prior_basis")}
            for h in history
        ]
    hints = [h for h in hints if h.get("name") != hint[0]]
    hints.append(record)
    _write_store(surface, sorted(hints, key=lambda h: str(h.get("name"))))
    return record


def remove_confirmed(surface: str, name: str) -> bool:
    """Delete a confirmed hint (idempotent); True when something was removed."""
    hints = _read_store(surface)
    kept = [h for h in hints if h.get("name") != name]
    if len(kept) == len(hints):
        return False
    _write_store(surface, kept)
    return True


def list_confirmed(surface: str) -> list[dict[str, Any]]:
    """The stored records (name, entity, confirmed_at, prior_basis, history) —
    for ``gecko graph declared``."""
    return _read_store(surface)
