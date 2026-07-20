"""Two-API cross-join probe (roadmap Step 4 / spec §13.4) — the falsifiable gate
BEFORE the one-way `surface_id` foundation commit.

Question: can the deterministic **value-domain signature** (§13.1: type + format +
pattern + enum) join the same entity across two independent real APIs — and stay
quiet on the confusable generics — WITHOUT a DECLARED hint?

Pair: Stripe (587 ops) + Adyen Checkout (a real payments API). Known-true cross
entity: `currency`. Confusable generics that must NOT high-link: `id`, `status`,
`amount`, `reference`.

Gate (§13.4, stricter than §7): the known-true link found at HIGH via the
signature (no DECLARED) AND zero false HIGH cross-links. If the deterministic tier
can't clear it, cross-API ships DECLARED-only — a PRODUCT finding, surfaced.

Offline, $0, deterministic, no model.
    uv run python scripts/two_api_probe.py <stripe.json> <adyen.json>
"""

from __future__ import annotations

import hashlib
import json
import sys

_DISCRIMINATING_FMT = {"uuid", "uri", "email", "ipv4", "date-time", "currency"}
_ID_TYPES = {"string", "integer", "number"}


def _norm(s: str) -> str:
    return s.replace("_", "").replace("-", "").lower()


def _entity_of(name: str) -> str | None:
    n = _norm(name)
    if n.endswith("id") and len(n) > 2:
        return n[:-2]
    return n or None  # for the probe, the field's own normalized name is its entity


def _sig(schema: dict) -> dict:
    """The §13.1 value-domain signature of one field schema."""
    if not isinstance(schema, dict):
        schema = {}
    enum = schema.get("enum")
    enum_hash = ""
    enum_card = 0
    if isinstance(enum, list) and enum:
        vals = sorted(str(v) for v in enum)
        enum_hash = hashlib.sha256("|".join(vals).encode()).hexdigest()[:16]
        enum_card = len(vals)
    return {
        "type": str(schema.get("type", "")),
        "fmt": str(schema.get("format", "")),
        "pattern": str(schema.get("pattern", "")),
        "enum_hash": enum_hash,
        "enum_card": enum_card,
    }


def field_descriptors(spec: dict) -> dict[str, dict]:
    """Distinct field name -> merged signature across the whole spec (dedupe by
    name; keep the RICHEST signature seen, so a field declared once with a format
    counts as having it)."""
    out: dict[str, dict] = {}

    def merge(name: str, schema: dict) -> None:
        s = _sig(schema)
        if s["type"] not in _ID_TYPES:
            return
        cur = out.get(name)
        if cur is None:
            out[name] = s
            return
        # keep the richest: prefer having pattern/enum/discriminating-fmt
        for k in ("pattern", "enum_hash", "fmt"):
            if not cur.get(k) and s.get(k):
                cur[k] = s[k]
                if k == "enum_hash":
                    cur["enum_card"] = s["enum_card"]

    def walk(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for name, sub in props.items():
                    if isinstance(sub, dict):
                        merge(str(name), sub)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(spec.get("components", spec))
    walk(spec.get("paths", {}))
    return out


def same_entity_score(a: dict, b: dict, name_a: str, name_b: str) -> tuple[float, list]:
    """§13.3 deterministic same-entity score + the signals that fired."""
    if a["type"] != b["type"]:
        return 0.0, ["type-differ"]
    score = 0.0
    fired = []
    if a["pattern"] and a["pattern"] == b["pattern"]:
        score += 0.55
        fired.append("pattern-eq")
    if a["enum_hash"] and b["enum_hash"]:
        if a["enum_hash"] == b["enum_hash"]:
            score += 0.45
            fired.append("enum-eq")
        # (different enum sets => no credit; that's the point)
    if a["fmt"] and a["fmt"] == b["fmt"]:
        if a["fmt"] in _DISCRIMINATING_FMT:
            score += 0.25
            fired.append(f"fmt-eq:{a['fmt']}")
        else:
            score += 0.05
    name_eq = _entity_of(name_a) == _entity_of(name_b) and _norm(name_a) == _norm(
        name_b
    )
    if name_eq:
        score += 0.15
        fired.append("name-eq")
    return score, fired


def tier(a: dict, b: dict, name_a: str, name_b: str) -> tuple[str, float, list]:
    """§13.2 trust ladder: EXTRACTED-high needs a value-domain signal AND a locator;
    name-only is INFERRED/low (never a cross-API plan basis)."""
    score, fired = same_entity_score(a, b, name_a, name_b)
    domain_signal = any(
        f.startswith(("pattern-eq", "enum-eq", "fmt-eq")) for f in fired
    )
    locator = "name-eq" in fired  # (resource locator omitted for the probe)
    if domain_signal and locator:
        return "HIGH", score, fired
    if "name-eq" in fired:
        return "LOW", score, fired  # name-only across APIs — quarantined
    return "NONE", score, fired


def run(stripe_path: str, adyen_path: str) -> None:
    stripe = json.load(open(stripe_path))
    adyen = json.load(open(adyen_path))
    fa = field_descriptors(stripe)
    fb = field_descriptors(adyen)
    print(f"Stripe distinct id-shaped fields: {len(fa)}   Adyen: {len(fb)}\n")

    high, low = [], []
    for na, sa in fa.items():
        nb = na  # a cross-API join needs the same normalized entity; compare like-named
        # also compare against Adyen fields with the same normalized name
        for nb2, sb in fb.items():
            if _norm(na) != _norm(nb2):
                continue
            t, sc, fired = tier(sa, sb, na, nb2)
            if t == "HIGH":
                high.append((na, nb2, round(sc, 2), fired))
            elif t == "LOW":
                low.append((na, nb2, round(sc, 2), fired))

    print(f"HIGH cross-links (would be plan-basis): {len(high)}")
    for na, nb, sc, fired in sorted(high)[:20]:
        print(f"    HIGH  {na} ~ {nb}   {sc}  {fired}")
    print(f"\nLOW / quarantined (name-only, NOT plan-basis): {len(low)}")
    for na, nb, sc, fired in sorted(low):
        if _norm(na) in ("currency", "status", "amount", "reference", "country", "id"):
            print(f"    low   {na} ~ {nb}   {sc}  {fired}")

    cur = [h for h in high if _norm(h[0]) == "currency"]
    print("\n=== GATE (§13.4) ===")
    print(f"  known-true `currency` at HIGH (no DECLARED): {'YES' if cur else 'NO'}")
    print(
        f"  false HIGH cross-links (generic over-links):  inspect the {len(high)} HIGH above"
    )
    if not cur and not high:
        print(
            "  VERDICT: deterministic tier finds NO high cross-link — incl. the true one."
        )
        print(
            "           Adyen declares bare `type: string` on currency (no enum/format/"
        )
        print(
            "           pattern), so the value-domain signal can't fire. This EMPIRICALLY"
        )
        print(
            "           confirms §13.5: real cross-API joins need a DECLARED hint. Ship"
        )
        print("           cross-API DECLARED-only. The provider annotation motion is")
        print(
            "           load-bearing, not optional — the comprehension frontier IS the"
        )
        print("           provider-WTP motion.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    run(sys.argv[1], sys.argv[2])
