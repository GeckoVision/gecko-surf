---
description: When onboarding a provider's API or wiring payments, AGGREGATE never replace the provider's own MCP, and COMPOSE the payment rail never become it — no take-rate, no public catalog, no signing/broadcasting.
alwaysApply: false
---

# Rule: aggregate, don't replace — compose the rail, don't become it

When making a provider's API agent-ready (or adding pay-per-call), stay in the
**comprehension / consumption** lane. Two boundaries, never crossed:

## 1. Aggregate, never replace

- **Never touch the provider's own MCP.** Do not modify, proxy, or shut it down.
  Gecko comprehends the OpenAPI and serves the **full** surface **alongside** it.
- The provider's hand-wrapped tools keep working unchanged; Gecko adds first-call-
  correct coverage of the rest. Onboarding is **additive** — nothing regresses.
- **No public catalog.** Discovery is provider-hosted and breadcrumb-based
  (`llms.txt` / `gecko.json` at the provider's origin). Never re-list a provider's
  API in a Gecko-hosted directory — that's a marketplace, not our lane.

## 2. Compose the rail, never become it

- **Money flows agent → provider, settled by a rail we compose** (PayAI / Metera /
  pay.sh). Gecko is **never in the money path** and takes **no cut**.
- **`payTo` is the provider's wallet, never Gecko's.** The provider keeps 100%.
- **Never sign or broadcast a mainnet transaction.** Live x402 settlement is
  founder-run only; default `X402_MODE=stub` and never flip to live without explicit
  founder go-ahead. Claude simulates and hands over the command.

## 3. Control-plane only

Store the API **surface** + generated tool defs + correctness metadata. **Never**
store response payloads, user data, or secrets. That governance promise is what lets
a provider onboard unilaterally — protect it.

## The check

Before any onboarding or payments step, ask:

- Does this modify or replace the provider's own MCP? → **stop.**
- Does this route money through Gecko, add a take-rate, or custody funds? → **stop.**
- Does this list the provider's API in a Gecko-hosted catalog? → **stop.**
- Does this store a response payload, user datum, or secret? → **stop.**
- Would this sign or broadcast a transaction without founder go-ahead? → **stop.**

If any answer is yes, the plan has drifted out of the comprehension lane into the
rail or marketplace lane. Re-scope it back to: *comprehend the surface, serve it
alongside what exists, compose the rail, take nothing.*
