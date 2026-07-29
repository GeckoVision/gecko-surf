# Provider connection + live-data validation plan

**Status:** plan (2026-07-29). Answers the two questions: *"can a provider plug their service
into Gecko, Context7-style — do they get a URL?"* and *"does what we built actually connect
many providers, live?"* Pairs with `docs/provider-delivery.md`, `docs/context7-integration.md`,
`docs/trust-boundary.md`. GTM/pricing stays in `private/`.

## Part 1 — How a provider connects to Gecko (every way)

**The answer to "will everyone who wants to plug their service connect with Gecko?": yes — and
the provider builds nothing.** Gecko comprehends their OpenAPI unilaterally (invariant #2, no
cold-start). The ways, from most self-serve to most delivered:

1. **Self-serve — `gecko add <api>` / `gecko <openapi-url>`.** Anyone points Gecko at an OpenAPI
   (or recovers one from docs) and gets a comprehended, first-call-correct, safety-checked MCP
   surface with auth injected at call-time. No provider integration work. This is the floor:
   *any* OpenAPI plugs in.
2. **A hosted URL — `mcp.geckovision.tech/<provider>/mcp`.** We serve the provider's comprehended
   surface at a stable URL the agent adds — exactly like the shipped
   `mcp.geckovision.tech/txline/mcp`. **This is the "new URL for Raff":**
   `mcp.geckovision.tech/pegana/mcp` — a hosted, safe, correlated Pegana surface. The provider
   still runs their own API; Gecko is the comprehension + safety layer in front of it.
3. **Discovery — Context7-style.** (a) the side-by-side `mcp.json` (Context7 for concept docs +
   gecko for the safe surface); (b) the Context7 catalog listing (`context7.json`, PR #226) so an
   agent *discovers* Gecko and uses it on any painful API. Discovery is the indexed→used bridge
   (see `docs/trust-boundary.md` and the telemetry: indexed-not-used).
4. **Provider delivery — what we SHOW the provider.** (a) the **Scorecard** (`gecko report`) —
   the agent-readiness report: their surface, verified against reality (VERIFIED/REFUTED badges),
   correlated, safe, with the gap findings; (b) the **Playground** — a chat showcase: *"this is
   how an agent uses your API,"* context7-style.

**Boundary (reaffirmed):** we **AGGREGATE, never replace** the provider's own MCP, and we are a
catalog/discovery + safe-execution layer, **never a marketplace or a payment rail.** We list
gecko-surf, not a directory of providers.

## Part 2 — Live-data validation (Pegana + the combined services)

Offline validation is **done** (PR #229): all 14 committed providers comprehend, 0 expose an auth
header, the `pegana↔birdeye↔jupiter` mint correlation holds, and the safe-chain composes clean.
The live tiers finish the Pattern-B ladder (recorded first, live smoke last):

| Tier | What | Cost / gate | Status |
|---|---|---|---|
| **0 — offline** | comprehend + correlate + safe-chain across 14 providers; the durable matrix test | `$0` | ✅ done (PR #229) |
| **1 — keyless live** | hit the real **Pegana** reads (keyless) + **Jupiter** public (`lite-api.jup.ag`); `verify-docs --live` (Pegana already VERIFIED live); run the safe-chain for the keyless hops against reality | `$0`, no secrets | runnable now |
| **2 — BYOK live** | the full `pegana → birdeye → jupiter` chain live with a **Birdeye key** (+ any keyed provider); keys stay local, injected at call-time, never in `mcp.json` | needs key, founder-gated | pending keys |
| **3 — the demo** | the recorded `$0` safety-of-the-chain demo (Phase 2) + a live variant Raff can run in front of users | `$0` recorded | demo shipped; live variant on Tier 2 |

**Honesty gates:** keyless VERIFIED only from real 2xx; keyed hops labelled by access, never
overclaimed; the live safe-chain must **still quarantine the poisoned-provider variant** (safety
holds live, not just offline); control-plane #1 — no response payloads stored.

## Part 3 — What we hand Raff this week

1. **The hosted URL** — `mcp.geckovision.tech/pegana/mcp` (the safe, correlated Pegana surface).
   Raff's users add it to their agent; keyless, nothing to configure.
2. **The Scorecard** — Pegana's agent-readiness report (verified live, the surface graph, the
   gap findings the provider can act on).
3. **The safety-of-the-chain demo** (Phase 2) + the side-by-side `mcp.json` (Context7 + gecko).
4. **Founder actions to light it up:** deploy the hosted `pegana` surface (redeploy), submit the
   Context7 listing (founder-run), and provide the Tier-2 keys for the full live chain.

## Flag — quality gate before scaling to many providers

The multi-provider validation (PR #229) surfaced that our anti-poisoning **over-quarantines on
some real specs**: privy loses **72/159** ops (base58 wallet-address FP class), plus payapi
`createTransfer` and surfpool `requestAirdrop` (fund-routing / base58-in-response FPs). This
directly limits "usable surface" on real providers. **Recommended next security task: a
`defi-security-engineer` pass on the base58 / fund_routing false-positive classes** (relax the FP
without opening a bypass) — the validation test is the tripwire that measures it. This is the same
FP class the `fix/base58-address-false-positive-quarantine` line targets.
