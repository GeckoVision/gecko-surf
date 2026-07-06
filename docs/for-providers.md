# For providers — make your API agent-ready, keep every cent

You bring the API. Gecko makes it the one agents **find**, **call right the first
time**, and — if you charge — **pay you for**, directly. You keep 100% of the revenue.

Gecko is a comprehension layer, not a middleman:

- It **aggregates** onto whatever you already have — your own MCP server, your docs,
  your endpoints all stay intact. Gecko never replaces them.
- It is **not a payment rail**: it composes onto *your* x402 endpoint, holds no funds,
  and signs nothing. The money moves between the agent and you.
- It **never re-lists your API** in a public catalog. Your surface is served because
  you asked for it, to the agents you point at it — not marketed to the world.
- Your developers **never pay Gecko** to use your API. (How the provider side is
  eventually priced is a separate, later conversation — see *Honest status* below.)

## The five moves

Bringing an API onto Gecko is five moves. Some are self-serve and free today; one is a
deliberate manual handshake (we don't want your credentials flowing through a form); a
couple are still hands-on while we build them toward self-serve. Every step is labeled
with what's real today.

```
  your API (an OpenAPI URL — or just human docs)
        │
        ▼
  1. COMPREHEND    paste a URL → first-call-correct tools        [live · self-serve · $0]
        │
        ▼
  2. ACCESS        hand us a sandbox key → we inject it at        [seam live · manual handshake]
                   call time; the agent never sees it
        │
        ▼
  3. SERVE         your surface hosted at /{you}/mcp + the        [live · founder-run today]
                   agent-native breadcrumbs, one add-command
        │
        ▼
  4. SETTLE        wire the tools to YOUR x402 endpoint —         [proven offline · live is early]
                   agents pay per call, you keep 100%
        │
        ▼
  5. STAY CORRECT  drift-watch keeps it first-call-correct as     [roadmap · V2]
                   you ship; you watch agent adoption
```

### 1. Comprehend — prove it works before you commit *(live · self-serve · $0)*

Paste your OpenAPI URL into the **Bring your API** page, `POST /comprehend`, or the
`comprehend_api` MCP tool. Gecko ingests the *surface* and hands back question-shaped,
first-call-correct tools plus a preview of your agent-ready surface. No account, no cost,
no commitment — you see your own API comprehended in seconds, and you can prove every
generated call is correct **offline, for $0** (recorded mode) before a single live call.

No OpenAPI? Point Gecko at your docs page — `gecko from-docs <url>` drafts a spec from
prose. It's born **quarantined** (untrusted input, treated as such) and reviewed before
it's ever served. This is how we onboarded a docs-only API (Jito) with no spec at all.

### 2. Access — the one manual handshake *(seam live · manual)*

Most real APIs are auth-gated, so this is the one step that needs a human exchange: you
hand us a **sandbox / test credential** and we agree the **safe scope** — which
operations agents may call. Gecko stores the credential as an *opaque reference* and
injects it at call time through a single seam (`access.py`); the agent describes intent
and never sees a token, in the tool defs or anywhere else.

Two things we settle together here, especially for money-moving APIs:

- **Sandbox, not production** — so agent calls touch no real funds or data while you
  evaluate.
- **Safe scope** — read operations are open by default; anything that moves money is
  excluded or **step-up-gated** by the risk gate, not handed to an arbitrary agent.

We keep this manual on purpose: credentials deserve a real handshake, not a casual form.

### 3. Serve — your surface, one add-command *(live · founder-run today)*

Gecko serves your comprehended surface over Streamable-HTTP MCP at `/{you}/mcp`, with the
agent-native breadcrumbs generated from the surface — `llms.txt`, `gecko.json`,
`.well-known/gecko.json`, `.well-known/x402.json` — so an agent can *discover* you, not
just call you. You get a single line to hand your developers:

```
claude mcp add --transport http you https://mcp.geckovision.tech/you/mcp
```

Your own MCP, if you have one, keeps running untouched — Gecko sits **beside** it.
Adding a new partner surface to the hosted deployment is **founder-run today** (a config
entry + a redeploy); self-serve hosting is on the roadmap.

> **This is the step that decides whether the run means anything.** Gecko *measures* the
> funnel — how many developers connected, made a first call, and came back — but it
> doesn't *create* the traffic. Put the add-command in front of your developers (docs,
> changelog, dev channel) and the numbers become real.

