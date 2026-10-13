"""Owner-run: mint, list or revoke read-only keys on ONE of your datasets.

    GECKO_KEY=<your gecko_sk_ key> uv run --extra events python \\
        scripts/dataset_reader_keys.py mint --dataset lead-contacts --label analyst
    ... scripts/dataset_reader_keys.py list
    ... scripts/dataset_reader_keys.py revoke --key-id <key_id from list>

Owner-only: the account is the one YOUR key proves (``GECKO_KEY``, else a hidden
prompt), never a flag, so nobody mints on a dataset they do not own. A reader key reads
that one dataset, masked, and reaches no paying tool. It is printed once by ``mint`` and
never again; the registry keeps only its hash. ``list`` and ``revoke`` use ``key_id``, a
non-secret handle. The MongoDB connection string comes from the keychain only.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from gecko.anyquery.reader_keys import (
    READER_COLLECTION,
    MongoReaderKeyRegistry,
    ReaderKeyError,
    ReaderKeyRegistry,
    mint_reader_key,
    revoke_reader_key,
)
from gecko.anyquery.store import Store, StoreError
from gecko.keyregistry import GeckoKeyResolver, KeyRegistry


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    mint = sub.add_parser("mint")
    mint.add_argument("--dataset", required=True)
    mint.add_argument("--label", default="reader")
    sub.add_parser("list")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--key-id", required=True)
    return parser.parse_args(argv)


def _live() -> tuple[Store, KeyRegistry, ReaderKeyRegistry]:
    from gecko.anyquery.mongo import mongo_store_from_keychain
    from gecko.keyregistry import _MONGO_COLLECTION, _MONGO_DB, MongoKeyRegistry

    mongo = mongo_store_from_keychain()
    db = mongo.client[_MONGO_DB]
    return (
        mongo,
        MongoKeyRegistry(db[_MONGO_COLLECTION]),
        MongoReaderKeyRegistry(db[READER_COLLECTION]),
    )


def main(
    argv: list[str] | None = None,
    *,
    store: Store | None = None,
    keys: KeyRegistry | None = None,
    readers: ReaderKeyRegistry | None = None,
    owner_key: str | None = None,
) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    presented = (
        owner_key or os.environ.get("GECKO_KEY") or getpass.getpass("Your key: ")
    )
    try:
        if store is None or keys is None or readers is None:
            store, keys, readers = _live()
        owner = GeckoKeyResolver(keys)(presented.strip())
        if owner is None:
            print("STOP: that is not an enabled account key", file=sys.stderr)
            return 1
        if args.command == "mint":
            key, grant = mint_reader_key(
                readers, store, owner=owner, dataset_id=args.dataset, label=args.label
            )
            print(
                f"Reader key {grant.key_id} on {grant.dataset_id} (read only, masked)."
            )
            print("Shown ONCE; it is never stored in plaintext:\n")
            print(f"  {key}\n")
            print(f"Revoke with: dataset_reader_keys.py revoke --key-id {grant.key_id}")
        elif args.command == "list":
            for grant in readers.readers_for(owner):
                state = "enabled" if grant.enabled else "revoked"
                print(f"{grant.key_id}  {grant.dataset_id}  {state}  {grant.label}")
        elif revoke_reader_key(readers, owner=owner, key_id=args.key_id):
            print(f"Revoked {args.key_id}.")
        else:
            print(
                f"STOP: no enabled key {args.key_id} on this account", file=sys.stderr
            )
            return 1
    except (ReaderKeyError, StoreError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
