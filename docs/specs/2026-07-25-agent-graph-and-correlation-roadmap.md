# The Agent Graph & Multi-API Correlation — Implementation Plan + Roadmap

> Successor track to the Skill Guard security layer (merged). This is the "second video"
> foundation: **any connected provider becomes a graph, and two+ graphs get a
> provenance-carrying correlation** — the multi-API correlation system shown e2e.

**One line:** context7 hands an agent *prose* about a library and the agent still guesses.
Gecko hands an agent the **deterministic call graph** (per-API) and the **provenance-tagged
correlation** between APIs — so it doesn't guess. "Graphify for APIs" (we graph API
**surfaces** with provenance + cross-API joins; Graphify graphs local **code**).

**The founder's two deliverables, mapped honestly:**
- **Determinism → the graph structure.** Per-API deterministic call graph. *Mostly built.*
- **Correlation → a confidence between two+ APIs ("this correlates to that — why?").**
  The confidence is the **provenance ladder**, NOT a learned float. "Category" = a
  **value-domain entity** (e.g. `solana-token-mint`). *The V2 frontier — partly built.*

---

## What already exists (build ON it — verified in the 5-lane review, 2026-07-24)

| Capability | Where | State |
|---|---|---|
| Per-API call graph (nodes + provenance-tagged edges) | `graph.py` `SurfaceGraph` | built |
| Graph as data / image | `surfaceviz.graph_data` / `render_svg`; `gecko graph json\|svg` | built (#195/#196) |
| Multi-provider join engine | `compose.py` `Workspace` + `cross_plan` (DECLARED-only) | built |
| DECLARED value-domain vocab (durable) | `hints.py` `~/.gecko/declared/<surface>.json` | built |
| Provider aggregator + intent routing | `paysh_catalog.CatalogRegistry` + `catalog_mcp.CatalogMcpSurface` | built |
| Surface façade | `surface.py` `Surface` (`.graph`, `.plan`, `.graph_data`, `.render_svg`) | built |

**The gaps this roadmap closes:** (1) the graph reaches agents only *implicitly* (a `plan`
glued onto `search_capabilities`) — no first-class "give me the graph" tool; (2) there is no
correlation **verdict object** exposed; (3) no provider **resolve** step (context7's
`resolve-library-id` analogue); (4) no e2e correlation showcase.

## Non-negotiable guardrails (from the review — do not violate)

1. **Confidence = provenance, not a float.** `DECLARED` (provider/customer-vouched) = high ·
   signature-corroborated = medium (**intra-API only**) · name-only = low/candidate. No
   learned/statistical score — that would re-import the *retired* corpus-moat framing
   ([[three-pillar-thesis]]) and isn't defensible or WTP-validated.
2. **Cross-API correlation is DECLARED-ONLY.** On real specs both name-equality AND
   value-domain signature were proven to fail *across* API boundaries (§13.6, Stripe×Adyen).
   Never mint an INFERRED cross-surface edge — that is the one-way data-integrity invariant.
   Cross-API is therefore effectively **binary**: DECLARED (they correlate, high) vs candidate
   (quarantined, a human confirms). Do not market a smooth cross-API number.
3. **Control-plane only (invariant #1).** Correlation inputs come ONLY from
   `{name, type, value-domain signature, provenance, declared entity}` — NEVER response
   values, sampled overlap, or observed cardinality (that needs traffic → breaks #1). Encode
   it as a typed rule on the scorer's input so a payload can't be fed in.
4. **Provider-DECLARED ≠ customer-confirmed.** A provider-DECLARED entity comes from an
   *untrusted* spec; today it sits at the same tier as a customer-confirmed one. A malicious
   spec could declare a false category to misroute a value. Provider-DECLARED-from-spec must
   be a **candidate** until the workspace owner confirms — the anti-poisoning gap to close
   before any cross-*org* correlation.
5. **No moat, and say so.** The correlation registry is re-derivable structure — a
   deterministic function of two surfaces + their declared vocabs. The only accreting asset is
   the DECLARED confirmations themselves (`hints.py`). Materialize edges as a *cache*, never
   call it a moat. Execution/integration advantage, not a data moat.
6. **API-agnostic engine (invariant #2).** No provider-specific code; a new provider is data
   + the adapter seam. Adding correlation must not touch `ingest`/`catalog`/`tools`/`caller`.

---

## Phase 1 — The graph as a first-class agent surface

**Goal:** an agent (or a context7-style consumer) can *ask for* the call graph, scoped.

- **1.1** A hidden, opt-in MCP tool `get_surface_graph` on `McpSurface` (pattern: the
  callable-but-not-enumerated `get_capability`/`query_docs` seam — keeps `tools/list`
  byte-identical, so the strict projection tests don't move). Returns
  `surfaceviz.graph_data(client.surface_graph)`: operations + provenance-tagged op→op `feeds`
  edges, each with join key + provenance + confidence. Structure-only (control-plane clean).
- **1.2** Scope it — return the **neighbors of a named op** or the **plan for a named target**,
  not a whole-graph dump (Stripe's ~337 edges would blow the token budget and evaporate the
  O(1)-at-scale advantage). Reuse the lightweight-ref discipline.
- **1.3** `Surface.graph_json()` / `gecko graph json|svg` already ship — this is the agent-facing
  twin.
- **Proof:** an agent asks "what feeds `getFixturesValidation`?" and gets the ordered supplier
  chain with provenance, without trial-and-error.

## Phase 2 — The correlation engine (the confidence, done honestly)

**Goal:** `correlate(surfaceA, surfaceB)` → a ranked, provenance-carrying set of
"A.output ↔ B.input" links, each with a human-readable **basis** (the "why").

- **2.1** `gecko/correlate.py` — `correlate_surfaces(a: Surface, b: Surface) -> CorrelationResult`.
  Plumbing seam only; the scoring reuses `graph.py` primitives (`_sig_of`,
  `_sig_corroborates`, `_entity_of`, genericity demotion) + the `hints.py` DECLARED vocab.
- **2.2** `CorrelationBasis` (frozen dataclass) rides each link — mirrors the existing edge
  provenance:
  ```
  entity ("solana-token-mint") · tier · provenance (DECLARED|INFERRED) ·
  src_surface/dst_surface · src_field/dst_param · signals (["declared-by:birdeye",
  "name-entity:token","pattern-eq"]) · weakest_origin (SPEC|DOCS) · confidence
  ```
  Human render: *"birdeye.address → dailynews.token: both value-domain solana-token-mint;
  birdeye DECLARED via x-gecko, dailynews name+pattern corroborates → tier 3, high."*
- **2.3** The ladder (all deterministic, reuse shipped code): tier 3 DECLARED (cross-API,
  plan-eligible) · tier 2 EXTRACTED-sig (**intra-API only** — §13.6 gate) · tier 1
  INFERRED-name (intra only; cross-API → quarantined candidate) · tier 0 none. **Type is a
  hard gate**, not a term.
- **2.4** `Surface.correlate(other)` façade + `gecko correlate <specA> <specB>` CLI (thin, like
  `gecko graph`) + a `correlate_surfaces` tool on the **aggregator** (`CatalogMcpSurface`
  holds N clients; single `McpSurface` holds one and can't correlate).
- **2.5** Wire the guardrail-4 split: provider-DECLARED-from-spec → candidate until confirmed.
- **Semantic tier stays OUT** (Step-7 verdict): the measured gap is a *declaration* gap
  (Adyen ships bare `type:string`), not a synonym gap — embeddings would guess the join the
  gate exists to forbid. A semantic tier, if ever admitted, may only *propose* tier-1
  candidates for a human to confirm into DECLARED; never mint tier ≥2.

## Phase 3 — Multi-provider registry (the "resolve" step)

**Goal:** "any connected provider becomes a graph," enumerable/resolvable like context7.

- **3.1** Promote `SurfaceRegistry` from in-memory to a `surface_rev`-keyed persistent store
  (content-addressed; a re-comprehend bumps the rev and auto-invalidates cached edges); make
  the declared vocab addressable by `surface_id` inside it.
- **3.2** `resolve_provider(name)` / `enumerate_providers()` on `CatalogMcpSurface` (the
  `resolve-library-id` analogue) — resolve **within** a `CatalogRegistry`, NOT a global public
  index (exposing `ids()` = becoming a marketplace, a hard non-goal).
- **3.3** The value-domain index: `entity → [(surface, op, field)]` — a plain deterministic
  lookup (no vectors; §13.6 proved fuzzy matching manufactures false cross-API edges). Answers
  "what correlates with birdeye's token endpoints across all connected providers."
- **3.4** Correlation edges stay **stateless** (computed on demand from two loaded graphs);
  materialize only as a `(rev_A, rev_B, declared_rev)`-keyed cache when the O(surfaces²) scan
  actually hurts — justify in that PR (invariant-3 discipline).

## Phase 4 — The e2e showcase (Video 2)

**Goal:** a multi-API correlation system working end-to-end, honestly.

- **4.1 Falsifiable proof first (Pattern B, $0 offline)** on the existing **Birdeye × daily-news**
  testbed ([[birdeye-daily-news-testbed]]): with NO DECLARED hint → the token join stays a
  **quarantined candidate** (tier ≤1, matches §13.6); add one DECLARED entity
  `solana-token-mint` on both sides → it jumps to **tier 3** and `cross_plan` recovers the
  cross-API chain **first-plan-correct**. Two asserts; falsified if a bare-name match reaches
  high confidence without DECLARED.
- **4.2 The demo** (`demo/kit`, honesty rules = law): "evaluate the APIs you're using → divide
  by **category** (value-domain) → show which link and which don't, with confidence + basis."
  Two real APIs, the graph of each, the cross-API correlation with its provenance explain, and
  the agent making a first-plan-correct chained call across both. This is the "GraphRAG for
  APIs" nodes-interacting picture, grounded and honest.

---

## Roadmap sequence & tie to the two videos

1. **Video 1 — Security (Skill Guard).** Merged. Ships after red-team validation confirms the
   honest catch story (image-injection via OCR/metadata; base64/LSB are disclosed residuals).
2. **Phase 1 → Phase 2 → Phase 3 → Phase 4** (this doc). Each phase is one or more reviewed
   PRs; scoring internals route through `ai-ml` + `defi-security` (the anti-poisoning
   guardrails). Correlation touches the quarantine/trust posture → defi-security gate.
3. **Video 2 — the multi-API correlation showcase** (Phase 4). The e2e "second video."

## Out of scope (named, not silently dropped)
- A numeric/ML correlation **score** (confidence is provenance; a black-box number is neither
  defensible nor WTP-validated).
- Vectorized retrieval (evidence-gated; the deterministic value-domain index is the default).
- A public cross-provider catalog / marketplace (hard non-goal — we consume, never re-list).
- Traffic/response-value-derived correlation (breaks control-plane invariant #1).
