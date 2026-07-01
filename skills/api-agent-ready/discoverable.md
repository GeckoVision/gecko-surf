# Step 4 — Make it discoverable (breadcrumb, not a catalog)

**Status: Building.** The breadcrumb pattern is documented here; there is no hosted
Gecko discovery service, and there will **not** be a public catalog.

Discoverability is the last mile: an agent has to be able to *find* the agent-ready
MCP. There are two ways to do that, and this kit deliberately picks the smaller one.

## The discipline: breadcrumb, not catalog

**Gecko does not host a public directory of providers' APIs.** That is a standing
product decision (the "day-one model" discipline), for two reasons:

1. Listing providers' APIs in a central catalog would make Gecko a **marketplace** —
   the frames.ag / Bazaar lane. We compose with those; we don't become one.
2. A catalog re-lists supply we don't own. We *consume* a provider's spec to make it
   usable; we never re-publish it as a provider.

So discovery is **provider-hosted and breadcrumb-based**: the agent finds the MCP
from the provider's *own* domain, not from a Gecko index.

## How an agent finds it

The breadcrumbs from [artifacts.md](artifacts.md) do the work, all served from the
provider's origin:

1. Agent (or its crawler) reads `https://api.example.com/llms.txt`.
2. `llms.txt` points at `gecko.json` (machine breadcrumb) and the MCP URL.
3. The agent adds the MCP with the one-click string and starts calling
   first-call-correct tools.

No central registry is consulted at any step. The provider stays the source of
truth for its own surface.

## What "discoverable" is *not*

- **Not** a Gecko-hosted list of every onboarded API.
- **Not** a re-publication of the provider's spec under a Gecko namespace.
- **Not** a ranking/marketplace of providers competing for agent attention.

If a plan starts to make discovery into any of those, stop — that's the rail/
marketplace lane, not ours. See [aggregate-not-replace.md](aggregate-not-replace.md)
and [`rules/aggregate-not-rail.md`](../../rules/aggregate-not-rail.md).

Next: [aggregate-not-replace.md](aggregate-not-replace.md) — the invariant that
holds all four steps in our lane.
