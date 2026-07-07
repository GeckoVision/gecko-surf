#!/usr/bin/env python3
"""Serve the Colosseum Copilot API to your agent — first-call-correct.

Gecko comprehended this API from its docs (Colosseum publishes no OpenAPI spec). Your
Personal Access Token is injected at call time, NEVER shown to the agent, and sent only to
Colosseum's pinned host — Gecko refuses to leak a secret to any other host. BYOK: the token
stays on your machine.

    export COLOSSEUM_COPILOT_PAT=...      # get one at https://arena.colosseum.org/copilot
    uvx --from "gecko-surf[serve]" python serve_colosseum.py
    claude mcp add --transport http colosseum http://127.0.0.1:8000/mcp
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from gecko.client import AgentApiClient
from gecko.http_server import serve_http

BASE = "https://copilot.colosseum.com/api/v1"


@dataclass
class BearerSession:
    """Injects the PAT as a bearer token. Gecko sends a real User-Agent by default, so
    Colosseum's Cloudflare WAF doesn't 403 the call — no workaround needed here."""

    token: str

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def main() -> int:
    pat = os.environ.get("COLOSSEUM_COPILOT_PAT")
    if not pat:
        print(
            "Set COLOSSEUM_COPILOT_PAT — get one at https://arena.colosseum.org/copilot",
            file=sys.stderr,
        )
        return 1
    spec = json.loads(
        (Path(__file__).parent / "colosseum_copilot_openapi.json").read_text()
    )
    # base_url pins the trust anchor to Colosseum's host, so Gecko will inject the PAT
    # (it degrades to a $0 recorded call rather than fire a secret at an unpinned host).
    client = AgentApiClient(spec, base_url=BASE, session=BearerSession(pat))
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
