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

from .pda import (
    ConstantPdaSeedNode,
    PdaNode,
    PdaSeed,
    ResolverPdaSeedNode,
    SeedEncoding,
    VariablePdaSeedNode,
)

__all__ = ["extract_seed_consts", "from_source"]

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
