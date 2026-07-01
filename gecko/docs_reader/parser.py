"""Pure, offline docs->surface extractor — the falsifiable core of docs_reader.

Given an already-fetched page as an ordered stream of headings / code blocks /
tables, pull candidate operations ``{operationId, method, path, params[]}``. No
browser, no network, no LLM: deterministic Gecko-domain logic, unit-tested on
committed fixtures.

Two signals, fused by DOCUMENT ORDER:
  * the ``curl`` example — HTTP method, wire URL, and (for JSON-RPC) the envelope
    ``method`` + positional ``params`` shape from real values; and
  * the nearest preceding param table + section heading in the same section —
    which give per-op required/optional/default/enum + the human descriptions and
    discovery keywords a raw method name lacks.

What the docs genuinely don't pin down (semantic names for positional JSON-RPC
args, which some APIs label generically "params") is kept faithful to the docs' own
label and flagged ``low`` for review — never invented.

Treat all input as untrusted: parse defensively, never eval, cap what we read.

Productized from ``spikes/docs_reader/parser.py``.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from .models import CandidateOp, CandidateParam, PageNode, ParsedPage, Table

_CURL_URL = re.compile(r"\bcurl\s+(https?://\S+)", re.IGNORECASE)
_CURL_METHOD = re.compile(r"-X\s+([A-Z]+)")
# -d '<payload>' — payloads are JSON in double quotes, so the single quote is a
# safe shell delimiter; take everything to the final quote (DOTALL, greedy).
_CURL_DATA = re.compile(r"-d\s+'(.*)'", re.DOTALL)

_MAX_BLOCK_CHARS = 200_000  # defensive cap on any single untrusted block


def _camel(segment: str) -> str:
    parts = re.split(r"[_\-\s]+", segment.strip())
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _humanize(identifier: str) -> str:
    """``sendBundle`` -> ``Send bundle`` — so the lexical catalog can tokenize it.

    Method names are single camelCase tokens; a raw dump is undiscoverable by the
    word-based catalog. Splitting them into words is real docs->question shaping.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", identifier)
    spaced = spaced.replace("_", " ").replace("-", " ").strip()
    if not spaced:
        return identifier
    return spaced[0].upper() + spaced[1:].lower()


def _first_sentence(text: str, cap: int = 160) -> str:
    text = text.strip()
    text = re.sub(r"^(REQUIRED|OPTIONAL):\s*", "", text)
    head = text.split(".")[0].strip()
    return head[:cap]


def _clean_heading(text: str) -> str:
    """Drop leading emoji/symbols and a trailing colon from a section heading."""
    text = re.sub(r"[^\w\s()/:-]", " ", text)  # strip emoji & decorative glyphs
    return re.sub(r"\s+", " ", text).strip().rstrip(":")


def _host_of(url: str) -> str:
    s = urlsplit(url)
    return f"{s.scheme}://{s.hostname}" if s.hostname else ""


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _is_param_table(table: Table | None) -> bool:
    return bool(table) and any(h.strip().lower() == "parameter" for h in table.headers)  # type: ignore[union-attr]


