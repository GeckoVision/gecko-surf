#!/usr/bin/env python3
"""Look up a landed transaction on live mainnet and print what the chain says.

A `curl` of this would be the obvious choice, but the JSON body does not survive the
screenplay's `shlex.split`, and a mangled command on camera teaches the viewer nothing.
This does the same request with the same public endpoint, and prints only the four
fields the claim rests on.

    uv run python demo/kit/verify_tx.py <signature> [--rpc URL]

Read-only. Nothing here signs, sends, or needs a key.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/demo/", 1)[0])

from gecko.rpc import default_rpc_call  # noqa: E402

MAINNET = "https://api.mainnet-beta.solana.com"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("signature")
    parser.add_argument("--rpc", default=MAINNET)
    args = parser.parse_args(argv)

    reply = default_rpc_call(
        args.rpc,
        "getTransaction",
        [args.signature, {"maxSupportedTransactionVersion": 0, "encoding": "json"}],
    )
    result = reply.get("result")
    if not result:
        print(f"  not found on {args.rpc}")
        return 1

    meta = result.get("meta") or {}
    print(f"  slot                  {result.get('slot')}")
    print(f"  error                 {meta.get('err')}")
    print(f"  computeUnitsConsumed  {meta.get('computeUnitsConsumed')}")
    print(f"  fee (lamports)        {meta.get('fee')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
