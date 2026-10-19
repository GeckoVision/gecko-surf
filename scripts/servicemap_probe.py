"""Founder-run: tier 1 probe of the registry's x402 routes. Never pays.

    uv run --extra events python scripts/servicemap_probe.py --only scrapecreators
    uv run --extra events python scripts/servicemap_probe.py --only fiber --write
    uv run --extra events python scripts/servicemap_probe.py \\
        --confirm-pay-to scrapecreators=<PAYTO> --write    # lift a quarantine

Each probe is ONE unpaid request to an operation's x402 route; only the response
headers are read (the 402 challenge), never the body. At most one request per second per
host and ``--limit`` probes per run (default 200). Operations come from ``gecko_registry``
(run ``scripts/servicemap_import.py --write`` first).

Dry by default: it reads the registry and probes, but writes what it saw into memory and
prints it. ``--write`` stores offers, health and events in ``gecko_registry`` (keychain
slot ``MONGODB_URI``, never ``.env``, never printed), refused while the kill switch is
engaged. A payTo that changed is QUARANTINED: the payer refuses that operation until a
probe past the 24h hold sees the same address again, or ``--confirm-pay-to``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.anyquery.store import StoreError  # noqa: E402
from gecko.orthogonal.spec import X402_HOST  # noqa: E402
from gecko.servicemap.pins import confirm_pay_to, pinned_pay_tos  # noqa: E402
from gecko.servicemap.observe import sweep  # noqa: E402
from gecko.servicemap.probe import DEFAULT_LIMIT  # noqa: E402
from gecko.servicemap.registry import (  # noqa: E402
    MemoryRegistry,
    Registry,
    RegistryError,
)

#: Which host serves each catalog's x402 routes. Data, not engine: the probe names none.
#: Orthogonal's is the one a hosted payer pays.
X402_HOSTS = {"orthogonal": X402_HOST}


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", default=None, help="one service (api slug)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--confirm-pay-to",
        default=None,
        metavar="API=PAYTO",
        help="founder confirms a quarantined payTo (needs --write); no probe is sent",
    )
    parser.add_argument("--write", action="store_true")
    return parser.parse_args(argv)


def _mongo_registry() -> Registry:
    from gecko.anyquery.mongo import mongo_store_from_keychain
    from gecko.servicemap.mongo import MongoRegistry

    store = mongo_store_from_keychain()
    return MongoRegistry(store.client, unavailable=store.unavailable)


def main(argv: list[str] | None = None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if args.confirm_pay_to is not None:
        api, sep, pay_to = args.confirm_pay_to.partition("=")
        if not sep or not api or not pay_to:
            print("--confirm-pay-to takes API=PAYTO", file=sys.stderr)
            return 2
        if not args.write:
            print("--confirm-pay-to changes the registry: add --write", file=sys.stderr)
            return 2
    try:
        held = _mongo_registry()
        if args.confirm_pay_to is not None:
            lifted = confirm_pay_to(held, api, pay_to, at=now)
            print(f"lifted {len(lifted)} quarantined operation(s) of {api}")
            return 0 if lifted else 1
        target = held if args.write else MemoryRegistry()
        report = sweep(
            read=held,
            write=target,
            hosts=X402_HOSTS,
            at=now,
            only=args.only,
            limit=args.limit,
        )
        pinned = pinned_pay_tos(target)
    except (RegistryError, StoreError) as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 1

    print("WROTE to gecko_registry" if args.write else "DRY RUN (nothing written)")
    for line in report.lines():
        print(f"  {line}")
    print("  pinnable Solana USDC payTos (not quarantined):")
    for pay_to, services in pinned.items():
        print(f"    {pay_to}  {', '.join(services)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
