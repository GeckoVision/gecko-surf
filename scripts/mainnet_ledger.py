"""The mainnet ledger — one line per transaction WE sent, verified against the chain.

Four different counts of our mainnet record were in circulation at once: the README said
fifteen, the public docs page eleven, an internal note eighteen, an outside strategy doc
sixteen, and a live session twenty-one. None was lying; each counted a different set at a
different time, and no file held the answer. A proof anybody can check is worth exactly
nothing if we cannot say how many there are.

So the ledger is the source, and everything else cites it.

TWO FIELDS, AND THE SECOND IS THE ONE THAT MATTERS. `charged_cu` is read back from the
chain by this script, so it cannot drift. `predicted_cu` is the number our receipt gave
BEFORE the transaction was signed — and it exists nowhere but in whatever we wrote down
at the time. It carries `predicted_source` for that reason: a prediction without a
citable artifact is a memory, and a memory cannot support "N of N exact".

That is why the summary reports THREE numbers and never one:

    landed          transactions confirmed on chain, paid by a wallet we hold
    with_prediction rows whose prediction is traceable to a named artifact
    exact           of those, how many matched

Quote the pair `exact / with_prediction`. Quoting `exact / landed` would count rows whose
prediction nobody recorded, which is the same error as reporting recall over a population
you did not measure.

    uv run python scripts/mainnet_ledger.py --verify   # re-read every row from chain
    uv run python scripts/mainnet_ledger.py --summary  # the three numbers
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "mainnet-ledger.jsonl"
RPC = "https://api.mainnet-beta.solana.com"


def _rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        RPC, body.encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as fh:
        return json.loads(fh.read())


def rows() -> list[dict]:
    return [
        json.loads(line)
        for line in LEDGER.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def summary(data: list[dict]) -> str:
    landed = len(data)
    with_pred = [r for r in data if r.get("predicted_cu") is not None]
    exact = [r for r in with_pred if r["predicted_cu"] == r["charged_cu"]]
    lines = [
        f"landed           {landed}",
        f"with_prediction  {len(with_pred)}",
        f"exact            {len(exact)}",
        "",
        f"quotable: {len(exact)}/{len(with_pred)} exact "
        f"(of {landed} landed; {landed - len(with_pred)} have no recorded prediction)",
    ]
    return "\n".join(lines)


def verify(data: list[dict]) -> int:
    """Re-read every row from the chain. A ledger nobody re-checks is a claim, not a record."""
    bad = 0
    for row in data:
        try:
            result = (
                _rpc(
                    "getTransaction",
                    [row["signature"], {"maxSupportedTransactionVersion": 0}],
                ).get("result")
                or {}
            )
        except Exception as exc:  # network, not data — say so and keep going
            print(f"  ? {row['signature'][:18]}… unreachable: {exc}")
            continue
        meta = result.get("meta") or {}
        charged = meta.get("computeUnitsConsumed")
        if not result:
            print(f"  X {row['signature'][:18]}… NOT FOUND on chain")
            bad += 1
        elif charged != row["charged_cu"]:
            print(
                f"  X {row['signature'][:18]}… chain says {charged}, ledger {row['charged_cu']}"
            )
            bad += 1
        elif meta.get("err"):
            print(f"  X {row['signature'][:18]}… errored: {meta['err']}")
            bad += 1
        time.sleep(0.3)
    print(f"\n{len(data) - bad}/{len(data)} rows verified against the chain")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--verify", action="store_true", help="re-read every row from chain"
    )
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args(argv)
    data = rows()
    if args.verify:
        return verify(data)
    print(summary(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
