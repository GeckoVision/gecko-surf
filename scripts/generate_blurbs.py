"""Regenerate the PINNED S0 blurbs (explicit, reviewed step — never a per-ingest call).

Runs ``HaikuEnricher`` once over every operation of the two committed golden fixtures and
writes ``tests/fixtures/golden/blurbs/{txodds,pegana}.json`` (raw blurb keyed by tool_name,
with model provenance + a pinned hash). The hash freezes the data the gate measures against,
so a non-deterministic LLM output can't silently move the baseline (plan §4 determinism).

    uv run python scripts/generate_blurbs.py            # both fixtures
    uv run python scripts/generate_blurbs.py txodds     # one fixture

Reads CLAUDE_API_KEY from .env (never printed). Blurbs are sanitized at CONSUME time
(``safe_blurb`` in the catalog wiring and ``surfacedoc_from_operation``), not here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.enrich import HaikuEnricher, dump_pinned_blurbs  # noqa: E402
from gecko.ingest import extract_operations, load_spec  # noqa: E402
from gecko.tools import tool_name  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BLURB_DIR = ROOT / "tests" / "fixtures" / "golden" / "blurbs"
SPECS = {
    "txodds": ROOT / "tests" / "fixtures" / "txodds_docs.yaml",
    "pegana": ROOT / "tests" / "fixtures" / "pegana_openapi.json",
}


def _env(key: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(rf"^{re.escape(key)}=(.*)$", line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    raise SystemExit(f"{key} not found in .env")


def main() -> None:
    which = sys.argv[1:] or list(SPECS)
    enricher = HaikuEnricher(api_key=_env("CLAUDE_API_KEY"))
    BLURB_DIR.mkdir(parents=True, exist_ok=True)
    for name in which:
        ops = extract_operations(load_spec(str(SPECS[name])))
        blurbs: dict[str, str] = {}
        for op in ops:
            blurbs[tool_name(op)] = enricher.blurb(op)
            print(f"[{name}] {tool_name(op)}: {len(blurbs[tool_name(op)])} chars")
        dest = BLURB_DIR / f"{name}.json"
        dest.write_text(dump_pinned_blurbs(blurbs), encoding="utf-8")
        print(f"wrote {dest} ({len(blurbs)} blurbs)\n")


if __name__ == "__main__":
    main()
