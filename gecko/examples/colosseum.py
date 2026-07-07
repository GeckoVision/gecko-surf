"""Serve the Colosseum Copilot API to your agent — first-call-correct, BYOK.

The surface (comprehended from Colosseum's docs — no OpenAPI is published) ships *inside*
the package, so there is no local file to fetch:

    export COLOSSEUM_COPILOT_PAT=...      # https://arena.colosseum.org/copilot
    uvx --from "gecko-surf[serve]" colosseum-mcp
    claude mcp add --transport http colosseum http://127.0.0.1:8000/mcp

Your PAT is injected at call time, hidden from the agent, and sent only to Colosseum's
pinned host — Gecko refuses to leak a secret to any other host.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from importlib import resources
from typing import Any

from gecko.client import AgentApiClient

BASE = "https://copilot.colosseum.com/api/v1"


@dataclass
class BearerSession:
    """Injects the PAT as a bearer token. (Gecko's caller supplies a real User-Agent by
    default, so Colosseum's Cloudflare WAF doesn't 403 the stdlib client.)"""

    token: str

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def load_spec() -> dict[str, Any]:
    """Load the packaged OpenAPI stub from importable package data (works from the
    installed wheel, not a cwd-relative path)."""
    text = (
        resources.files("gecko.examples")
        .joinpath("colosseum_copilot_openapi.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)  # type: ignore[no-any-return]


def build_client(pat: str) -> AgentApiClient:
    # base_url pins the trust anchor to Colosseum's host, so Gecko will inject the PAT
    # (it degrades to a $0 recorded call rather than fire a secret at an unpinned host).
    return AgentApiClient(load_spec(), base_url=BASE, session=BearerSession(pat))


def main() -> int:
    pat = os.environ.get("COLOSSEUM_COPILOT_PAT")
    if not pat:
        print(
            "Set COLOSSEUM_COPILOT_PAT — get one at https://arena.colosseum.org/copilot",
            file=sys.stderr,
        )
        return 1
    from gecko.http_server import serve_http  # optional [serve] deps, imported lazily

    client = build_client(pat)
    print(
        f"Colosseum Copilot — {len(client.list_tools())} first-call-correct tools ready."
    )
    print("PAT injected at call time, hidden from the agent, sent only to Colosseum.")
    print(
        "Add it:  claude mcp add --transport http colosseum http://127.0.0.1:8000/mcp"
    )
    serve_http(client, host="127.0.0.1", port=8000, mode="live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
