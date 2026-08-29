"""Write the prediction down beside the signature, at the moment both exist.

The prediction and the signature are known about two seconds apart and by the same
process, and for eighteen mainnet transactions nobody wrote them down together. Sixteen
of those eighteen were predicted correctly and cannot prove it: the number scrolled past
in a terminal and the terminal closed. The claim "N transactions, N exact" then rested on
memory, which is the one thing a company selling verifiable receipts cannot offer.

So this is not analytics. It is the artifact that makes the claim checkable, appended at
the only instant where both halves are in the same place.

CONTROL PLANE ONLY, and the line is not blurry here. A signature is public the moment it
lands, compute is public, a slot is public — this records facts about OUR OWN
transaction that anyone can already read off the chain. It records no payload, no
account balance, no key, and nothing belonging to a customer. What it adds to the public
record is the ONE fact the chain does not carry: what we said the cost would be BEFORE we
signed. That fact exists nowhere else, which is exactly why it has to be written here.

Failure is never fatal. A ledger that can abort a send would make recording more
dangerous than not recording, and the transaction is already irreversible by the time we
get here — refusing to write it down does not un-send it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["LEDGER_ENV", "LedgerRow", "default_path", "record"]

#: Override for tests and for a founder who keeps the ledger outside the repo.
LEDGER_ENV = "GECKO_MAINNET_LEDGER"

_DEFAULT = Path("docs/mainnet-ledger.jsonl")


@dataclass(frozen=True)
class LedgerRow:
    """One landed transaction, and what we predicted before it landed."""

    signature: str
    predicted_cu: int | None
    predicted_source: str
    network: str
    program: str | None = None
    date: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "signature": self.signature,
            "date": self.date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "network": self.network,
            "program": self.program,
            "predicted_cu": self.predicted_cu,
            "predicted_source": self.predicted_source,
            # charged_cu is deliberately absent: `scripts/mainnet_ledger.py --verify`
            # reads it from the chain. A number we write ourselves and then check
            # against itself proves nothing, which is the same self-referential trap
            # `sign_and_send` documents about its own receipt.
        }


def default_path() -> Path:
    override = os.environ.get(LEDGER_ENV, "").strip()
    return Path(override).expanduser() if override else _DEFAULT


def record(row: LedgerRow, *, path: Path | None = None) -> Path | None:
    """Append one row. Returns where it went, or ``None`` when it could not be written.

    MAINNET ONLY. A fork signature in the mainnet ledger is the fork-is-not-mainnet
    error committed in the one file whose whole purpose is being trustworthy.
    """
    if row.network != "mainnet":
        return None
    target = path or default_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row.to_json()) + "\n")
    except OSError:
        # Never fatal — see the module docstring. The send already happened.
        return None
    return target
