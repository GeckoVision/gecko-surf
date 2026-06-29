"""Serve the SOS Venezuela surface as a hosted MCP endpoint (Mode B), locally.

This is the same comprehension MCP that would run at mcp.geckovision.tech — but on
your laptop, so you can test the real product flow with zero deploy:

    uv run --extra serve python -m examples.sos_vzla_bot.serve_sos_mcp

Then add  http://127.0.0.1:8000/mcp  as an MCP server in Claude Code / Cursor and call
the SOS tools — they hit the LIVE SOS Venezuela API through surfcall (mode="live").
"""

from __future__ import annotations

import json

from gecko.http_server import serve_http

from .config import SPEC_PATH

HOST = "127.0.0.1"
PORT = 8000


def main() -> None:  # pragma: no cover - run-the-server entrypoint
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    print(f"Serving SOS Venezuela MCP (live) at http://{HOST}:{PORT}/mcp", flush=True)
    print("Add that URL as an MCP server in Claude Code / Cursor.", flush=True)
    serve_http(spec, host=HOST, port=PORT, mode="live", server_name="sosvenezuela")


if __name__ == "__main__":
    main()
