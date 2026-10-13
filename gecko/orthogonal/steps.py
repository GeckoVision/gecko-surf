"""One free call outside Orthogonal, as the field ledger keeps it: names and counts.

Shared by every job that reads a public source beside its paid calls (the supplier
watch's GLEIF lookup, the living file's RPC, Jupiter and legal-page reads). It carries
field NAMES and a row count, never a value, so a ledger row cannot leak what was read.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PublicStep:
    path: str
    input_fields: tuple[str, ...]
    http_status: int | None
    output_fields: tuple[str, ...]
    rows: int
    error: str | None = None
