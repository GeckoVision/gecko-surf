# Gecko — make any API agent-usable, no integration code

**The one-liner:** point an agent at an API — even a messy, paywalled one behind
human-shaped docs — and Gecko makes it find the right call, make it correctly the
first time, and run.

Docs and endpoints are built for humans. Gecko is the **comprehension layer** that
translates an API's *surface* into question-shaped, first-call-correct agent tools,
handles the auth handshake behind one seam, and lets your agent call the real API
directly. Minutes, not days — and you can prove every call is correct **offline, for
$0**, before you spend a token or a cent.

> **Before:** read the reference, hand-write a client, wire the auth, and *guess*
> whether the agent is calling it right — until it fails in production.
> **After:** `gecko https://api.example.com/openapi.json` → a hosted MCP your agent
> calls correctly on the first try.

![Gecko comprehends an API and serves it to your agent over MCP](assets/hero.gif)

▶ **[Watch the 70-second launch demo](assets/launch.gif)** — comprehend a paywalled API (TxODDS) into 8/8 first-call-correct tools + live odds, block a poisoned spec (8/8→0/8), and `gecko test` writes 32/32 checks.

## Start here
- **[Quickstart](quickstart.md)** — serve any API over MCP, or embed the SDK, in minutes.
- **[Why Gecko](why.md)** — the before/after, and who this is for.
- **[How it works](how-it-works.md)** — the five-stage comprehension pipeline.
- **[Stay correct](stay-correct.md)** — what happens when the upstream API changes.
- **[FAQ & data governance](faq.md)** — control-plane-only, BYOK, SSRF, and the honest roadmap.

## Honest status
V1 — the comprehension path — is **live**: ingest → first-call-correct tools →
access → direct call, including a full two-token on-chain subscribe against the real,
paywalled TxODDS API. The capability search is
**lexical** today; the vectorized semantic index, the docs→OpenAPI on-ramp, and the
auto-update "stay-correct" job are **designed for V2, not yet built** — and labeled as
such wherever they appear. Whether teams will *pay* for this is still being validated:
Gecko is a working comprehension layer, not yet a proven business.
