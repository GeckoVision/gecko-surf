# Step 2 — Emit the agent-native artifacts (breadcrumbs)

**Status: Building.** This step is a **hand-authored pattern** today. `gecko` does
**not** emit these files yet — do not tell a provider the tool generates them. What
*is* Live is the comprehension + the served MCP those breadcrumbs point at (steps 1
and 3). Author these by hand for now; auto-emission is on the roadmap.

## Why breadcrumbs

A first-call-correct MCP is useless if agents can't *find* it. Agents (and the
crawlers that feed them) look for a few conventional files at a known path. The job
here is to drop small, standard breadcrumbs that say: *"this API has an agent-ready
MCP — here's where."* Nothing more. This is discovery-by-breadcrumb, **not** a
public catalog (that discipline is [discoverable.md](discoverable.md)).

## The three artifacts

### 1. `llms.txt` — the human/agent-readable breadcrumb

Served at the site root (`https://api.example.com/llms.txt`). Points an agent at the
MCP and the machine breadcrumb. Keep it short.

```
# Example API
> Peg-risk data for Solana assets. Full surface is agent-usable over MCP.

## Agent access
- MCP (Streamable-HTTP): https://mcp.example.com/mcp
- Add to Claude Code: `claude mcp add --transport http example https://mcp.example.com/mcp`
- Machine breadcrumb: https://api.example.com/gecko.json
- OpenAPI: https://api.example.com/openapi.json

## Notes
- Auth is injected at call time; tool defs never expose credentials.
- The provider's own MCP is unaffected — this is additive.
```

### 2. `gecko.json` — the machine breadcrumb

A tiny control-plane pointer (surface + correctness metadata only — **never**
payloads or secrets). Illustrative shape:

```json
{
  "schema": "gecko-breadcrumb/0.1",
  "api": "https://api.example.com",
  "openapi": "https://api.example.com/openapi.json",
  "mcp": { "transport": "streamable-http", "url": "https://mcp.example.com/mcp" },
  "operations_ingested": 41,
  "tools_surfaced": 26,
  "auth_gated_hidden": 15,
  "coexists_with_provider_mcp": true
}
```

The counts mirror what `gecko` computes live at serve time — copy them from the
serve banner; don't invent them.

### 3. `x-gecko` — spec annotations (inline, optional)

If you control the OpenAPI, you can annotate operations with an `x-gecko` vendor
extension to give the comprehension hints the prose implies (units, an identity
note, whether an op is priced). Example on one operation:

```yaml
paths:
  /v1/assets/by-mint/{mint}/state:
    get:
      operationId: state_by_mint
      x-gecko:
        intent: "peg state for an asset you hold as a mint address"
        identity: "mint (base58), not a ticker symbol"
```

`x-gecko` is **additive metadata** — it never changes the API's behavior, only how
cleanly `gecko` comprehends it. Leave it out and comprehension still works; add it
and the ambiguous ops get sharper.

## The rule for all three

**Control-plane only.** These breadcrumbs carry the API *surface* and correctness
metadata — never response data, user data, or secrets. If an artifact starts to
embed live data, stop: that violates the governance invariant that lets a provider
onboard unilaterally.

Next: [serve-mcp.md](serve-mcp.md) — stand up the MCP these breadcrumbs point at.
