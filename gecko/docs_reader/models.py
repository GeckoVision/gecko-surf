"""Data contracts crossing the parser / html / emit boundary.

Kept deliberately small and dataclass-only: a ``ParsedPage`` is the already-fetched
raw material the *pure* parser consumes (so the parser never touches a browser or a
network), and ``CandidateOp`` / ``CandidateParam`` are what it produces — the draft
surface the emitter turns into OpenAPI. Every field the parser could not pin down
carries a ``confidence`` + ``review`` note so a human knows exactly what to check.

Productized from ``spikes/docs_reader/models.py`` unchanged — the spike's contracts
were already the right shape; only the location changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Confidence = Literal["high", "medium", "low"]
Transport = Literal["jsonrpc", "rest"]


@dataclass
class Table:
    """A rendered doc table (header row + body rows), source-order preserved."""

    headers: list[str]
    rows: list[list[str]]


@dataclass
class PageNode:
    """One node of a page's content, in DOM order.

    Document order is the load-bearing signal: a param table belongs to the curl
    that follows it in the same section, and the preceding heading names the
    operation. Flattening these into separate lists loses that association (the bug
    that made every op inherit the first table's description).
    """

    kind: Literal["heading", "code", "table"]
    text: str = ""  # heading text, or code-block text
    table: Table | None = None


@dataclass
class ParsedPage:
    """Everything the parser needs from one rendered doc page — no browser here.

    An ordered stream of headings / code blocks (``<pre>``) / tables. Populated from
    an HTML document by ``docs_reader.html``, or from a committed fixture in tests.
    """

    url: str
    nodes: list[PageNode] = field(default_factory=list)

    @property
    def code_blocks(self) -> list[str]:
        return [n.text for n in self.nodes if n.kind == "code"]


@dataclass
class CandidateParam:
    name: str
    type: str  # json-schema-ish: string | array | object | boolean | integer | number
    required: bool
    location: Literal["body", "query"] = "body"
    description: str = ""
    default: str | None = None
    enum: list[str] | None = None
    confidence: Confidence = "high"
    review: str = ""  # non-empty => a human should confirm this field


@dataclass
class CandidateOp:
    """One recovered operation, pre-OpenAPI.

    For JSON-RPC ops ``http_path`` is the *wire* path (several methods can share it)
    and ``jsonrpc_method`` is the envelope method; the emitter gives each its own
    virtual ``/{operationId}`` route. ``params`` order is the positional envelope
    order for JSON-RPC.
    """

    operation_id: str
    http_method: str  # GET | POST
    http_path: str  # wire path, e.g. /api/v1/bundles
    host: str  # scheme://host the example targeted
    transport: Transport
    jsonrpc_method: str | None = None
    params: list[CandidateParam] = field(default_factory=list)
    summary: str = ""
    response_example: object | None = None
    confidence: Confidence = "high"
    review: list[str] = field(default_factory=list)
    source_url: str = ""
