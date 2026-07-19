"""§7 probe, v3 basis — the gate that decides whether the surface graph gets built.

v1 (name+type) and v2 (id-shaped only) were both falsified on the Stripe control
(66k/65k edges). v3, from spec §10:
  1. ENTITY naming — the field name carries the entity ("fixtureId" = fixture+id),
     matched to a same-entity param; a bare "id" is scoped by its PARENT object.
  2. STATISTICAL genericity demotion — a name produced by more than a small
     fraction of the API's operations is auto-demoted (replaces the manual stoplist).
  3. rare non-id flow keys (e.g. "seq") survive by rarity even without an entity.

Gate: BOTH known TxLINE chains emerge AND the Stripe false-link rate stays low.
Offline, $0, deterministic.
"""

import sys
from collections import defaultdict

from gecko.ingest import load_spec, extract_operations  # noqa: E402

MAX_DEPTH = 6
GENERIC_FRAC = 0.05  # a produced name in >5% of ops is generic -> demoted


def norm(s):
    return s.replace("_", "").replace("-", "").lower()


def entity_of(name, parent=None):
    """The entity a name refers to. 'fixtureId'->'fixture'; bare 'id' under
    parent 'Customer'->'customer'; else None (not an entity reference)."""
    n = norm(name)
    if n.endswith("id") and len(n) > 2:
        return n[:-2]
    if n == "id" and parent:
        p = norm(parent)
        return p[:-1] if p.endswith("s") else p  # depluralize
    return None


def resource_noun(op):
    """The entity a path operates on: last non-parameter path segment."""
    segs = [s for s in op.path.split("/") if s and not s.startswith("{")]
    if not segs:
        return None
    last = norm(segs[-1])
    return last[:-1] if last.endswith("s") else last


def response_leaves(op):
    """(field_name, parent_object_name, type) leaves of the 200 response."""
    out = []
    resp = (op.responses or {}).get("200") or {}
    schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema") or {}

    def walk(node, parent, depth, seen):
        if depth > MAX_DEPTH or not isinstance(node, dict) or id(node) in seen:
            return
        seen = seen | {id(node)}
        # the object's own name-ish: title, or inferred from context (parent passed down)
        title = node.get("title")
        for name, sub in (node.get("properties") or {}).items():
            if isinstance(sub, dict):
                t = sub.get("type") or ("object" if sub.get("properties") else "?")
                if t in ("integer", "number", "string", "boolean"):
                    out.append((name, title or parent, "number" if t == "integer" else t))
                walk(sub, name, depth + 1, seen)
        walk(node.get("items") or {}, title or parent, depth + 1, seen)
        for k in ("oneOf", "anyOf", "allOf"):
            for sub in node.get(k, []):
                walk(sub, parent, depth + 1, seen)

    walk(schema, None, 0, frozenset())
    return out


def build_v3(ops):
    # produced- AND consumed-name frequency across ops (for genericity)
    produced_by = defaultdict(set)  # norm_name -> set(op_id) that PRODUCE it
    consumed_by = defaultdict(set)  # norm_name -> set(op_id) that CONSUME it as a param
    producers = defaultdict(list)  # norm_name -> [(op_id, field, parent)]
    for op in ops:
        seen_here = set()
        for f, parent, _t in response_leaves(op):
            n = norm(f)
            produced_by[n].add(op.operation_id)
            if n not in seen_here:
                producers[n].append((op.operation_id, f, parent, _t))
                seen_here.add(n)
        for p in op.parameters:
            consumed_by[norm(p.name)].add(op.operation_id)
    n_ops = max(1, len(ops))
    import math

    # threshold floored at 4 so a small API (seq in 1 of 18 ops) isn't over-demoted,
    # scaling to ~3% for large APIs (limit consumed by 381 of 587 -> demoted).
    generic_t = max(4, math.ceil(0.03 * n_ops))

    def is_generic(name):
        return len(produced_by[name]) > generic_t or len(consumed_by[name]) > generic_t

    kept, dropped_generic = [], 0
    for op in ops:
        rnoun = resource_noun(op)
        for p in op.parameters:
            n = norm(p.name)
            if n not in producers:
                continue
            is_path = f"{{{p.name}}}" in op.path
            # param entity: from an id-suffix name, or a bare path-param scoped by
            # its own name / the path's resource noun.
            p_ent = entity_of(p.name) or (n if is_path else None) or (rnoun if is_path else None)
            for (src_op, fld, parent, ftype) in producers[n]:
                if src_op == op.operation_id:
                    continue
                f_ent = entity_of(fld, parent)
                if f_ent and p_ent:
                    # rule 1: ENTITY match — entity ids are the API's spine, exempt
                    # from genericity (the entity scope already prevents over-linking).
                    if f_ent == p_ent:
                        kept.append((src_op, fld, op.operation_id, p.name, n))
                elif f_ent is None and p_ent is None:
                    # rule 3: a non-id flow key (e.g. `seq`) survives ONLY if rare —
                    # rule 2 genericity demotion kills widely-produced OR
                    # widely-consumed non-id names (`created`, `status`, `limit`).
                    if is_generic(n) or ftype not in ("number", "string"):
                        dropped_generic += 1
                    else:
                        kept.append((src_op, fld, op.operation_id, p.name, n))
    return sorted(set(kept)), dropped_generic


def run(label, path):
    ops = extract_operations(load_spec(path))
    kept, generic = build_v3(ops)
    print(f"\n===== {label}: {len(ops)} ops =====")
    print(f"v3 edges kept: {len(kept)}   generic-demoted: {generic}")
    return ops, kept


tx_ops, tx = run("TxLINE", sys.argv[1])  # arg1: the OpenAPI spec to graph
c1 = any("Fixtures" in s and norm(f) == "fixtureid" and "Odds" in d for s, f, d, _, _ in tx)
c2 = any("Scores" in s and norm(f) == "seq" and "validation" in d.lower() for s, f, d, _, _ in tx)
print(f"  CHAIN 1 fixtures->odds via FixtureId : {'FOUND' if c1 else 'MISSING'}")
print(f"  CHAIN 2 scores->stat-validation seq  : {'FOUND' if c2 else 'MISSING'}")
print("  sample TxLINE edges:")
for s, f, d, p, n in tx[:10]:
    print(f"    {s[:38]:38} .{f:14} -> {d[:38]:38} ?{p}")

st_ops, st = run("Stripe", sys.argv[2])  # arg2: a rich control spec (Stripe spec3.json)
from collections import Counter
byname = Counter(n for *_ , n in st)
print("  top Stripe edge names (want NO 'created'/'status' domination):")
for k, v in byname.most_common(10):
    print(f"    {k:22} {v}")
import random
random.seed(11)
print("  random 12 Stripe edges (eyeball for false links):")
for s, f, d, p, n in random.sample(st, min(12, len(st))):
    print(f"    {s[:36]:36} .{f:16} -> {d[:36]:36} ?{p}")

print("\n=== GATE ===")
print(f"  TxLINE chains: {'BOTH FOUND' if (c1 and c2) else 'INCOMPLETE'}")
print(f"  Stripe edges: {len(st)} (v1 was 66,984; v2 64,699 — v3 must be a small fraction)")
