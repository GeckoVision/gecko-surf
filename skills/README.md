# gecko-api-kit

**Make every API easily pluggable. Point Gecko at a provider's OpenAPI and its
*whole* surface becomes agent-usable — first-call-correct, served over MCP,
alongside whatever MCP the provider already ships.**

Three skills — two for **API providers** who want agents to actually *use* their
API, and one for **agent builders** who need to *consume* an untrusted API safely:

| Skill | For | What it does | Status |
|---|---|---|---|
| [`api-agent-ready`](skills/api-agent-ready/SKILL.md) | provider | Comprehend the API with `gecko` → emit the agent-native breadcrumbs → serve the full surface over MCP with a one-click add → make it discoverable. **Leaves the provider's own MCP intact.** | comprehend + serve **Live**; artifacts + discoverability **Building** |
| [`x402-payai-setup`](skills/x402-payai-setup/SKILL.md) | provider | Wire x402 micropayments onto the provider's API via **PayAI** — point the agent-facing tools at the provider's own x402 endpoint. **The provider keeps 100%.** | handshake/offline stub **Building (Pattern B)**; live settlement **Building / founder-gated** |
| [`anti-poisoning`](skills/anti-poisoning/SKILL.md) | agent builder | Protect your agent from a **poisoned API surface** — out-of-band trust anchor, spec-text/schema sanitizer, fail-closed auth-host firewall, quarantine. Treats every ingested spec as untrusted input. | **defenses Live** in the engine (free forever); hosted logs/analytics **Building (Cloud Pro)** |

Built by **[GeckoVision](https://geckovision.tech)**, the API-comprehension
company, on top of the open-source engine
[`gecko-surf`](https://github.com/GeckoVision/gecko-surf) (MIT, on PyPI).

## The one idea

Docs and endpoints are built for **humans**. Most providers can only hand-wrap a
handful of their endpoints into an MCP — so agents see a fraction of the API. The
long tail (the by-mint lookup, the history route, the methodology version, the
delivery-health stats) stays invisible unless someone writes integration code.

`gecko` is the layer that closes that gap **without the code**: it comprehends the
OpenAPI and turns the *full* surface into question-shaped, first-call-correct agent
tools, with auth hidden and injected at call time.

## Two rules this kit never breaks

1. **Aggregate, not replace.** We never touch the provider's own MCP. Gecko covers
   the full surface *alongside* it. (See [`rules/aggregate-not-rail.md`](rules/aggregate-not-rail.md).)
2. **Provider-pays, and Gecko is never the rail.** The developer uses the
   comprehension engine free; the provider is the customer. When you add payments
   we **compose** the x402 rail (PayAI, Metera, pay.sh) — we take **no cut** and we
   are **not** a marketplace. If a step drifts toward becoming a rail or a catalog,
   stop and re-read that rule.

## The worked example: Pegana (41 vs ~6)

Pegana — *the peg-risk oracle for Solana* — did the right thing and shipped an MCP,
but by hand, so it exposes only **~6 substantive tools**. Point `gecko` at Pegana's
OpenAPI and the unmodified engine ingests **41 operations** — **26** surfaced to a
public agent, **15** JWT-gated and hidden until a session can satisfy them. On a
6-task offline scorecard it scores **top-1 100% · well-formed 100%**, including the
two gotchas a hand integration gets wrong: routing a **mint address** to
`/v1/assets/by-mint/{mint}/state` (not the `{symbol}` sibling), and **refusing** a
JWT-gated `/v1/me/*` op on a public session. See the demo:
[`examples/pegana_demo/`](../examples/pegana_demo/).

Pegana's REST is **free / no-auth today** — this is not a paywalled API. That
worked example runs through this kit's `api-agent-ready` skill.

## Quickstart

Install the plugin from the **Marketplace** in Claude Code:

```
/plugin marketplace add GeckoVision/gecko-surf
/plugin install gecko-surf@geckovision
```

Then drive it:

- Command: `/make-agent-ready <openapi-or-docs-url>` — run the onboarding spine.
- Command: `/setup-x402 <api>` — wire the x402 rail via PayAI (offline first).
- Agents: `api-onboarding-engineer`, `x402-payments-engineer`.

The engine itself is a separate, explicit install you run yourself:

```bash
pip install "gecko-surf[serve]"
gecko https://api.example.com/openapi.json   # comprehend + serve over MCP
```

PyPI: https://pypi.org/project/gecko-surf/ · source:
https://github.com/GeckoVision/gecko-surf

## What's Live vs Building (honest)

**Live today (in `gecko-surf`):**
- Comprehend any OpenAPI 3.x → first-call-correct, question-shaped tools + a
  synthetic `search_capabilities` (intent → ranked endpoints).
- Serve the full surface over Streamable-HTTP MCP with a **one-click add**
  (`claude mcp add`, Cursor / VS Code deeplinks), behind an **SSRF guard**.
- `gecko test` (first-call-correctness checks) and `gecko from-docs` (recover a
  draft OpenAPI from a human doc page).
- **Anti-poisoning defenses** — out-of-band trust anchor, spec-text/schema
  sanitizer, fail-closed auth-host firewall, and quarantine (every ingested spec is
  treated as untrusted). Proven by the 7-attack showcase (`examples/poisoning_showcase/`,
  22 tests) and the battle-test (`gecko/redteam/`, naive ASR ~100% → defended 0%).
  **Free forever, in the engine — safety is never gated.**

**Building (do not present as shipped):**
- **Agent-native artifacts** — `llms.txt` breadcrumb, `x-gecko` spec annotations,
  `gecko.json`. Today these are a **hand-authored pattern** this kit documents;
  `gecko` does not emit them yet.
- **Discoverability** — a breadcrumb pattern, deliberately **not** a public
  catalog.
- **Drift re-ingest** and the **correctness corpus** (every call teaching how to
  call the API right).
- **x402 / PayAI settlement** — the auth/session seam exists; live settlement is
  **founder-gated** and defaults to `X402_MODE=stub`.
- **Hosted poisoning monitoring (Cloud Pro)** — logs of blocked attempts, trends per
  `surface_rev`, ASR/FRR over time, fleet triangulation, regression alerts. The
  *defense* is free forever; only this *observability* is a paid tier. "First 500" is
  a launch offer for the analytics, **never** a gate on protection.

## Scope — the boundary

This kit is the **comprehension / consumption** layer ("APIs get **USED**"). It is
**not** a payment rail (that's Metera / MCPay / PayAI — we compose them) and **not**
a marketplace (that's frames.ag / Bazaar). It is **control-plane only**: it stores
the API *surface* and correctness metadata — never response payloads, user data, or
secrets. That data-governance promise is what lets a provider onboard unilaterally.

## License

MIT.
