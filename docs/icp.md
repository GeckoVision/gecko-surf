# ICP — who Gecko is for

**Status:** canonical (2026-07-26). Pairs with `docs/positioning.md` ("Stop guessing")
and `docs/provider-delivery.md` (the Agent-Readiness Scorecard).

## The consumer ICP — teams shipping production multi-API agents

Sharpened to **"the Nth *painful* API"**: the long-tail, messy, poorly-documented, often
paywalled APIs that coding agents do **not** already one-shot. A team wiring their fifth,
tenth, twentieth external API into an agent — where each new one is a fresh round of
guessing, retries, and silent wrong calls.

- **Is:** builders of agentic products who feel API-integration pain per-API and can measure
  it (or would, if they could see it — E2E success hides the intermediate guessing).
- **Is not:** API *providers* (that's the pay.sh ICP), hobbyists, or blue-chip-only teams
  whose whole stack is Stripe/Twilio (which agents already one-shot).
- **Where they gather:** r/LangChain, agent-framework GitHub issues, the build-in-public
  communities (Indie Hackers, Dev.to), X threads venting about MCP maintenance.
- **De-risked supply:** we ingest any OpenAPI unilaterally, so there is no cold-start — the
  consumer never waits for a provider to onboard.

## The provider ICP — for the paid motion (later)

The **painful/paywalled/drifting** long-tail provider whose API is at risk of being *invisible
in the agent era* — agents guess against it, misuse it, or pick a cleaner competitor surface.
The buyer is the **DX / product owner**, never CISO/CFO. First contact is a **free
Agent-Readiness Scorecard** (a lighthouse/design-partner motion), not a paid seat — see
`docs/provider-delivery.md`. TxODDS is provider #1 (they already asked about DX — a warm
provider-WTP signal).

## Why this ICP, not "everyone with an API"

"Any API" invites *"I already use my APIs and they work."* The ICP is specifically the API a
team's agent **gets wrong when they're not looking** — the one that isn't in the model's
training-frequency sweet spot. That specificity is the whole wedge: we help where agents
*don't* already one-shot.
