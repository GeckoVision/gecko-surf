"""A minimal app on ANY API — surfcall engine only, no LLM, no keys, $0.

    python -m examples._starter.app <openapi-spec> "<what you want>" [arg=value ...]

    # e.g. against any public OpenAPI:
    python -m examples._starter.app https://api.example.com/openapi.json \\
        "current weather for a city" city=Caracas

It comprehends the spec, finds the capability that best matches your plain-language
intent, prepares the *correct* request, and — in recorded mode — synthesizes the
response from the schema. That proves the call is well-formed before you spend a
cent or a token. This is the whole point: first-call-correct, falsifiable offline.

FORK THIS:
  - real data: pass a real `Session` and `mode="live"` (see surfcall.access).
  - a full agent: wrap `client.search` / `client.call` in an LLM tool-use loop —
    `examples/sos_vzla_bot/` is exactly that (Telegram + Claude/OpenRouter).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from surfcall import AgentApiClient, public_session


def run(
    spec: str | dict[str, Any],
    intent: str,
    args: dict[str, str],
    *,
    mode: str = "recorded",
) -> dict[str, Any]:
    """Comprehend `spec`, pick the capability matching `intent`, call it.

    `mode="recorded"` synthesizes the response from the schema (offline, $0);
    `mode="live"` hits the real API (pass an authed Session for paywalled ones).
    """
    client = AgentApiClient(spec, session=public_session())
    hits = client.search(intent, limit=3)
    if not hits:
        raise SystemExit(f"No capability in this API matched: {intent!r}")
    chosen = hits[0]
    return {"chose": chosen, "result": client.call(chosen["name"], args, mode=mode)}


def _parse_kv(pairs: list[str]) -> dict[str, str]:
    """Turn ``key=value`` CLI tokens into an args dict (ignores malformed ones)."""
    out: dict[str, str] = {}
    for p in pairs:
        if "=" in p:
            key, value = p.split("=", 1)
            out[key] = value
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(__doc__)
        return 2
    spec, intent, *rest = argv
    out = run(spec, intent, _parse_kv(rest))
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
