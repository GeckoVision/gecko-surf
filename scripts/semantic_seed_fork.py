"""Seed the 31-item catalogue onto a surfpool fork, and read it back — $0, no key.

    # terminal 1
    surfpool start --no-tui --no-deploy --rpc-url <mainnet-rpc> --port 8899
    # terminal 2
    uv run python scripts/semantic_seed_fork.py --rpc-url http://127.0.0.1:8899 --store geckocoffee

TRANSPORT ONLY. Proves the fork, writes the store from
``gecko.semantic_catalogue.to_store_config()`` through the sanctioned overlay
cheatcode, then lists it back through the same decode path a purchase uses. No
signature, no broadcast, no mainnet: a local validator state edit that exists
only on the fork. The mainnet seed is the founder's `initialize`+`add_product`
path; this is the dev-fast one.

Exit non-zero if the fork will not accept the store or the read-back count is
wrong — a seed that does not decode is a seed that did not take.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.fork_preflight import surfnet_overlay  # noqa: E402
from gecko.sandbox.surfnet import SandboxError, prove_surfnet  # noqa: E402
from gecko.semantic_catalogue import to_store_config  # noqa: E402
from gecko.semantic_seed import SeedError, read_store_on_fork, seed_store  # noqa: E402

# The real geckocoffee authority (payee). On a fork it is just a consistent
# pubkey; a buy credits ATA(authority, USDC), created by the purchase.
GECKOCOFFEE_AUTHORITY = "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed the semantic catalogue on a fork."
    )
    parser.add_argument("--rpc-url", default="http://127.0.0.1:8899")
    parser.add_argument("--store", default="geckocoffee")
    parser.add_argument("--authority", default=GECKOCOFFEE_AUTHORITY)
    args = parser.parse_args()

    try:
        proof = prove_surfnet(args.rpc_url)
    except SandboxError as error:
        print(f"fork not proven: {error}", file=sys.stderr)
        print(
            "boot: surfpool start --no-tui --no-deploy --rpc-url <mainnet> --port 8899"
        )
        return 2

    config = to_store_config(args.store)
    overlay = surfnet_overlay(proof.rpc_url)
    try:
        address = seed_store(proof, overlay, config, authority=args.authority)
    except SeedError as error:
        print(f"seed failed: {error}", file=sys.stderr)
        return 3

    listing = read_store_on_fork(address, proof.rpc_url)
    if listing is None:
        print("read-back failed after seeding", file=sys.stderr)
        return 1

    print(f"fork proven:  {proof.rpc_url}")
    print(f"seeded store: {listing.store_name} @ {address}")
    print(f"authority:    {listing.authority}")
    print(f"products:     {len(listing.products)}")
    if len(listing.products) != len(config["items"]):
        print("MISMATCH: read-back product count != seeded count", file=sys.stderr)
        return 1
    for product in listing.products[:6]:
        print(f"  {product.name:22} {product.price_ui:>8} {product.mint[:6]}…")
    print(f"  … and {len(listing.products) - 6} more")
    print(
        "\nseeded and read back through the purchase decode path — buyable on this fork."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
