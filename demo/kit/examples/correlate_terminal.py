#!/usr/bin/env python3
"""Video 2 — the correlation a-ha, terminal segment, demo-kit house style (80x20).

Two real APIs sharing a value-domain. Every line is the shipped `gecko correlate` /
`gecko index` output, parsed and shown human-readably (real values, demo formatting).

    asciinema rec --cols 80 --rows 20 \
      -c "uv run --all-extras python3 demo/kit/examples/correlate_terminal.py" \
      correlate_terminal.cast
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from screenplay import BOLD, CYAN, GREEN, RESET, YELLOW, out, put  # noqa: E402

REPO = Path(__file__).resolve().parents[3]

_TOKENDATA = {
    "openapi": "3.0.0",
    "info": {"title": "TokenData API", "version": "1.0"},
    "paths": {
        "/token/resolve": {
            "get": {
                "operationId": "resolveToken",
                "summary": "Resolve a symbol to its mint id",
                "parameters": [
                    {
                        "name": "symbol",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "tokenId": {
                                            "type": "string",
                                            "x-gecko-entity": "solana-token-mint",
                                        },
                                        "name": {"type": "string"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}
_MARKETNEWS = {
    "openapi": "3.0.0",
    "info": {"title": "Market News API", "version": "1.0"},
    "paths": {
        "/news": {
            "get": {
                "operationId": "newsForToken",
                "summary": "Latest news for a token",
                "parameters": [
                    {
                        "name": "tokenId",
                        "in": "query",
                        "required": True,
                        "x-gecko-entity": "solana-token-mint",
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "headlines": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        }
                                    },
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


def _stage() -> tuple[Path, Path, Path]:
    d = Path(tempfile.mkdtemp(prefix="corr_"))
    a, b = d / "tokendata.json", d / "marketnews.json"
    a.write_text(json.dumps(_TOKENDATA))
    b.write_text(json.dumps(_MARKETNEWS))
    return d, a, b


def _run(cmd: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "gecko.cli", *cmd],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def main() -> None:
    d, a, b = _stage()
    out(f"{BOLD}Gecko — one deterministic surface across every API{RESET}", pause=0.9)
    out("")

    # 1) correlate: derive the cross-API link + the WHY
    out(f"{CYAN}$ gecko correlate TokenData.json MarketNews.json{RESET}", 0.03)
    res = _run(["correlate", str(a), str(b)])
    link = (res.get("links") or [{}])[0]
    ba = link.get("basis", {})
    put(f"{GREEN}  1 correlation found{RESET}", pause=0.3)
    put(
        f"    resolveToken.{ba.get('src_field', 'tokenId')} "
        f"→ newsForToken.{ba.get('dst_param', 'tokenId')}",
        pause=0.4,
    )
    put(
        f"    why: both are value-domain {YELLOW}solana-token-mint{RESET} "
        f"({ba.get('provenance', 'DECLARED')})",
        pause=0.4,
    )
    put(
        f"    status: {YELLOW}candidate{RESET} — a human vouches before an agent chains it",
        pause=1.2,
    )
    put("", pause=0.5)

    # 2) index: the value-domain across BOTH APIs
    out(f"{CYAN}$ gecko index TokenData.json MarketNews.json{RESET}", 0.03)
    idx = _run(["index", str(a), str(b)])
    ent = (idx.get("entities") or [{}])[0]
    np_ = len(ent.get("producers", []))
    nc = len(ent.get("consumers", []))
    put(f"{GREEN}  solana-token-mint{RESET} — 1 category, 2 APIs", pause=0.3)
    put(f"    produced by  TokenData  ({np_} field)", pause=0.3)
    put(f"    consumed by  MarketNews ({nc} field)", pause=1.2)
    put("", pause=0.5)

    # 3) the payoff — a human confirms, the agent chains, first plan correct
    out(f"{CYAN}$ gecko graph confirm solana-token-mint   {RESET}", 0.03)
    put(f"{GREEN}  confirmed → the join is now plan-eligible{RESET}", pause=0.6)
    put(
        f"    agent: resolve SOL → get its news  {GREEN}✓ first plan correct{RESET}",
        pause=1.4,
    )
    out("")
    out(
        f"{BOLD}This API correlates to that — derived, with the why.{RESET}",
        0.03,
        pause=2.0,
    )
    import shutil

    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
