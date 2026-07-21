# gecko-surf — make any API agent-usable without integration code

<!-- mcp-name: tech.geckovision/surf -->

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB.svg)](https://www.python.org/)
[![uv](https://img.shields.io/badge/uv-managed-DE5FE9.svg)](https://docs.astral.sh/uv/)
[![Claude Code](https://img.shields.io/badge/surface-MCP-D97757.svg)](https://modelcontextprotocol.io/)
[![x402](https://img.shields.io/badge/x402-stub%20%7C%20live-9945FF.svg)](https://x402.org/)
[![status](https://img.shields.io/badge/V1-live%20on%20one%20API-orange.svg)](#-status-honest)

> **Agents one-shot clean APIs. They break on the painful ones** — messy, paywalled,
> half-documented, always drifting. Dumping the spec fails; hand-writing an MCP wrapper
> goes stale on the next API change.
>
> **Gecko turns an API into tools your agent calls right the first time — no integration
> code.** Point, add in one line, call the real API directly. Free and open source.
>
> *Docs are built for humans. Gecko translates them for agents.*

<p align="center">
  <img src="docs/assets/hero.gif" alt="One command: install gecko-surf, then comprehend any API into first-call-correct MCP tools" width="820">
</p>

### Where Gecko sits — three verbs, three layers

| Layer | What it does | Who |
|---|---|---|
| APIs get **PAID** | billing / settlement rail | Metera (gate402), MCPay |
| skills get **DISTRIBUTED** | marketplace / discovery | frames.ag, Bazaar |
| **APIs get USED** | **comprehension / consumption** | **Gecko** |

We compose on x402 / MCP / pay.sh. We are **not** a payment rail or a marketplace.

---

## Try it in 10 seconds — one line, zero install

No `pip`, no spec, no key — 18 first-call-correct tools on the TxODDS TxLINE World Cup
API (two-token on-chain paywall). **Recorded demo** (`$0`, schema-synthesized); use your
own TxLINE session for live data. Also live: **Jito** at `/jito/mcp`.

```bash
claude mcp add --transport http gecko-txline https://mcp.geckovision.tech/txline/mcp
```

**Cursor / VS Code / any MCP client** — same endpoint in `mcp.json`:

```jsonc
{ "mcpServers": { "gecko-txline": { "type": "http", "url": "https://mcp.geckovision.tech/txline/mcp" } } }
```

Transport is **MCP Streamable HTTP** (`2025-11-25`), not SSE — use
`streamablehttp_client`, not `sse_client`. Ready for your own API?
→ [Make any API agent-usable](#make-any-api-agent-usable).

---

## ⚠️ Status (honest)

V1 is **live on mainnet** against real TxODDS: ingest → comprehend → catalog → access →
correct call → real data. **$0 recorded mode** runs the same path offline. **442 tests
pass.** Not proven: **consumer willingness-to-pay** — discovery work, not a demo claim.
What's real: a working comprehension path on one painful API, and an API-agnostic engine.

---

## Watch it run — the 70-second launch demo

<div align="center">

![Gecko 70s launch demo — 18 first-call-correct TxODDS tools; redteam blocks 8/8 poisoned attacks; gecko test 32/32 checks](docs/assets/launch.gif)

[MP4 version](docs/assets/launch.mp4)

</div>

Every number from a real run:

- **Plug in** TxODDS → **18 first-call-correct tools**, recorded/$0 first call (live when subscribed).
- **Stay safe** → poisoned-spec attacks that hit a naive agent **8/8** are blocked **0/8**.
- **Stay correct** → `gecko test` writes **32/32** first-call-correctness checks for CI.

---

## Architecture

**Control plane, never data plane.** Gecko holds the API *surface*, tool defs, and
correctness metadata — never response payloads, user data, or secrets. That invariant
is what lets it ingest any API unilaterally.

<div align="center">

![Gecko architecture — agent intent → ingest/catalog/tools/access control plane → agent calls the real API directly](docs/assets/architecture.png)

[Interactive diagram](docs/assets/architecture.html) · [SVG](docs/assets/architecture.svg)

</div>

1. **Ingest** — OpenAPI 3.x → normalized ops/params (never response data).
2. **Catalog** — intent → endpoint (lexical at this scale).
3. **Tools** — question-shaped defs; auth headers hidden.
4. **Access** — subscribe/session via one seam: `Session.auth_headers()`.
5. **Call** — agent hits the real API; Gecko injects credentials, stays off the data path.
6. **Validate** — replay, confirm first-call-correct, JSONL log → V2 correctness corpus seed.

---

## What you get

| Surface | Entry point | Status |
|---|---|---|
| **Serve any API to agents** (paste a spec → hosted MCP + one-click "add to Claude/Cursor") | `gecko serve <openapi-url>` (or bare `gecko <openapi-url>`) | shipped |
| **Generate + run first-call-correctness tests** (before any live call) | `gecko test <openapi-url> [-o test_api.py]` | shipped |
| **Recover a draft OpenAPI from human docs** (no spec? point it at the doc page) | `gecko from-docs <doc-url-or-path> [-o draft.json]` | shipped |
| **Embed the SDK** (`search / list_tools / prepare / call`) | `from gecko import AgentApiClient` | shipped |
| **Forkable starter** (an app on any API, ~20 lines, $0) | `examples/_starter/` | shipped |
| **$0 recorded demo** (goal → discover → correct call → data, offline) | `python -m gecko.demo` | runnable now |
| **Live demo** against real TxODDS World Cup data | `gecko.demo:live_demo` (after subscribe) | mainnet-proven |
| **Correctness harness** (first-call-correct + flywheel log) | `gecko.validator` | shipped |

---

## The surface graph — intent → the right *chain* of calls

Real questions rarely map to one endpoint. *"Get live odds updates"* needs a
`fixtureId` — so Gecko plans a **chain** from the spec alone (no call logs, no
training data):

```
agent intent: "get live odds updates"

plan:
  1. GET /api/fixtures/snapshot              # supplies fixtureId
  2. GET /api/odds/updates/{fixtureId}
explain:
  fixtureId ← FixtureId   [INFERRED · entity:fixture · high]
```

**Spec-derived** with provenance on every edge (`EXTRACTED` vs `INFERRED` + confidence).
Plans are suggestions — your agent still makes every call. Measured offline: Stripe
control cut false links **66,984 → 337** (−99.5%) while finding every known chain on a
paywalled API — see [docs/benchmarks.md](docs/benchmarks.md). On by default in
`search_capabilities`. Cross-API chains are next:
[design](docs/specs/2026-07-19-surface-graph-correlations-design.md).

---

## Make any API agent-usable

Point at an OpenAPI — no client code, auth handled, first call correct.

**A · Claude Code — Marketplace plugin** (skills + live demo surface):

```
/plugin marketplace add GeckoVision/gecko-surf
/plugin install gecko-surf@geckovision
/make-agent-ready https://api.example.com/openapi.json
```

Wires `gecko-txline` plus `/make-agent-ready`, `/setup-x402`, and anti-poisoning.

**B · Everywhere else — CLI** (Cursor, VS Code, any framework):

```bash
uvx --from "gecko-surf[serve]" gecko <openapi-url>
```

Prefer `uvx` (nothing to verify). Or prove the installer first with
[`scripts/verify_install.py`](scripts/verify_install.py), then
`curl -fsSL https://get.geckovision.tech/install.sh | bash`.

`gecko <url>` prints the MCP URL and one-click add links (Cursor / VS Code / raw).
**Claude Code → Marketplace; everything else → CLI.** You don't need both.

**Or embed the SDK:**

```python
from gecko import AgentApiClient, public_session

client = AgentApiClient(spec, session=public_session())
hit = client.search("what you want")[0]            # intent → right endpoint
client.call(hit["name"], {...}, mode="recorded")   # "live" for real data
```

Forkable starter: [`examples/_starter/`](examples/_starter/) (~20 lines, $0). Full
agent: [`examples/sos_vzla_bot/`](examples/sos_vzla_bot/).

### Registry surfaces

```bash
gecko serve --registry colosseum --auth-env COLOSSEUM_COPILOT_PAT
```

Free surfaces need no account. Premium: `GECKO_API_KEY` via
`POST /registry/keys` → OTP → `gk_live_...` (shown once; we store a salted hash).
Your provider key stays local — Gecko never sees it.

---

## Develop / falsify offline ($0, no keys, no subscription)

```bash
git clone https://github.com/GeckoVision/gecko-surf
cd gecko-surf && uv sync
uv run pytest                       # 442 passing
uv run python -m gecko.demo      # E2E: goal → discover → correct call → data (recorded, $0)
```

The recorded demo runs the **same code path** as live — it just synthesizes responses
from the schema instead of hitting the network. That's the point: you can falsify the
comprehension offline before spending a cent.

---

## Going live (real World Cup data)

Recorded mode needs no subscription. For live data, do the one-time on-chain subscribe
— see [`scripts/SUBSCRIBE.md`](scripts/SUBSCRIBE.md) — then pass a real `Session`:

```python
from gecko.client import AgentApiClient
client = AgentApiClient(spec, base_url="https://...", session=my_session)
client.call(tool, args, mode="live")   # same path as recorded
```

> **Mainnet boundary:** the subscribe transaction is **founder-run only**. The tooling
> *simulates* (no spend) and hands over the exact command; a human broadcasts.

---

## What's in this repo

| Path | Purpose |
|---|---|
| `gecko/ingest.py` | OpenAPI 3.x → normalized `Operation`/`Param` (`$ref` resolution, guarded) |
| `gecko/catalog.py` | Lexical capability search (intent → endpoint) |
| `gecko/tools.py` | `Operation` → question-shaped agent tool defs (**auth hidden**) |
| `gecko/caller.py` | tool + args → correct `PreparedRequest` (stdlib `urllib`) |
| `gecko/access.py` | `Session.auth_headers()` — the engine/adapter seam; two-token session |
| `gecko/sample.py` | deterministic schema → example (powers $0 recorded mode) |
| `gecko/client.py` | `AgentApiClient` — `search / list_tools / prepare / call` |
| `gecko/mcp_server.py` | `McpSurface` — the agent-facing MCP surface |
| `gecko/validator.py` | replay + first-call-correct + JSONL outcome log (moat seed) |
| `gecko/demo.py` | `run()` (recorded) + `live_demo()` |
| `gecko/serve.py` | `gecko <url>` CLI — comprehend + serve over Streamable-HTTP MCP (+ one-click add) |
| `examples/_starter/` | forkable "app on any API" (engine-only, $0); `examples/sos_vzla_bot/` is the full LLM agent |
| `scripts/subscribe.py` | one-time on-chain subscribe (solders); simulate by default |
| `docs/` · `private/` | strategy & PRD · gitignored business docs (canvas, pitch, numbers) |

**Rule:** the comprehension logic is the product and lives in `gecko/`. The MCP
server, the client, and scripts are thin transport.

---

## Stack

| Layer | Tool |
|---|---|
| Language | Python 3.11+, managed with `uv` |
| Engine | stdlib-first (`urllib`); minimal deps; `pyyaml` for spec loading |
| Agent surface | `mcp` (Model Context Protocol) |
| Access / payments | x402; on-chain subscribe via `solders`; modes `stub` / `live` |
| Quality | `ruff` · `mypy` · `pytest` (442 tests) |

---

## Environment variables

**Source of truth:** [`.env.example`](.env.example).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `X402_MODE` | no | `stub` | `stub` / `live`. **Stub is intentional during user-testing — do not flip to live without founder go-ahead.** |
| `TXODDS_*` / session file | for live only | — | live World Cup access after the on-chain subscribe (see `scripts/SUBSCRIBE.md`) |

Recorded mode and the test suite need **no** keys.

---

## Development

```bash
uv run ruff format && uv run ruff check --fix
uv run mypy gecko
uv run pytest                       # targeted invocations preferred
uv run python -m gecko.demo      # $0 recorded smoke
```

See [`CLAUDE.md`](CLAUDE.md) for the architecture invariants, the subagent team, and
conventions.

---

## Team

- **Ernani** ([@ernanibritto](https://x.com/ernanibritto)) — Technical co-founder.
  Builds the Gecko engine end-to-end: ingest, comprehension, the access layer, and
  the MCP surface.
- **Leticia** ([@0xLeti](https://x.com/0xLeti)) — Co-founder, Product Designer. 8+
  years designing for enterprises and startups; ex-Liga Ventures.

---

## License

**Apache License 2.0** — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Apache-2.0 carries
an explicit patent grant. The engine is open (the distribution funnel); the correctness corpus
and hosted layer stay private (open-core).

---

*The comprehension layer for the agentic economy.*
