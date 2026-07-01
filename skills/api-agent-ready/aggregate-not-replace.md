# Step 5 — Aggregate, not replace (the invariant)

**Status: invariant.** This holds across every step and every API.

The single rule that keeps this skill honest and welcome to a provider: **Gecko
never touches the provider's own MCP.** It comprehends the OpenAPI and serves the
*full* surface **alongside** whatever the provider already built.

## Why this matters to a provider

A provider who shipped an MCP invested in it. If onboarding to Gecko meant
"replace your MCP," you'd be asking them to throw away work and hand you their
distribution. No provider says yes to that.

Aggregate-not-replace flips it: Gecko is **additive**. The provider's ~6 hand-tuned
tools keep working exactly as they were; Gecko adds first-call-correct coverage of
the other N−6 operations they never had time to wrap. Nothing they built regresses.

## What "aggregate" looks like in practice

| The provider's MCP | Gecko's served MCP |
|---|---|
| ~6 hand-wrapped, highlight tools | The full surface (e.g. Pegana: 41 ops → 26 surfaced) |
| Whatever transport they chose | Streamable-HTTP, one-click add |
| Curated, opinionated | Mechanical, spec-driven, first-call-correct |
| **Unchanged by Gecko** | Runs **side by side** |

An agent can add both. They don't conflict: the provider's MCP is the curated front
door; Gecko's is the complete surface.

## The hard boundary

Aggregate-not-replace has a twin: **never become the rail or the marketplace.**

- **Not a rail.** When a provider wants payments, we *compose* the x402 rail (PayAI,
  Metera, pay.sh) and take **no cut** — see [`x402-payai-setup`](../x402-payai-setup/SKILL.md).
- **Not a marketplace.** We don't host a public catalog of providers' APIs
  (see [discoverable.md](discoverable.md)); the provider stays the source of truth.
- **Control-plane only.** We store the surface + correctness metadata, never
  response payloads, user data, or secrets.

The canonical statement of this boundary is
[`rules/aggregate-not-rail.md`](../../rules/aggregate-not-rail.md). If any onboarding
step drifts toward replacing the provider's MCP, becoming the rail, or listing
supply we don't own — **stop and re-read that rule.**

Back to the spine: [SKILL.md](SKILL.md).
