"""Founder-run: import the local catalog files into the service registry.

    uv run --extra events python scripts/servicemap_import.py --details DIR \\
        [--ledgers DIR] [--paysh saved-pay.sh-catalog.json]          # dry: counts only
    uv run --extra events python scripts/servicemap_import.py ... --write   # into Mongo

Dry by default: the import runs into an in-memory registry, prints what it would write,
and checks that the planner's pool read back from it has the same snapshot sha as
``load_catalog`` reading the same files. ``--write`` writes to ``gecko_registry`` on the
cluster whose connection string is in the keychain slot ``MONGODB_URI`` (never ``.env``,
never printed), and refuses if the registry kill switch is engaged.

No network besides that one Mongo connection: the pay.sh catalog is a saved file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

from gecko.anyquery.catalog_load import load_catalog
from gecko.anyquery.catalog_load import DETAILS_ENV, LEDGERS_ENV
from gecko.anyquery.store import StoreError
from gecko.paysh_catalog import CatalogError
from gecko.servicemap.importer import import_files
from gecko.servicemap.readers import paysh_entries_from_text
from gecko.servicemap.registry import MemoryRegistry, Registry, RegistryError
from gecko.servicemap.snapshot import load_from_registry
from gecko.servicemap.types import EVIDENCE_RANK


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--details", default=os.environ.get(DETAILS_ENV))
    parser.add_argument("--ledgers", default=os.environ.get(LEDGERS_ENV))
    parser.add_argument("--paysh", default=None)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args(argv)


def _mongo_registry() -> Registry:
    from gecko.anyquery.mongo import mongo_store_from_keychain
    from gecko.servicemap.mongo import MongoRegistry

    store = mongo_store_from_keychain()
    return MongoRegistry(store.client)


def main(argv: list[str] | None = None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    if not args.details or not Path(args.details).is_dir():
        print(f"--details (or {DETAILS_ENV}) must name a folder", file=sys.stderr)
        return 2
    details = Path(args.details)
    ledgers = sorted(Path(args.ledgers).glob("*.jsonl")) if args.ledgers else []
    paysh = Path(args.paysh) if args.paysh else None
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        registry = _mongo_registry() if args.write else MemoryRegistry()
        if registry.killed():
            print(
                "the registry kill switch is engaged; nothing written", file=sys.stderr
            )
            return 3
        counts = import_files(
            registry, details=details, ledgers=ledgers, paysh_catalog=paysh, now=now
        )
    except (RegistryError, StoreError, CatalogError) as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 1

    print("WROTE to gecko_registry" if args.write else "DRY RUN (nothing written)")
    for line in counts.lines():
        print(f"  {line}")
    fields = registry.fields()
    by_evidence = {e: sum(1 for f in fields if f.evidence == e) for e in EVIDENCE_RANK}
    print(
        f"  held: {len(registry.services())} services, "
        f"{len(registry.operations())} operations, "
        f"{len(registry.sightings())} sightings, {len(fields)} fields "
        + str({k: v for k, v in by_evidence.items() if v})
    )
    entries = (
        paysh_entries_from_text(paysh.read_text(encoding="utf-8")) if paysh else []
    )
    files = load_catalog(details, ledgers=ledgers, paysh=entries)
    read_back = load_from_registry(registry, now=now)
    same = files.candidates.snapshot_sha == read_back.candidates.snapshot_sha
    print(
        f"  planner pool: files {len(files.candidates.candidates)} "
        f"sha {files.candidates.snapshot_sha[:12]} | registry "
        f"{len(read_back.candidates.candidates)} "
        f"sha {read_back.candidates.snapshot_sha[:12]} | same: {same}"
    )
    return 0 if same else 4


if __name__ == "__main__":
    raise SystemExit(main())
