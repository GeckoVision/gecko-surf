# Pegana correlation surface — the "one query, many correlated services" delivery

**Status:** approved plan (2026-07-28). First real cross-API correlation surface on a
live, third-party design-partner API. Pairs with `docs/context7-integration.md` (VAS-4),
`docs/specs/2026-07-26-verified-agent-surface-milestone-1.md`, `docs/positioning.md`.

## The JTBD

> **"Should I worry about this pegged asset — and can I still get out?"**

Pegana (`api.pegana.xyz`) is a peg-risk **state** oracle for 69 pegged Solana assets across
7 peg shapes (LSTs, fiat stables, yield-bearing, CDP, delta-neutral, FX, leveraged synth). It
answers one question: *is this asset still doing what it promises?* — as a 5-state FSM
(`PEGGED → DRIFT → DEPEG → CRITICAL → BLACK_SWAN`) with intrinsic-vs-market discount.

Pegana's own docs name the gap this surface fills:

> *"Not a price oracle — cross-check with Pyth/Switchboard. Not a single-source liquidation
> feed. We publish state, not sell-now signals."*

The cross-check Pegana tells you to run is manual today. **Gecko makes it one query,
first-plan-correct.** We are not stretching a correlation — the provider *invites* it.

## What "one query → many correlated services" means

User asks: **"Is jitoSOL safe to hold right now?"** Gecko plans a chain joined by the
**Solana token mint** — the value-domain signature (`solana-token-mint`, `gecko/vindex.py`)
that Pegana, Birdeye, and Jupiter already share:

1. **Pegana** `GET /v1/assets/{symbol}/state` (or `/by-mint/{mint}/state`) → the verdict:
   state + `intrinsic_usd` vs `market_usd` discount. Keyless, `$0`.
2. *If `DRIFT` or worse*, fan out (the multi-step chain — the production death-zone we hold):
   - **Birdeye** (by mint) → liquidity depth + volume + token security → *can you exit, is the
     token itself sane?*
   - **Jupiter** (bundled, by mint) → live `{asset}→USDC` route + price impact → *actual
     exitability, not theoretical.*
   - **news** (by token) → the *why* — is an event driving it?
3. Gecko composes → one grounded, provenance-tagged answer, e.g. *"jitoSOL in DRIFT
   (−32 bps, 6 min). Exit: $Z m depth; Jupiter routes 100k→USDC at 11 bps. No news event.
   → market-noise drift, exit is liquid."*

The cross-API join is **DECLARED** (value-domain), **customer-CONFIRMED** before it is
plan-eligible (`graph.confirmed` gate) — no silent auto-join across providers.

## How Context7 + the existing providers combine

- **Context7** serves Pegana's *concept* docs (peg-risk-101, the FSM, the five case studies) —
  the "what does DRIFT mean" knowledge, retrieved as an **unverified INPUT**.
- **Gecko** serves the deterministic *call chain* + verify (is the endpoint real) + the
  cross-service correlation + safety.
- Shipped as the **side-by-side `mcp.json`** from VAS-4 (Arch 2): Context7 for concepts, Gecko
  for the grounded multi-API answer. Pegana becomes a new node that joins Birdeye / Jupiter /
  news by mint in the correlation registry.

## Plan (phased, testable-with-people)

| Phase | Deliverable |
|---|---|
| **0 · Comprehend + verify** | `gecko` ingest `https://api.pegana.xyz/openapi.json` → first-call-correct tools; `gecko verify-docs --live` → **VERIFIED** badges (Pegana is keyless → real live verify, `$0`); `gecko report` → Agent-Readiness Scorecard for Raff. |
| **1 · Declare + confirm joins** | Declared hints: Pegana `by-mint` ↔ Birdeye / Jupiter / news mint params (`solana-token-mint`); `gecko graph confirm`; an **offline correlation test** (Pattern B) asserting the chain is first-plan-correct on recorded fixtures. |
| **2 · The one-query surface** | Intent → chain ("is `<asset>` safe to hold"); a **recorded, `$0`, offline-falsifiable demo** runnable in front of people today, + a live variant; the side-by-side `mcp.json` Raff drops into Claude/Cursor. |
| **3 · Playground (optional v1)** | The Context7-style chat showcase — type the question, watch the chain resolve with provenance; zero install for the person being shown. |

## Honesty gates (project law)

- **AGGREGATE, do not replace.** Pegana already ships its own MCP (`mcp.pegana.xyz`, free +
  x402-paid tools). We do **not** re-list it — we add the correlation layer *above* it. Their
  MCP stays intact.
- **Pattern B.** The recorded offline demo is the first deliverable and can falsify the chain;
  live smoke is the final check.
- **No overclaim.** Pegana is a *state* oracle. We present *state + exitability*, never
  "sell now" — matching their own "not a stop-loss oracle."
- **Verify tier stays honest.** VERIFIED only from real (keyless) live calls; keyed/paid hops
  (Birdeye key, Jupiter Pro, Pegana x402 tools) labelled by access, never overclaimed.
- **Control-plane #1.** Store the surface + correlation metadata only — never Pegana's (or any
  provider's) response payloads.

## Ingest-safety note

Treat Pegana's spec + docs as untrusted input (Skill Guard on ingest, same as any API). The
`llms.txt` and `.md` concept pages are Context7's INPUT lane, not ground truth.
