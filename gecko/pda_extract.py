"""Recover PDA seed recipes from program SOURCE — the join the IDL/llms.txt loses.

Phase 1 of the Program Surface. An Anchor IDL silently omits a PDA's ``seeds``
whenever a seed is a helper-function output (Anchor #4057), and non-Anchor
frameworks (Steel, native) never emit an IDL at all — but the recipe is always
right there in the program source, as a ``find_program_address(&[...])`` call. This
module reads that source and produces the :class:`~gecko.pda.PdaNode` graph
:mod:`gecko.pda` derives from.

Steel/native is *machine-regular*: one const table (``pub const SEED: &[u8] = b"…";``)
and one accessor per PDA (``pub fn foo_pda(authority: Pubkey) -> (Pubkey, u8) {
Pubkey::find_program_address(&[SEED, &authority.to_bytes()], &crate::ID) }``). We
resolve each seed against the const table and the accessor's signature. Anything we
can't map (a ``max_key(a, b)`` helper, a hashed seed, an unknown type) becomes a
:class:`~gecko.pda.ResolverPdaSeedNode` — flagged with its dependencies, never
fabricated, never silently dropped.

Input is untrusted (control-plane invariant #1): we only *read* text and emit a
data model — we never execute the source.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from .pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    PdaSeed,
    ResolverPdaSeedNode,
    SeedEncoding,
    VariablePdaSeedNode,
    b58_encode,
)
from .provenance import ProgramProvenanceTier

__all__ = [
    "extract_seed_consts",
    "from_source",
    "from_anchor_idl",
    "merge_pda_nodes",
    "merge_pda_nodes_with_origin",
]

# Rust integer type -> byte width, for `.to_le_bytes()` / `.to_be_bytes()` seeds.
_INT_WIDTHS: dict[str, int] = {
    "u8": 1,
    "i8": 1,
    "u16": 2,
    "i16": 2,
    "u32": 4,
    "i32": 4,
    "u64": 8,
    "i64": 8,
    "u128": 16,
    "i128": 16,
}

# `pub const NAME: &[u8] = b"literal";` — the seed-byte constant table.
_CONST_RE = re.compile(
    r'(?:pub\s+)?const\s+(\w+)\s*:\s*&\s*\[\s*u8\s*\]\s*=\s*b"((?:[^"\\]|\\.)*)"\s*;'
)

# A function signature: `fn name(params)`. We locate PDA accessors by finding each
# `find_program_address` call and attributing it to the nearest preceding signature.
_FN_SIG_RE = re.compile(r"\bfn\s+(\w+)\s*\(([^)]*)\)")
_FPA_RE = re.compile(r"find_program_address\s*\(\s*&\[([^\]]*)\]")

# `ident.method()` seed forms.
_METHOD_RE = re.compile(
    r"(\w+)\s*\.\s*(to_le_bytes|to_be_bytes|to_bytes|as_ref|as_bytes)\s*\(\s*\)"
)
# min/max(a, b) pool-pair ordering (Meteora min/max, Anchor max_key/min_key) — the
# helper-fn seed the IDL macro drops. Operands may carry `&`; a method chain
# (`.as_ref()`, `.key().as_ref()`, `.to_bytes()`) may follow.
_ORDERED_RE = re.compile(
    r"(min|max|min_key|max_key)\s*\(\s*&?\s*(\w+)\s*,\s*&?\s*(\w+)\s*\)"
    r"(?:\s*\.\s*\w+\s*\(\s*\))*\s*"
)
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def _decode_byte_str(raw: str) -> bytes:
    """Decode the body of a Rust byte-string literal (``b"…"``) to bytes.

    Handles the common escapes (``\\xHH``, ``\\n \\t \\r \\0 \\\\ \\" \\'``); most
    seeds are plain ASCII words.
    """
    out = bytearray()
    i = 0
    n = len(raw)
    escapes = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, '"': 34, "'": 39}
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "x" and i + 3 < n:
                out.append(int(raw[i + 2 : i + 4], 16))
                i += 4
                continue
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            out.append(ord(nxt))
            i += 2
            continue
        out.append(ord(c))
        i += 1
    return bytes(out)


def _encoding_for(value: bytes) -> SeedEncoding:
    """A seed rendered as printable ASCII round-trips as ``utf8``; else ``bytes``."""
    return "utf8" if value and all(0x20 <= b < 0x7F for b in value) else "bytes"


def extract_seed_consts(source: str) -> dict[str, bytes]:
    """Recover the ``pub const NAME: &[u8] = b"…";`` seed-byte table from source."""
    return {
        m.group(1): _decode_byte_str(m.group(2)) for m in _CONST_RE.finditer(source)
    }


def _parse_params(params: str) -> dict[str, str]:
    """`authority: Pubkey, id: u64` -> {authority: 'Pubkey', id: 'u64'}."""
    out: dict[str, str] = {}
    for part in params.split(","):
        part = part.strip()
        if not part or ":" not in part or part.startswith("&"):
            continue
        name, _, ty = part.partition(":")
        name = name.strip()
        # strip a leading `mut ` binding modifier
        name = name[4:].strip() if name.startswith("mut ") else name
        if name and name != "self":
            out[name] = ty.strip()
    return out


def _split_seeds(seed_src: str) -> list[str]:
    """Split a seed array body on top-level commas (respecting nested () [] {})."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in seed_src:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _unresolved(token: str, params: dict[str, str], reason: str) -> ResolverPdaSeedNode:
    """Build an honest placeholder for a seed we couldn't map, keeping its deps."""
    idents = list(dict.fromkeys(_IDENT_RE.findall(token)))
    param_deps = tuple(i for i in idents if i in params)
    deps = param_deps or tuple(idents)
    name = param_deps[0] if param_deps else (idents[0] if idents else "seed")
    return ResolverPdaSeedNode(name=name, depends_on=deps, reason=reason)


def _variable_seed(
    ident: str, method: str, rust_type: str, params: dict[str, str]
) -> PdaSeed:
    """Map an `ident.method()` seed to a variable node, using the param's type."""
    base_ty = rust_type.lstrip("&").strip()

    if method in ("to_le_bytes", "to_be_bytes"):
        width = _INT_WIDTHS.get(base_ty)
        if width is None:
            return _unresolved(
                f"{ident}.{method}()",
                params,
                f"{ident}.{method}() but type {rust_type!r} has no known byte width",
            )
        return VariablePdaSeedNode(
            ident,
            source="argument",
            encoding="le" if method == "to_le_bytes" else "be",
            width=width,
        )

    if method == "to_bytes":  # Pubkey::to_bytes() -> 32 bytes
        return VariablePdaSeedNode(ident, source="account", encoding="pubkey")

    # as_ref() / as_bytes() — encoding depends on the parameter's type
    if base_ty == "Pubkey":
        return VariablePdaSeedNode(ident, source="account", encoding="pubkey")
    if base_ty in _INT_WIDTHS:
        return _unresolved(
            f"{ident}.{method}()",
            params,
            f"{ident}.{method}() on integer {rust_type!r} — ambiguous byte order",
        )
    # a &str / &[u8] parameter passed through as bytes
    return VariablePdaSeedNode(ident, source="account", encoding="bytes")


def _seed_from_token(
    token: str, consts: dict[str, bytes], params: dict[str, str]
) -> PdaSeed:
    """Resolve one seed expression to a constant / variable / resolver node."""
    t = token.strip().lstrip("&").strip()

    # byte-string literal: b"..."
    lit = re.fullmatch(r'b"((?:[^"\\]|\\.)*)"', t)
    if lit:
        value = _decode_byte_str(lit.group(1))
        return ConstantPdaSeedNode(value, encoding=_encoding_for(value))

    # a bare identifier naming a known seed const
    if re.fullmatch(r"\w+", t) and t in consts:
        value = consts[t]
        return ConstantPdaSeedNode(value, encoding=_encoding_for(value))

    # min/max(a, b) pair ordering — the seed Anchor's IDL macro drops (#4057), now
    # RESOLVABLE (not a resolver): sort the two operands at derive time.
    ordered = _ORDERED_RE.fullmatch(t)
    if ordered:
        select: Literal["min", "max"] = (
            "min" if ordered.group(1).startswith("min") else "max"
        )
        return OrderedPairPdaSeedNode(
            left=ordered.group(2), right=ordered.group(3), select=select
        )

    # ident.method()
    method = _METHOD_RE.fullmatch(t)
    if method:
        ident, meth = method.group(1), method.group(2)
        if ident in params:
            return _variable_seed(ident, meth, params[ident], params)
        return _unresolved(t, params, f"{ident} is not an accessor parameter")

    # a bare parameter identifier (e.g. a &[u8] slice arg used directly)
    if re.fullmatch(r"\w+", t) and t in params:
        base_ty = params[t].lstrip("&").strip()
        if base_ty == "Pubkey":
            return VariablePdaSeedNode(t, source="account", encoding="pubkey")
        return VariablePdaSeedNode(t, source="account", encoding="bytes")

    # a bare identifier that is neither a const nor a param — an unknown const
    if re.fullmatch(r"\w+", t):
        return _unresolved(
            t, params, f"unknown seed constant {t!r} (not in source const table)"
        )

    # anything else: a function call (max_key(...)), an index, arithmetic — flag it
    return _unresolved(t, params, f"unrecognized seed expression: {t}")


def from_source(source: str, program_id: str | None = None) -> dict[str, PdaNode]:
    """Recover ``{account_name: PdaNode}`` from Rust program source.

    Finds every ``find_program_address(&[...])`` call, attributes it to its
    enclosing ``fn`` (the account name is the fn name with a trailing ``_pda``
    stripped), and resolves each seed against the source const table and the fn
    signature. ``program_id`` (base58), if given, is stamped on every node.

    Note: associated-token-account derivations
    (``get_associated_token_address``) use their own scheme, not
    ``find_program_address``, so they are correctly *not* returned here — an ATA is
    a distinct edge to model later, not a PDA seed recipe.
    """
    consts = extract_seed_consts(source)
    signatures = [
        (m.start(), m.group(1), m.group(2)) for m in _FN_SIG_RE.finditer(source)
    ]

    nodes: dict[str, PdaNode] = {}
    for fpa in _FPA_RE.finditer(source):
        preceding = [s for s in signatures if s[0] < fpa.start()]
        if not preceding:
            continue
        _, fname, raw_params = preceding[-1]
        account = fname[:-4] if fname.endswith("_pda") else fname
        params = _parse_params(raw_params)
        seeds = tuple(
            _seed_from_token(tok, consts, params) for tok in _split_seeds(fpa.group(1))
        )
        if seeds:
            nodes[account] = PdaNode(name=account, seeds=seeds, program_id=program_id)
    return nodes


# --- Anchor 0.30+ IDL extraction ------------------------------------------
#
# Anchor's own docs say it plainly: "Generated clients can derive PDAs only when
# the IDL contains explicit array-form seed metadata. Opaque expression seeds ...
# prevent clients from reproducing the address derivation." An account whose seed
# is a helper-fn output therefore ships with NO `pda` block at all (Anchor #4057) —
# so from_anchor_idl recovers only what the IDL kept, and merge_pda_nodes fills the
# dropped recipes from source ("both, joined").

# IDL scalar type string -> byte width for integer arg seeds.
_IDL_INT_WIDTHS = dict(_INT_WIDTHS)


def _idl_arg_seed(path: str, arg_types: dict[str, Any]) -> PdaSeed:
    """An `{kind: arg, path}` seed, encoded from the instruction's arg type."""
    if "." in path:  # a field of an arg struct — not statically reproducible
        head = path.split(".", 1)[0]
        return ResolverPdaSeedNode(
            name=head,
            depends_on=(head,),
            reason=f"arg field seed {path!r} — runtime value",
        )
    ty = arg_types.get(path)
    if isinstance(ty, str):
        if ty in _IDL_INT_WIDTHS:
            return VariablePdaSeedNode(
                path, source="argument", encoding="le", width=_IDL_INT_WIDTHS[ty]
            )
        if ty == "pubkey":
            return VariablePdaSeedNode(path, source="argument", encoding="pubkey")
        if ty == "string":
            return VariablePdaSeedNode(path, source="argument", encoding="utf8")
        if ty == "bytes":
            return VariablePdaSeedNode(path, source="argument", encoding="bytes")
    return ResolverPdaSeedNode(
        name=path,
        depends_on=(path,),
        reason=f"arg {path!r} has unsupported seed type {ty!r}",
    )


def _idl_seed(seed: dict[str, Any], arg_types: dict[str, Any]) -> PdaSeed:
    """One Anchor-IDL seed entry -> a PdaSeed."""
    kind = seed.get("kind")
    if kind == "const":
        value = bytes(seed.get("value", []))
        encoding = _encoding_for(value)
        # A 32-byte non-printable const is a hardcoded pubkey (a program/account
        # address baked into the seed, e.g. the fee program's target program id) —
        # keep the pubkey provenance so it round-trips to base58, not a byte list.
        if encoding == "bytes" and len(value) == 32:
            encoding = "pubkey"
        return ConstantPdaSeedNode(value, encoding=encoding)
    if kind == "account":
        path = str(seed.get("path", ""))
        if "." in path:  # a field read from another account's DATA — runtime value
            head, _, field = path.partition(".")
            # named after the FIELD whose value fills the seed (`creator` for
            # `bonding_curve.creator`), depending on the account it is read from.
            return ResolverPdaSeedNode(
                name=field.split(".", 1)[0] or head,
                depends_on=(head,),
                reason=f"account field seed {path!r} — runtime data",
            )
        return VariablePdaSeedNode(path, source="account", encoding="pubkey")
    if kind == "arg":
        return _idl_arg_seed(str(seed.get("path", "")), arg_types)
    # `program` seed (the program id itself) or anything unexpected: flag, don't guess
    return ResolverPdaSeedNode(
        name=kind or "seed",
        depends_on=(),
        reason=f"IDL seed kind {kind!r} not statically resolvable",
    )


def _idl_pda_program(
    pda: dict[str, Any], pinned: dict[str, str], default: str | None
) -> str | None:
    """The program a PDA derives under, honoring the IDL's ``pda.program`` field.

    Anchor 0.30 records cross-program PDAs (an ATA, a fee-program config) with a
    ``program`` entry: ``const`` (the 32 program-id bytes inline) or ``account``
    (derive under whatever program account is passed — resolvable only when the
    IDL pins that account's ``address``). An unpinnable program yields ``None``
    (the honest unknown; :func:`~gecko.pda.derive_pda` then demands an explicit
    program), never a plausible-but-wrong default — deriving under the *owning*
    program id when the real one is foreign is exactly the silent-wrong-address
    class this module exists to kill.
    """
    prog = pda.get("program")
    if not isinstance(prog, dict):
        return default
    kind = prog.get("kind")
    if kind == "const":
        value = bytes(prog.get("value", []))
        return b58_encode(value) if len(value) == 32 else None
    if kind == "account":
        return pinned.get(str(prog.get("path", "")))
    return None


def from_anchor_idl(idl: dict[str, Any]) -> dict[str, PdaNode]:
    """Anchor 0.30+ IDL -> ``{account_name: PdaNode}`` for every account that carries
    a ``pda.seeds`` block.

    ``program_id`` is ``idl["address"]`` unless the account's ``pda.program``
    entry says the PDA derives under a FOREIGN program (an ATA, a fee-program
    config): a ``const`` program is decoded to its base58 id, an ``account``
    program resolves through the instruction's pinned account addresses, and an
    unpinnable one leaves ``program_id=None`` (honest unknown). Arg seeds are
    encoded from each instruction's ``args`` types; a seed that reads another
    account's field (``path`` with a ``.``) is a runtime-data seed and becomes an
    honest resolver.

    Accounts whose ``pda`` block Anchor *dropped* (the #4057 case) are simply absent
    here — that gap is filled by :func:`merge_pda_nodes` from source recovery.
    """
    program_id = idl.get("address") or (idl.get("metadata") or {}).get("address")
    nodes: dict[str, PdaNode] = {}
    for ix in idl.get("instructions", []):
        arg_types = {a.get("name"): a.get("type") for a in ix.get("args", [])}
        # accounts whose address the IDL pins (e.g. a fee/metadata program passed as
        # an account) — these resolve an `account`-kind pda.program to a real id.
        pinned = {
            str(a.get("name")): str(a.get("address"))
            for a in ix.get("accounts", [])
            if isinstance(a, dict) and a.get("address")
        }
        for acct in ix.get("accounts", []):
            pda = acct.get("pda")
            name = acct.get("name")
            if not pda or not name or name in nodes:
                continue
            seeds = tuple(_idl_seed(s, arg_types) for s in pda.get("seeds", []))
            if seeds:
                nodes[name] = PdaNode(
                    name=name,
                    seeds=seeds,
                    program_id=_idl_pda_program(pda, pinned, program_id),
                )
    return nodes


def merge_pda_nodes_with_origin(
    idl_nodes: dict[str, PdaNode], source_nodes: dict[str, PdaNode]
) -> tuple[dict[str, PdaNode], dict[str, ProgramProvenanceTier]]:
    """Join IDL-recovered and source-recovered PDA graphs — "both, joined" — and
    say, per account, WHICH input won.

    IDL gives breadth (all instructions' accounts + arg-typed seeds); source fills
    the exact recipes the IDL dropped (#4057) and resolves opaque IDL seeds. Rule,
    per account name:

    - in IDL only  -> keep IDL (authoritative array-form seeds);
    - in source only -> use source (the IDL omitted the account's ``pda`` entirely);
    - in both -> keep IDL if it is :attr:`~gecko.pda.PdaNode.resolvable`; otherwise
      take the source node when *it* resolves the seeds the IDL left opaque.

    Returns ``(merged, origin)`` where ``origin[name]`` is ``"extracted"`` (the
    kept recipe is the IDL's) or ``"recovered"`` (source's won). This is the ONE
    place that fact is decided, so it is the one place it is recorded — a consumer
    that re-derives it from the merged nodes is duplicating an invariant that will
    drift. ``"manual"`` (the third tier) never originates here: it is stamped by
    whoever applies an explicit overlay on top of this join.
    """
    merged: dict[str, PdaNode] = dict(idl_nodes)
    origin: dict[str, ProgramProvenanceTier] = dict.fromkeys(idl_nodes, "extracted")
    for name, snode in source_nodes.items():
        inode = merged.get(name)
        if inode is None or (not inode.resolvable and snode.resolvable):
            merged[name] = snode
            origin[name] = "recovered"
    return merged, origin


def merge_pda_nodes(
    idl_nodes: dict[str, PdaNode], source_nodes: dict[str, PdaNode]
) -> dict[str, PdaNode]:
    """The merged recipes alone. Prefer :func:`merge_pda_nodes_with_origin` — a
    caller that only takes the nodes cannot tell an IDL-stated recipe from one
    regex-recovered out of untrusted source text."""
    return merge_pda_nodes_with_origin(idl_nodes, source_nodes)[0]
