"""A run that writes down what it did, so a graph can be drawn from the run itself.

Every purchase lane in this repo is a fixed sequence of steps, each taken by one party:
Gecko prepares and verifies, a builder assembles, a relay signs as fee payer, a buyer
signs as authority, a node simulates and lands. A diagram of that sequence drawn by hand
is a claim; one generated from a run is a measurement. This module is the measurement.

WHAT A ROW CARRIES, AND WHAT IT NEVER CARRIES. A row is a step name, the party that took
it, how it ended (``ok`` or a refusal code), how long it took, the compute units it
reported, and the first sixteen characters of a binding when one exists. Nothing else.
No transaction bytes, no key, no address beyond its first eight characters, no node
payload, no log line. Control-plane invariant #1 applies to a trace exactly as it applies
to a receipt: it must be safe to publish, and it is safe by construction rather than by
review, because :class:`TraceRow` has no field that could hold any of those things.

WHY A CONTEXT MANAGER. ``with trace.step("sponsor", "relay"):`` measures the wall clock
and records the outcome on the way out, including when the body raises: a
:class:`RelayRefused`, :class:`SignerRefused` or :class:`CosignRefused` carries a
``code``, and the row records that code and re-raises. A refusal is then a node in the
graph with its code on it, which is the whole reason to draw the graph.

The sink is injected. A lane takes ``trace: Trace | None`` and does nothing when it is
``None``, so no production path pays for tracing it did not ask for.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

__all__ = ["Party", "Trace", "TraceRow", "short"]

#: Who took the step. A closed set, because a lane gets a swim lane per party.
Party = Literal["gecko", "builder", "relay", "buyer", "node", "store"]

#: How many characters of an address or a binding a row may carry.
SHORT = 8


def short(value: str | None) -> str | None:
    """The first :data:`SHORT` characters, or ``None``. Never the whole thing."""
    if not value:
        return None
    return value[:SHORT]


@dataclass(frozen=True)
class TraceRow:
    """One step of one run. Every field is a value; none can hold a payload."""

    seq: int
    step: str
    party: Party
    outcome: str
    ms: int
    units: int | None = None
    binding_prefix: str | None = None
    note: str | None = None


@dataclass
class Trace:
    """The rows of one run, in order, plus the facts about the run they belong to.

    ``lane`` names which path produced them (``rehearsal`` or ``settle``), ``network``
    the chain the run was pointed at. Both travel into the graph's title.
    """

    lane: str
    network: str
    rows: list[TraceRow] = field(default_factory=list)
    started: float = field(default_factory=time.time)

    def record(
        self,
        step: str,
        party: Party,
        outcome: str = "ok",
        *,
        ms: int = 0,
        units: int | None = None,
        binding_prefix: str | None = None,
        note: str | None = None,
    ) -> TraceRow:
        row = TraceRow(
            seq=len(self.rows),
            step=step,
            party=party,
            outcome=outcome,
            ms=ms,
            units=units,
            binding_prefix=binding_prefix,
            note=note,
        )
        self.rows.append(row)
        return row

    @contextmanager
    def step(
        self, name: str, party: Party, *, note: str | None = None
    ) -> Iterator[dict[str, Any]]:
        """Time a step and record how it ended, even when it raises.

        The yielded dict lets the body attach ``units`` and ``binding_prefix`` once it
        knows them (``facts["units"] = receipt.units_consumed``). An exception with a
        ``code`` attribute records that code as the outcome; any other exception records
        its type name. Both re-raise: a trace observes, it never swallows.
        """
        facts: dict[str, Any] = {}
        began = time.monotonic()
        try:
            yield facts
        except BaseException as exc:
            code = getattr(exc, "code", None)
            outcome = str(code) if code else type(exc).__name__
            self.record(
                name,
                party,
                outcome,
                ms=int((time.monotonic() - began) * 1000),
                units=facts.get("units"),
                binding_prefix=facts.get("binding_prefix"),
                note=note,
            )
            raise
        self.record(
            name,
            party,
            str(facts.get("outcome", "ok")),
            ms=int((time.monotonic() - began) * 1000),
            units=facts.get("units"),
            binding_prefix=facts.get("binding_prefix"),
            note=facts.get("note", note),
        )

    @property
    def refused(self) -> TraceRow | None:
        """The first row that did not end ``ok``, or ``None``."""
        return next((row for row in self.rows if row.outcome != "ok"), None)

    def to_jsonl(self) -> str:
        header = {"lane": self.lane, "network": self.network, "started": self.started}
        lines = [json.dumps(header, sort_keys=True)]
        lines.extend(json.dumps(asdict(row), sort_keys=True) for row in self.rows)
        return "\n".join(lines) + "\n"

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(self.to_jsonl())
        return target

    @classmethod
    def read(cls, path: str | Path) -> Trace:
        lines = [line for line in Path(path).read_text().splitlines() if line.strip()]
        if not lines:
            raise ValueError(f"{path} is empty")
        header = json.loads(lines[0])
        trace = cls(
            lane=str(header.get("lane", "?")),
            network=str(header.get("network", "?")),
            started=float(header.get("started", 0.0)),
        )
        for line in lines[1:]:
            raw = json.loads(line)
            trace.rows.append(TraceRow(**raw))
        return trace