### 4. Settle — let agents pay you, keep 100% *(proven offline · live is early)*

If you charge agents per call, Gecko **composes** payment onto *your own* x402 endpoint —
it does not become the rail. The agent-facing tools are pointed at your endpoint; the 402
challenge, the funds, and the settlement are yours. Gecko holds nothing, signs nothing,
and **takes no cut**.

You see it work **before any real money moves**: with `X402_MODE=stub` the full 402
challenge → settle handshake runs offline, so you can falsify it on your own machine
first (the project's Pattern-B rule — the free local simulation is the first deliverable,
live is the final check). On-chain settlement (Solana / x402) is first-class and
end-to-end proven on devnet; **live settlement is early and hands-on**, and any
mainnet transaction is run by you, never by us.

The `.well-known/x402.json` we serve is honest by construction: every operation reads
`payment: "none"` until a real price flows from *your* data — Gecko never fabricates a
price, an address, or an endpoint.

### 5. Stay correct — the reason it's worth paying for *(roadmap · V2)*

Comprehension is a one-time win; **staying** correct is the recurring one. Because tools
are a pure function of the surface (`build_tools`, `gecko/tools.py`) and every surface
carries a content fingerprint (`surface_rev`, `gecko/surfaces.py`), drift is tractable:
when you rename a field or move a path, the tools move with it instead of silently
422-ing your consumers on a Saturday. Drift-watch re-comprehends on change and keeps the
surface first-call-correct, and an adoption view shows you connect → activate → return
per surface. The fingerprint and the metadata are built today; **the auto-update loop and
the dashboard are designed for V2, not yet shipped** — see [Stay correct](stay-correct.md).

## What we need from you

To take a real API from comprehended to a live, measured surface:

- [ ] **A sandbox / test API key** (+ the sandbox base URL) — unlocks the gated
      operations and keeps calls off production.
- [ ] **Your current OpenAPI spec** (or confirmation the docs URL is current) — so the
      surface reflects today's API, and drift is measured against a real baseline.
- [ ] **The safe scope** — which operations agents may call; what to exclude or
      step-up-gate (anything that moves money).
- [ ] **Distribution** — put the add-command in front of your developers. Without real
      external connects, the funnel stays empty and we both learn nothing.

That's the whole ask. **No production credentials, no build work on your side, no money
changing hands to start, no exclusivity.**

## The promises we hold

| Promise | What it means |
|---|---|
| **Control plane only** | Gecko stores your API's *surface* and correctness metadata — never response payloads, user data, or your secrets. |
| **Compose, never become, the rail** | Payment settles on *your* x402 endpoint. Gecko holds no funds, signs nothing, takes no cut. |
| **No public catalog** | Your surface is served to the agents you point at it — never re-listed or marketed as ours. |
| **Aggregate, not replace** | Your own MCP, docs, and endpoints stay intact. Gecko sits beside them. |
| **Auth is invisible to the agent** | Credentials are injected at call time; the agent never sees a token. |

## Honest status

Moves **1–3** are live (move 3's new-partner hosting is founder-run while we build it
toward self-serve). Move **4** is proven offline and early live — on-chain settlement is
validated on devnet; mainnet is founder-run. Move **5** — drift-watch and the adoption
dashboard — is designed for V2 and **not yet shipped**; it's labeled as such everywhere.
And the honest open question underneath all of it: whether providers will *pay* for
agent-readiness is still being validated. Gecko is a working comprehension layer that
your API can join today — not yet a proven business.
