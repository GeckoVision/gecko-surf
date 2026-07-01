# Pegana: two ways an agent can reach it

**Pegana ships its own MCP (~6 hand-wrapped tools). Gecko comprehends Pegana's
OpenAPI and makes all 41 operations first-call-correct — alongside that MCP, not
instead of it.** Aggregate, don't replace.

Pegana is *the peg-risk oracle for Solana*: 63 pegged assets tracked through a
`PEGGED → … → BLACK_SWAN` state machine. Its team already
did the right thing and shipped an MCP — but by hand, so it exposes only the ~6
tools they had time to wrap. The rest of their REST surface — the by-mint state
lookup, discount history, methodology version, delivery-health stats — is
invisible to an agent unless someone writes integration code.

Gecko is that layer, without the code. Point it at Pegana's OpenAPI and the
**unmodified engine** turns the whole surface into question-shaped, first-call-
correct agent tools. Nothing Pegana already built is touched.

## The comparison

| | Reach |
|---|---|
| **Pegana's own MCP** | ~6 substantive tools (hand-wrapped highlights) |
| **Gecko comprehension** | **41 ops** ingested from the OpenAPI |
| | 26 surfaced to a public (no-auth) agent |
| | 15 auth-gated — hidden until a session can satisfy them |

The 41 / 26 / 15 split is computed live from the spec by the engine, not asserted
in prose. `26 + 15 = 41`; the 15 JWT-gated `/v1/me/*` ops stay hidden from a public
agent so it can't mis-call them.

## The scorecard (6/6, offline, $0)

```bash
uv run python examples/pegana_demo/demo.py     # renders the before/after + scorecard
uv run pytest examples/pegana_demo/ -q          # the same numbers, as a guard
```

Six representative tasks, each scored on retrieval (did the agent find the right
operation?) and well-formedness (did it build a valid request?):

```
top-1 100% · top-5 100% · well-formed 100%
```

No network, no spend, deterministic — synthesized from the committed fixture
(`tests/fixtures/pegana_openapi.json`) via the same engine path
`scripts/pegana_eval.py` uses.

## Two highlights an integration would get wrong

- **Mint vs symbol.** At decision time an agent holds a **mint address**
  (`J1toso1…GCPn`), not a ticker. Gecko routes it to
  `state_by_mint` → `/v1/assets/by-mint/{mint}/state`, **not** the sibling
  `/v1/assets/{symbol}/state` a naive integration reaches for first.
- **Auth boundary (JWT).** Forced to prepare a JWT-gated op on a public read,
  Gecko **refuses**: `prepare("list_subs")` → `CallError`. A public session never
  fires a `/v1/me/*` op. Auth is invisible to the agent *and* fails closed.

## Point Gecko at your OpenAPI

```bash
gecko https://api.pegana…/openapi.json     # → a first-call-correct MCP for the full surface
```

One command comprehends the spec and serves it over MCP, next to whatever MCP the
provider already ships.

## Honest box

- This is **comprehension proof, not willingness-to-pay.** It shows an agent can
  reach Pegana's whole surface correctly, first try — it does **not** prove anyone
  will pay for that. WTP is discovery-interview work, kept separate.
- **Drift re-ingest** (re-comprehend as the API changes) and the **correctness
  corpus** (every call teaching how to call the API right) are **Building**, not
  shipped.
- **Pegana's REST is free / no-auth today** — this is not a paywalled API. The only
  gated ops are the JWT-protected `/v1/me/*` set.

## Credit

Pegana, its API, and its MCP are built by the Pegana team. This example only
*reads* Pegana's public OpenAPI to make it agent-usable; Gecko stays control-plane
only and stores no Pegana response data.
