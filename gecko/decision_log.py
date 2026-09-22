"""The decision log: one row per run Gecko carried, served or refused, joined to its end.

The mainnet ledger records what LANDED. It cannot say what Gecko stopped, and a log of
stops alone cannot say what Gecko let through. So every run gets a row here, and every row
ends in one terminal fact:

* ``landed``    — every transaction the run meant to send confirmed; their signatures.
* ``refused``   — Gecko (or a party it asked) said no, by name. ``code`` is the SAME string
  the runner prints on the refusal line (``REFUSED [code]``), never a second vocabulary.
  ``signatures`` still lists any transaction of the run that landed before the stop.
* ``abandoned`` — the run stopped itself before asking anyone to sign (a pre-check), with
  the step it stopped at.

What a row carries, and what it does not. Date, network, lane, the programs the run
called, the step and party where it ended, the code, and the amount at stake per mint in
that mint's raw units, with USD at face value for the two stablecoins and ``null`` for
anything else (a price we did not read is not a price). No wallet address, no recipient,
no intent text, no transaction bytes. For a refused run the amount never became chain
state, so it is our record, not the chain's: the file is written under ``private/``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from .trace import Trace

__all__ = ["DEFAULT_PATH", "Terminal", "append_decision", "decision_row"]

DEFAULT_PATH = Path("private/decision-log.jsonl")

Terminal = Literal["landed", "refused", "abandoned"]

#: The mints whose raw amount has a face value in USD. Nothing else is priced here.
_FACE_VALUE: Mapping[str, tuple[str, int]] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USDC", 6),
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH": ("USDG", 6),
}


def _amount(mint: str, raw: int, decimals: int) -> dict[str, Any]:
    face = _FACE_VALUE.get(mint)
    usd = (
        round(raw / 10 ** face[1], 6)
        if face is not None and face[1] == decimals
        else None
    )
    return {
        "mint": mint,
        "symbol": face[0] if face else None,
        "raw": int(raw),
        "decimals": int(decimals),
        "usd": usd,
    }


def decision_row(
    *,
    trace: Trace,
    lane: str,
    network: str,
    programs: Sequence[str],
    terminal: Terminal,
    code: str | None = None,
    signatures: Sequence[str] = (),
    at_stake: Iterable[tuple[str, int, int]] = (),
    now: float | None = None,
) -> dict[str, Any]:
    """Fold one run into a row. ``at_stake`` is ``(mint, raw, decimals)`` per mint."""
    if terminal == "landed" and (code is not None or not signatures):
        raise ValueError("a landed run carries its signatures and no refusal code")
    if terminal != "landed" and not code:
        raise ValueError("a run that did not land names why, with the code it printed")
    moment = time.time() if now is None else now
    stopped = trace.refused
    last = trace.rows[-1] if trace.rows else None
    ended = stopped if stopped is not None else last
    return {
        "decision_id": str(uuid.uuid4()),
        "date": time.strftime("%Y-%m-%d", time.gmtime(moment)),
        "network": network,
        "lane": lane,
        "programs": list(programs),
        "steps": [f"{row.step}:{row.outcome}" for row in trace.rows],
        "terminal": terminal,
        "code": code,
        "ended_at": ended.step if ended is not None else None,
        "ended_by": ended.party if ended is not None else None,
        # Every transaction of this run that LANDED, whatever the terminal fact: a run that
        # opened a position and was refused on the deposit still moved money once.
        "signatures": list(signatures),
        "at_stake": [_amount(mint, raw, dec) for mint, raw, dec in at_stake],
    }


def append_decision(row: Mapping[str, Any], path: str | Path = DEFAULT_PATH) -> Path:
    """Append one row. The directory is created; nothing is ever rewritten."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row), sort_keys=True) + "\n")
    return target
