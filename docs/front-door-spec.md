# The universal front door — spec

*2026-07-03. The highest-leverage fix for the 189-visitor / 94%-bounce landing.
The diagnosis: **`pip install gecko-surf` is the wrong front door.** Day-one PyPI
downloads (~113) ≈ git clones, so a chunk of "installs" are people reading/reverse-
engineering the code, not *using* the hosted comprehension. A visitor who has to
`pip install`, read a README, point it at a spec, and self-host before seeing one
correct call will bounce. The front door must deliver a **working tool in one line,
zero install** — and the live path is what finally emits usage telemetry (today
`surf_events` only has our own 25 calls; see [[moat-corpus-flywheel]]).*

## What already exists (don't rebuild)

- **Hosted MCP is live** at `https://mcp.geckovision.tech` — a root index (`/`)
  listing surfaces, each mounted at `/{name}/mcp` with agent-native breadcrumbs
  (`/{name}/llms.txt`, `gecko.json`, `tools.md`). Two live surfaces today:
  `reportavnzla` (4 tools, **free, no auth**) and `sosvenezuela`. Code:
  `gecko/http_server.py` → `build_multi_surface_app`.
- **A marketplace plugin** (`gecko-surf`, under review): `.claude-plugin/marketplace.json`
  + `skills/.claude-plugin/plugin.json`. **Gap:** it ships three *skills*
  (api-agent-ready, x402-payai-setup, anti-poisoning) + two commands — and **zero
  MCP wiring**. Installing it teaches an agent *how* to use Gecko but hands it **no
  live tools**; the user still has to self-host. That gap is the fix below.

## The three tiers (fastest-to-value first)

### Tier 0 — the hero: one line, zero install (the anti-bounce)

```
claude mcp add --transport http gecko-reportavnzla https://mcp.geckovision.tech/reportavnzla/mcp
```

Ten seconds → the agent has **4 first-call-correct tools** against a real, free,
no-auth API. No `pip`, no spec, no key. This is the single command that must be
**the first thing above the fold** on the landing page and the README — replacing
`pip install`. `reportavnzla` is the ideal demo surface precisely because it's free
and unauthenticated: nothing between the visitor and a correct call.

Also emit the other client one-liners from the same block (same URL):
- **Cursor / Windsurf / VS Code**: the `mcpServers` JSON snippet (`{"type":"http","url":"…"}`).
- **Raw**: the URL itself — it's a standard Streamable-HTTP MCP endpoint.

### Tier 1 — the plugin, fixed to ship live tools

`/plugin install gecko-surf@geckovision` should give the agent the skills **and**
the live tools in one action. Today it gives skills only. **Fix:** bundle an
`.mcp.json` at the plugin root wiring the hosted surface(s):

```jsonc
// skills/.mcp.json  (plugin root — Claude Code auto-loads it on install)
{
  "mcpServers": {
    "gecko-reportavnzla": { "type": "http", "url": "https://mcp.geckovision.tech/reportavnzla/mcp" }
  }
}
```

Now `/plugin install` = skills that teach the pattern + a working surface to try it
on, atomically. (Keep the surface list short and free-tier; authed surfaces are the
user's own, added via Tier 2.) This is the change that turns the under-review plugin
from a docs bundle into a **product install**.

### Tier 2 — self-host / bring-your-own painful API (the ICP path)

For the actual ICP — "the Nth *painful* API" that is **theirs**, behind **their**
auth — the front door is the Python tool, run without a global install:

```
uvx --from gecko-surf gecko serve path/to/their-spec.yaml
```

`uvx` is the npx-equivalent for Python (`pip install` is not — never lead with it).
This serves their comprehended surface locally over the same `/mcp` endpoint; auth
stays their adapter seam (`Session.auth_headers()`), credentials never touch us.
The README's "your own API" section leads with `uvx`, mentions `pip install` only
as the contributor/library path.

## The one open decision: aggregator vs per-surface

Today each `claude mcp add` wires **one** surface. Two directions:

- **Now (ship it):** per-surface adds. Fine at 2 surfaces; the hero command points
  at the free `reportavnzla`.
- **V2 (flagged, not now):** a **root `/mcp` aggregator** exposing a cross-surface
  `search_capabilities` tool, so **one** add gives the agent the *whole catalog* and
  it discovers the right API by intent. This is the natural home for the enriched
  discovery in [[agent-native-surface-design]]. **Evidence-gate it:** build the
  aggregator when there are enough surfaces that per-surface adds are the friction —
  not before. Do **not** turn it into a public marketplace catalog (invariant:
  we consume pay.sh's catalog, we don't re-list as a provider).

## Why this is the bounce fix, concretely

- **Time-to-first-correct-call** drops from "clone + read + host" (minutes, high
  abandon) to one paste (seconds). That's the metric the 94% is really measuring.
- **It changes what a "user" is.** A `pip` download is indistinguishable from a
  clone-to-reverse-engineer. A `claude mcp add` to the hosted surface produces a
  **real call we can see** — the hosted surface emits `surf_events`, our first
  external usage signal. Usage, not downloads, is what turns the corpus flywheel
  ([[moat-corpus-flywheel]]).
- **It answers "why not build my own?"** at the door: the one-liner is *less* work
  than scaffolding even a trivial OpenAPI→tools layer, so DIY stops being the
  cheaper option for the painful-API case (the named enemy is "the coding agent
  one-shots it" — here it doesn't have to).

## Concrete change list (all founder-gated to merge)

1. **Landing page** (`gecko-mcpay-landing`): replace the `pip install` hero with the
   Tier-0 `claude mcp add` one-liner + a copy button + the Cursor/VS-Code snippet.
   Add a provider CTA (the who-pays gap in [[context-two-products-who-pays]]).
2. **README** (top): same swap — one-liner first, `uvx` for your-own-API, `pip` demoted
   to "contributing".
3. **Plugin**: add `skills/.mcp.json` (above); bump `plugin.json` version; note the
   live surface in the marketplace description. Re-submit if review requires.
4. **(V2, deferred)** root `/mcp` aggregator + `search_capabilities` — evidence-gated.

## Success metric

Not downloads. **% of landing visitors who run the one-liner**, measured by a first
`surf_events` call from a not-us client on the hosted surface. That's the first
honest external-usage number the project has ever had.
