# Onboard your API to Gecko — the brief

*A concise, self-serve brief for a provider (or an agent acting for one) who wants to make
an API agent-usable through Gecko. Everything here links to the canonical docs; this page
is just the shortest path through them.*

**Canonical docs:** https://docs.geckovision.tech/for-providers

Gecko turns your API's *surface* into first-call-correct agent tools, handles auth
invisibly, and — if you charge — lets agents pay you directly. You keep 100%. Gecko is
not a payment rail, holds no funds, and never re-lists your API in a public catalog. It
**aggregates** onto what you already have; it never replaces your own MCP or docs.

## Do this now — free, offline, $0

Comprehend your API and see the agent-ready tools, no account, no commitment:

```bash
uvx --from "gecko-surf[serve]" gecko <your-openapi-url>
```

No OpenAPI? Point Gecko at your docs page instead — see
[from docs](https://docs.geckovision.tech/from-docs).

## The five moves

The full onboarding path, one line each — depth at the link:

1. **Comprehend** — paste a URL → first-call-correct tools, provable offline. *live · self-serve · $0*
2. **[Access](https://docs.geckovision.tech/access-and-auth)** — hand us a sandbox key; we inject it at call time, the agent never sees it. *the one manual handshake*
3. **[Serve](https://docs.geckovision.tech/discoverability)** — hosted at `/{you}/mcp` + agent-native breadcrumbs + a one-line add-command. *live*
4. **Settle** — wire the tools to *your own* x402 endpoint; agents pay per call, you keep 100%. *proven offline · live is early*
5. **[Stay correct](https://docs.geckovision.tech/stay-correct)** — drift-watch keeps it first-call-correct as you ship. *V2 roadmap*

Full detail: **https://docs.geckovision.tech/for-providers**

## Hand Gecko these four things

To go from comprehended to a live, measured surface:

- [ ] **A sandbox / test API key** (+ the sandbox base URL) — unlocks gated operations, keeps calls off production.
- [ ] **Your current OpenAPI spec** (or confirm the docs URL is current) — the surface reflects today's API.
- [ ] **The safe scope** — which operations agents may call; what to exclude or step-up-gate (anything that moves money).
- [ ] **Distribution** — put the add-command in front of your developers. Gecko *measures* the funnel; it doesn't *create* the traffic.

That's the whole ask. No production credentials, no build work on your side, no money
changing hands to start, no exclusivity.

## The guarantees we hold

Control-plane only (we store your API's *surface*, never payloads/secrets) · compose,
never become, the payment rail · no public catalog · aggregate, not replace · auth is
invisible to the agent. Detail:
[data governance](https://docs.geckovision.tech/status).

## Worked example — a provider handover

> **Pegana** wants their BRL↔BRS fintech API agent-ready. The handover:
> a **sandbox key** (their API is apiKey-gated — only ~26 of 41 ops are callable without
> one), their **current OpenAPI URL**, the **safe read scope** (exclude fund-moving ops
> from the tracer bullet), and the **add-command dropped into their dev channel** so
> their consumers actually connect. Then `funnel.py` measures connect → call → return —
> the first honest signal that agents are using their surface.

Point any provider at **https://docs.geckovision.tech/for-providers** and they can start
at move 1 themselves.