def _curl_body(block: str) -> dict[str, Any] | None:
    m = _CURL_DATA.search(block)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(1).strip())
    except (ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _encoding_enrichment(
    table: Table | None,
) -> tuple[str | None, list[str] | None, str]:
    """Read the ``encoding`` row of this op's param table (default / enum / desc).

    Robust because "encoding" is an unambiguous key. Returns (default, enum, desc).
    """
    if table is None:
        return None, None, ""
    for row in table.rows:
        if row and row[0].strip().lower() == "encoding":
            desc = row[-1] if row else ""
            default = None
            dm = re.search(r"Default:\s*([A-Za-z0-9]+)", desc)
            if dm:
                default = dm.group(1)
            enum = re.findall(r"base64|base58", desc)
            return default, (sorted(set(enum)) or None), desc.strip()
    return None, None, ""


def _positional_param_desc(table: Table | None) -> str:
    """Description of this op's leading positional ``params`` arg, if the table has it."""
    if table is None:
        return ""
    for row in table.rows:
        if row and row[0].strip().lower() == "params" and len(row) >= 2:
            return row[-1].strip()
    return ""


def _params_from_jsonrpc(
    envelope_params: list[Any], table: Table | None
) -> list[CandidateParam]:
    """Map a positional JSON-RPC ``params`` array to named CandidateParams.

    Object elements expose real names (e.g. ``{"encoding": ...}``); scalar/array
    elements are positional and unnamed in the docs (some APIs label them all
    "params"), so we keep that documented label and flag it ``low`` for a human to
    rename.
    """
    default_enc, enum_enc, enc_desc = _encoding_enrichment(table)
    pos_desc = _positional_param_desc(table)
    out: list[CandidateParam] = []
    positional_idx = 0
    for element in envelope_params:
        if isinstance(element, dict):
            for key, value in element.items():
                is_encoding = key == "encoding"
                out.append(
                    CandidateParam(
                        name=key,
                        type=_json_type(value),
                        required=False,  # object-wrapped fields are optional
                        description=enc_desc if is_encoding else "",
                        default=default_enc if is_encoding else None,
                        enum=enum_enc if is_encoding else None,
                        confidence="high",  # name came straight from the example
                    )
                )
            continue
        name = "params" if positional_idx == 0 else f"param{positional_idx}"
        positional_idx += 1
        out.append(
            CandidateParam(
                name=name,
                type=_json_type(element),
                required=True,
                description=pos_desc,
                confidence="low",
                review=(
                    "positional JSON-RPC arg — docs use the generic label 'params'; "
                    "confirm a semantic name (e.g. transactions/transaction/bundleIds)"
                ),
            )
        )
    return out


def _summary(base: str, heading: str, hint: str) -> str:
    """Assemble a question-shaped summary from the method name + section + hint."""
    parts = [f"{base}."]
    clean = _clean_heading(heading)
    if clean and clean.lower() not in base.lower():
        parts.append(f"{clean}.")
    if hint:
        parts.append(f"{hint}.")
    return " ".join(parts)


def _op_from_curl(
    block: str, heading: str, table: Table | None, source_url: str
) -> CandidateOp | None:
    block = block[:_MAX_BLOCK_CHARS]
    url_match = _CURL_URL.search(block)
    if not url_match:
        return None
    raw_url = url_match.group(1).rstrip("'\"")
    split = urlsplit(raw_url)  # urlsplit.hostname drops an explicit :443
    path = split.path or "/"
    host = _host_of(raw_url)
    body = _curl_body(block)
    method_match = _CURL_METHOD.search(block)
    http_method = method_match.group(1) if method_match else ("POST" if body else "GET")

    if body and isinstance(body.get("method"), str):
        rpc_method = body["method"]
        envelope = body.get("params")
        params = (
            _params_from_jsonrpc(envelope, table) if isinstance(envelope, list) else []
        )
        hint = _first_sentence(params[0].description) if params else ""
        return CandidateOp(
            operation_id=rpc_method,
            http_method="POST",
            http_path=path,
            host=host,
            transport="jsonrpc",
            jsonrpc_method=rpc_method,
            params=params,
            summary=_summary(
                f"{_humanize(rpc_method)}. JSON-RPC `{rpc_method}`", heading, hint
            ),
            confidence="high",
            source_url=source_url,
        )

    # No JSON-RPC envelope -> plain REST. Synthesize an id from the last path segment.
    segments = [seg for seg in path.split("/") if seg]
    tail = segments[-1] if segments else "root"
    prefix = "get" if http_method == "GET" else http_method.lower()
    op_id = prefix + _camel(tail)
    return CandidateOp(
        operation_id=op_id,
        http_method=http_method,
        http_path=path,
        host=host,
        transport="rest",
        summary=_summary(f"{_humanize(op_id)}. REST {http_method} {path}", heading, ""),
        confidence="medium",
        review=[f"operationId '{op_id}' synthesized from the URL path; confirm a name"],
        source_url=source_url,
    )


def _maybe_json(block: str) -> Any | None:
    stripped = block.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except (ValueError, json.JSONDecodeError):
        return None


def parse_page(page: ParsedPage) -> list[CandidateOp]:
    """Extract candidate operations from one page's ordered node stream.

    Walks nodes in document order, associating each curl with the nearest preceding
    param table + section heading, deduping base64/base58 example variants, and
    attaching the first following JSON block as the operation's response example.
    """
    ops: list[CandidateOp] = []
    by_key: dict[str, CandidateOp] = {}
    heading = ""
    req_table: Table | None = None
    last_op: CandidateOp | None = None

    for node in page.nodes:
        if node.kind == "heading":
            heading = node.text
            req_table = None  # a new section: don't carry a stale table across it
            continue
        if node.kind == "table":
            if _is_param_table(node.table):
                req_table = node.table
            continue
        # code node
        if node.text.lstrip().lower().startswith("curl"):
            op = _op_from_curl(node.text, heading, req_table, page.url)
            req_table = None
            if op is None:
                continue
            key = op.jsonrpc_method or f"{op.http_method} {op.http_path}"
            if key in by_key:
                last_op = by_key[key]
                continue  # base64/base58 duplicate — same method+path+shape
            by_key[key] = op
            ops.append(op)
            last_op = op
        elif last_op is not None and last_op.response_example is None:
            parsed = _maybe_json(node.text)
            if parsed is not None:
                last_op.response_example = parsed
    return ops


def detect_uuid_auth(pages: list[ParsedPage]) -> dict[str, str] | None:
    """Recover the optional ``*-auth`` UUID scheme from the auth note, if present.

    Returns an apiKey scheme descriptor (header name) so the emitter can surface it
    as *optional* security, or None if no such note was rendered.
    """
    for page in pages:
        for block in page.code_blocks:
            header_match = re.search(r"([a-z][a-z0-9-]*-auth)\b", block, re.IGNORECASE)
            if header_match and "header" in block.lower():
                return {"in": "header", "name": header_match.group(1)}
    return None


def parsed_page_from_nodes(url: str, raw_nodes: list[dict[str, Any]]) -> ParsedPage:
    """Build a ParsedPage from raw node dicts (the html extractor's / a fixture's)."""
    nodes: list[PageNode] = []
    for raw in raw_nodes:
        kind = raw.get("kind")
        if kind == "table":
            nodes.append(
                PageNode(
                    kind="table",
                    table=Table(
                        headers=[str(h) for h in raw.get("headers", [])],
                        rows=[[str(c) for c in row] for row in raw.get("rows", [])],
                    ),
                )
            )
        elif kind in ("code", "heading"):
            nodes.append(PageNode(kind=kind, text=str(raw.get("text", ""))))
    return ParsedPage(url=url, nodes=nodes)
