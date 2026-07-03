---
name: x402-payai-setup
description: Wire x402 micropayments onto a provider's API via PayAI — point the agent-facing tools at the provider's OWN x402 endpoint so agents can pay-per-call. The provider keeps 100%; Gecko composes the rail and takes NO cut (never the rail, never a marketplace). Covers the 402 challenge/settlement handshake, mapping which operations are priced, and verifying a paid call OFFLINE before going live. Solana/x402-first, but the pattern is any-rail. Use after api-agent-ready when a provider wants to charge agents for calls.
user-invocable: true
---

# x402 + PayAI Setup Skill

> **Do [`api-agent-ready`](../api-agent-ready/SKILL.md) first.** This skill assumes
> the API is already comprehended and served over MCP. It adds **pay-per-call** on
> top — the provider charging agents for priced operations, settled over the x402
> rail via **PayAI**.

## What this skill is for

Some operations are worth money. A provider who wants agents to *pay* for calls
needs the payment handshake wired into the tools the agent already uses — without
Gecko ever holding funds or taking a cut.

The mechanism is **x402**: an HTTP-native micropayment flow. A priced endpoint
answers an unpaid request with `402 Payment Required` and a challenge; the agent
attaches a payment and retries; a **facilitator** (PayAI, on Solana) verifies and
settles; the endpoint returns `200` + the data. Gecko's only job is
**comprehension**: point the agent-facing tool at the provider's own x402 endpoint
and surface the handshake so the agent knows to pay.

## The line we never cross

**Provider-pays for comprehension; provider keeps 100% of call revenue; Gecko is
never the rail.**

- The money flows **agent → provider**, settled by **PayAI**. Gecko is not in the
  money path and takes **no take-rate**.
- We **compose** the rail (PayAI / Metera / pay.sh) — we do not build one and do not
  become a marketplace.
- We never custody, never sign, never broadcast. Live settlement is **founder-gated**
  and defaults to `X402_MODE=stub`.

If any step routes money *through* Gecko or adds a cut, stop — re-read
[`rules/aggregate-not-rail.md`](../../rules/aggregate-not-rail.md).

## The three-step spine

| # | Step | Read | Status |
|---|---|---|---|
| 1 | **Map priced ops** — which operations sit behind x402 vs stay free | this file, below | provider's toggle |
| 2 | **Wire x402 via PayAI** — point tools at the provider's x402 endpoint | [wire-x402-payai.md](wire-x402-payai.md) | **Building / founder-gated** |
| 3 | **Verify offline first** — prove the paid-call shape before live settlement | [verify-paid-call.md](verify-paid-call.md) | **Building** — offline stub (`X402_MODE=stub`) |

### Step 1 — Map priced ops (the provider's toggle, not Gecko's)

Decide which comprehended operations are priced and which stay free. This is the
**provider's** decision — Gecko only reflects it.

- List the operations from step 1 of `api-agent-ready` (Pegana: 41; but Pegana's
  REST is **free / no-auth today** — pricing there is hypothetical, used only to
  illustrate the flow).
- For each priced op, note the amount, the asset (e.g. USDC on Solana), and the
  provider's `payTo` address — these come from the provider, not from Gecko.
- Free ops stay exactly as comprehended; priced ops carry the 402 handshake.

## This skill also ships

- **Command** — [`/setup-x402`](../../commands/setup-x402.md): wire the rail on a
  comprehended API, offline-first.
- **Agent** — [`x402-payments-engineer`](../../agents/x402-payments-engineer.md):
  the payments specialist (routes deep questions to the `web3-engineer` lane).
- **Rule** — [`aggregate-not-rail`](../../rules/aggregate-not-rail.md): compose the
  rail, take no cut; never sign or broadcast.

## Honest status

- The x402 **handshake shape** and the **offline (`X402_MODE=stub`) verification**
  are the deliverable you can build and prove today (Pattern B).
- **Live settlement** — a real payment moving on mainnet via PayAI — is **Building
  and founder-gated**. Claude simulates and hands over the command; the founder
  broadcasts. Never flip `X402_MODE` to live without explicit founder go-ahead.
- **PayAI-specific** facilitator URLs and SDK calls are marked `<!-- VERIFY -->` in
  [wire-x402-payai.md](wire-x402-payai.md) — confirm them against PayAI's live docs
  before shipping; do not invent them.

## Provider

Built by **[GeckoVision](https://geckovision.tech)** — the API-comprehension
company. Engine: [`gecko-surf`](https://github.com/GeckoVision/gecko-surf) (Apache-2.0).
Rail: **PayAI** (x402 facilitator on Solana) — composed, not owned.
