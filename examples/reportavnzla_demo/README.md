# ReportaVNZLA — a live humanitarian API, centralized on our MCP

**The API:** [ReportaVNZLA](https://reportavnzla.com) (open-source, MIT —
[bitupx00/reportavnzla](https://github.com/bitupx00/reportavnzla)) is the
Venezuela-2026 earthquake registry: ~61,000 missing/found/deceased people (with
last-known coordinates and a `estado` status) plus relief resources (donation
collection centers). Public reads, **no token**. Its `/desarrolladores` page
documents the endpoints for humans — nothing machine-readable for agents.

**What Gecko does:** comprehends the API (spec hand-authored from the dev page,
verified against live responses) into first-call-correct tools an agent picks by
intent — and serves it on our hosted MCP alongside the other relief APIs.

| | |
|---|---|
| Operations → agent tools | 4 → 4 (public, no token) |
| First-call-correct (intent → op → well-formed) | top-1 **100%** · well-formed **100%** |
| Live verified | `getStats` → 60,944 total · `searchPersonas` returns real records |
| Coordinates on person records | ✅ `lat`/`lng` → nearest-safe-place is a Haversine, not ML |

**Served, centralized:** `gecko/serve_mcp.py` serves this surface at
`mcp.geckovision.tech/reportavnzla/mcp` next to `/sosvenezuela/mcp` — one host, many
relief APIs, each with its own `/{name}/llms.txt` / `gecko.json` discovery surface.
`/` lists what's available. Add one — ship its spec + a line in `_SURFACES`.

```bash
uv run python examples/reportavnzla_demo/demo.py     # the showcase, offline $0
uv run pytest examples/reportavnzla_demo/ -q         # pin the claims

# emit ReportaVNZLA's agent-native surface (the "ready to use" docs, our product):
uv run --extra serve python -m gecko.serve \
  examples/reportavnzla_demo/spec/reportavnzla_openapi.json \
  --emit-dir /tmp/reportavnzla-agentnative --site-url https://reportavnzla.com
```

**Scope / integrity notes:**
- **Read surface only** — the write/telegram/subscription endpoints (their own bot
  plumbing) are excluded; the bot is a *consumer*.
- **PII:** person records may include `cedula` (national id) — the bot passes it
  through, **never stores or logs it** (control-plane invariant).
- **Status transparency, not a gate:** `estado` (buscado/encontrado/fallecido) is
  surfaced so an agent can present *unverified* data honestly rather than as fact —
  the crowdsourced-data mess-control we want, made machine-readable.
- The face-recognition endpoint (`/api/fr/*`, documented on their dev page) is a
  future add — comprehend it the same way once its service is back up (it 502'd
  during recon).
