---
name: api-agent-ready
description: Make ANY API agent-ready without integration code — design/harden the surface with the agent-readiness best-practices checklist (one canonical read, field-complete, clear enums/required fields, machine-authable auth), comprehend its OpenAPI/docs with gecko, emit the agent-native artifacts (llms.txt breadcrumb, x-gecko spec annotations, gecko.json), serve its FULL surface over MCP with a one-click add, and make it discoverable — while leaving the provider's own MCP intact (AGGREGATE, not replace). For API providers who want their whole API usable by agents first-call-correct, not just the handful of endpoints they hand-wrapped. Solana/x402-flavored for the first market, but the capability is any-API. Use when building, hardening, or onboarding a provider's API to the agent ecosystem. NOT a payment rail and NOT a marketplace.
user-invocable: true
---

# API Agent-Ready Skill

> **For the provider.** Every other agent skill teaches how to *call one* API.
> This skill takes an API you *own* (or one you want agents to use) and makes its
> **whole surface** agent-usable — first-call-correct, over MCP — without writing
> integration code, and **without touching the MCP the provider already ships**.

## What this skill is for

A provider's API is built for humans: prose docs, an auth handshake assumed, units
implied, a spec with dozens–hundreds of operations. The provider can only hand-wrap
a few endpoints into an MCP, so agents see a **fraction** of the API. The long tail
stays invisible unless someone writes glue.

This skill closes that gap with `gecko`, the open-source comprehension engine. You
run a **spine** that turns an OpenAPI (or a doc page) into a first-call-correct MCP
for the *full* surface, emits the breadcrumbs agents use to *find* it, and serves it
with a one-click add — **alongside** whatever MCP the provider built.

If the provider is still **building or hardening** the API, start one step earlier:
**Step 0 — Design for agents** ([best-practices.md](best-practices.md)) is a checklist
for shaping endpoints agents consume well (one canonical read, field-complete, clear
enums/required fields, a machine-authable auth path) *before* Gecko comprehends them.
A surface designed to that checklist lands first-call-correct because it's
unambiguous — not because a layer papered over the ambiguity.

**Designed to the checklist? Measure it.** `gecko inspect <api>` scores your API's
agent-readiness across four dimensions and prints located, fixable findings — the
best-practices checklist made **automatic**, and a CI gate you can fail a deploy on
([inspect.md](inspect.md)).

**"Make every API easily pluggable."**

## The spine

Pick the step you're on; load only the file you need (progressive, token-efficient).
Step 0 is provider-side design work; steps 1–3 are where Gecko acts; 4–5 are the
discipline that keeps it in lane.

| # | Step | Read | Status |
|---|---|---|---|
| 0 | **Design for agents** — the API best-practices checklist (do this while building) | [best-practices.md](best-practices.md) | provider-side guidance |
| ✓ | **Inspect** — score agent-readiness + gate CI (the checklist, automated) | [inspect.md](inspect.md) | **Live** (≥ 0.4.4) |
| 1 | **Comprehend** the OpenAPI/docs → first-call-correct tools | [comprehend.md](comprehend.md) | **Live** |
| 2 | **Emit artifacts** — `llms.txt`, `x-gecko`, `gecko.json` breadcrumbs | [artifacts.md](artifacts.md) | **Building** (hand-authored pattern) |
| 3 | **Serve MCP** — Streamable-HTTP + one-click `claude mcp add` | [serve-mcp.md](serve-mcp.md) | **Live** |
| 4 | **Make discoverable** — breadcrumb, not a public catalog | [discoverable.md](discoverable.md) | **Building** |
| 5 | **Aggregate, not replace** — never touch the provider's own MCP | [aggregate-not-replace.md](aggregate-not-replace.md) | invariant |

Get all of them and the provider's *entire* API is usable by an agent, first try —
not just the endpoints they had time to hand-wrap. New to the kit and want the whole
path end to end? [best-practices.md](best-practices.md) closes with a **"How a
provider uses this kit"** walk-through (install → `/make-agent-ready` → checklist →
discoverable → x402).

This skill also ships:
- **Command** — [`/make-agent-ready`](../../commands/make-agent-ready.md): run the
  onboarding spine on an OpenAPI or docs URL and emit the served MCP + add strings.
- **Agent** — [`api-onboarding-engineer`](../../agents/api-onboarding-engineer.md):
  a specialist that takes a provider API and returns it agent-ready.
- **Rule** — [`aggregate-not-rail`](../../rules/aggregate-not-rail.md): never replace
  the provider's MCP; never become the rail or a marketplace.

## The engine

```bash
pip install "gecko-surf[serve]"
# zero-install alternative:
uvx --from "gecko-surf[serve] @ git+https://github.com/GeckoVision/gecko-surf" gecko <spec>
```

One command comprehends **and** serves:

```bash
gecko https://api.example.com/openapi.json     # == gecko serve <spec>
```

It prints the comprehension summary (operations ingested, tools generated), the MCP
URL, and a one-click add for Claude Code / Cursor / VS Code. Also available:
`gecko inspect <spec>` (score agent-readiness + gate CI — see [inspect.md](inspect.md)),
`gecko test <spec>` (first-call-correctness checks) and `gecko from-docs <src>`
(recover a draft OpenAPI from a human doc page, then comprehend).

PyPI: https://pypi.org/project/gecko-surf/ · source:
https://github.com/GeckoVision/gecko-surf

## The worked example: Pegana (41 vs ~6)

Pegana — *the peg-risk oracle for Solana* — shipped its own MCP by hand: **~6
substantive tools**. Point `gecko` at Pegana's OpenAPI and the unmodified engine
ingests **41 operations** — **26** surfaced to a public agent, **15** JWT-gated and
hidden until a session can satisfy them (`26 + 15 = 41`, computed live from the
spec, not asserted). On a 6-task offline scorecard: **top-1 100% · well-formed
100%**, including the two gotchas an integration gets wrong:

- **Mint vs symbol** — an agent holds a mint address (`J1toso1…GCPn`); Gecko routes
  it to `state_by_mint` → `/v1/assets/by-mint/{mint}/state`, not the `{symbol}`
  sibling.
- **Auth boundary** — forced to prepare a JWT-gated op on a public read, Gecko
  **refuses** (`prepare("list_subs")` → `CallError`). A public session never fires a
  `/v1/me/*` op.

Pegana's REST is **free / no-auth today** — not a paywalled API. This is
**comprehension proof, not willingness-to-pay**; keep those separate.

## The boundary (what this is and isn't)

This is the **comprehension / consumption** layer — it makes an API *usable*. It is
**not** a payment rail and **not** a marketplace; it composes on MCP/x402 and
consumes a spec as input. It is **control-plane only**: it stores the API *surface*
and correctness metadata — never response payloads, user data, or secrets. That
governance promise is what lets a provider onboard unilaterally.

Want to charge agents for calls? That's the sibling skill,
[`x402-payai-setup`](../x402-payai-setup/SKILL.md) — compose the rail, take no cut,
provider keeps 100%.

## Provider

Built by **[GeckoVision](https://geckovision.tech)** — the API-comprehension
company. Engine: [`gecko-surf`](https://github.com/GeckoVision/gecko-surf) (Apache-2.0) ·
https://pypi.org/project/gecko-surf/.
