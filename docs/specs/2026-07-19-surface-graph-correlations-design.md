# Surface Graph — correlations & multi-call planning (V2 design)

> Status: DESIGN — the V2 "correlations" frontier from the three-pillar thesis.
> Provenance: adapted from the Graphify (YC S26) reference architecture per the
> 2026-07 evaluation. Graphify graphs local *code*; we graph *API surfaces* —
> it is a reference, not a drop-in. Gated behind the falsifiable probe (§7).

## 1. Problem

The catalog answers **one-call** intents: "get fixture odds" → one endpoint.
Real agent tasks are **chains**: *"prove England scored"* requires
`scores/snapshot/{fixtureId}` (to learn `Seq`) → `stat-validation?fixtureId&seq&statKey`.
Today the agent discovers chains by trial and error — exactly the
wrong-call-first behavior Gecko exists to remove. The comprehension gap has
moved from "which endpoint" to "which *sequence* of endpoints", and the flat
catalog cannot see sequences.

Live evidence (2026-07-18 funnel read): agents connect to the hosted surface
and rarely progress to a call. The blind segment between "saw the tools" and
"made a call" is where chain-shaped intents die.

## 2. The three patterns adopted (and their sources)

1. **Deterministic, LLM-free extraction.** The graph is built from
   `ingest.py`'s normalized `Operation`/`Param` — never from raw spec text and
   never by an LLM. Same spec in → byte-identical graph out. (Graphify's
   deterministic extract; also our own anti-poisoning posture: ingested spec
   content is untrusted input.)
2. **EXTRACTED vs INFERRED provenance on every edge.** Facts the spec states
   (a response schema field, a path parameter) are `EXTRACTED`. Links we
   *derive* (name/type match between an output field and another operation's
   input) are `INFERRED` with a recorded basis. The two never mix silently —
   a poisoned spec can at worst poison its own extracted facts, and inference
   is auditable and disableable. (Graphify's provenance split, reframed as an
   anti-poisoning control.)
3. **Query-path-explain.** A chain answer is returned *with its explanation*:
   which edges, which provenance, which parameters flow. The agent (and a
   human reviewing a failure) can see *why* Gecko proposed the sequence.
   (Graphify's path-explain, repurposed as multi-call planning output.)

## 3. Non-goals (the guardrails, verbatim from the evaluation)

- **Surface, not traffic.** Nodes/edges derive from the API surface only.
  No call outcomes, no payloads, no usage data — the control-plane invariant
  is untouched. (A future correctness-corpus edge weight is a separate,
  explicitly-gated decision — not this spec.)
- **Mechanism, not repositioning.** This is an internal engine improvement to
  first-call-correctness on chain-shaped intents. No "knowledge graph"
  positioning, no new product noun, no public catalog.
- **Not vectors.** Graph traversal complements lexical search; the semantic
  retrieval flip stays evidence-gated per the context-engineering canon.

## 4. Data model

New module `gecko/graph.py` (engine, API-agnostic — invariant #2 holds: adding
API #2 must not touch it).

**Nodes**
| Kind | Source | Example |
|---|---|---|
| `operation` | ingest | `getApiScoresStat-validation` |
| `param` | ingest | `seq` (query, int32, required) |
| `field` | response schema walk (depth/cycle-guarded, reusing ingest's guards) | `Scores.seq` |
| `resource` | path-segment nouns, deterministic stemming | `fixtures`, `scores` |

**Edges**
| Edge | Provenance | Basis |
|---|---|---|
| `operation --consumes--> param` | EXTRACTED | spec parameter list |
| `operation --produces--> field` | EXTRACTED | response schema |
| `operation --on--> resource` | EXTRACTED | path template |
| `field --feeds--> param` | **INFERRED** | recorded basis: the **v3 entity basis** (§10 — probe-derived; the two simpler bases were falsified 2026-07-19); never fuzzy-semantic in v1 |
| `operation --paginates--> operation` | EXTRACTED where the spec declares it; INFERRED from cursor-param patterns otherwise | |

Every INFERRED edge carries `{basis, confidence ∈ {high, low}}`. Serialization
is deterministic (sorted, content-addressed) so graph diffs are reviewable and
a drifted spec produces a reviewable graph diff.

## 5. Query path: intent → plan

`catalog.search()` stays the entry point. When the top operation's **required
inputs are not satisfiable from the agent's stated intent**, the planner walks
`feeds` edges backwards to find supplier operations, and returns a **plan**:

```
plan:
  1. GET /api/scores/snapshot/{fixtureId}     # supplies: seq   [EXTRACTED produces]
  2. GET /api/scores/stat-validation          # consumes: fixtureId, seq, statKey
explain:
  seq: Scores.seq --feeds--> stat-validation.seq   [INFERRED: exact-name+type, high]
```

Surfaced to agents as an enrichment of the existing projections
(`search_capabilities` gains an optional `plan` block; the tool contract stays
the single source of truth per the agent-native surface design). Plans are
**suggestions with provenance** — the agent still makes the calls directly
(invariant: Gecko never becomes the data plane).

Depth cap: plans of length ≤ 3 in v1. Anything deeper returns the honest
"no confident plan" rather than a speculative chain.

## 6. Auth & modes

Plans respect the existing seams untouched: every step's call goes through the
same `prepare`/`call` path, auth injected per step at call time (invariant #4),
recorded mode synthesizes each step's response so **a whole plan is
falsifiable offline at $0** — the plan's `feeds` edges tell `sample.py` which
synthesized output fields must flow into the next step's params.

## 7. The falsifiable probe (build gate — run BEFORE committing to the build)

One scripted, offline, $0 probe decides whether this earns the build:

1. Build the graph for the **TxLINE spec** (the painful API we know best) and
   **Stripe** (the well-documented control).
2. Assert the known chains exist as discoverable plans, without hand-hints:
   - `fixtures/snapshot → odds/updates/{fixtureId}` via `FixtureId`
   - `scores/snapshot/{fixtureId} → stat-validation` via `seq` (+`fixtureId`)
3. Count false plans (INFERRED edges that connect semantically unrelated
   params — e.g. every API's `limit` feeding every other `limit`). The known
   failure mode is generic-name over-linking; the probe must measure it, and
   a stoplist (`limit`, `offset`, `id`-bare, `page`) is part of v1.
4. **Gate:** both known chains found AND false-plan rate on the probe corpus
   under ~10% → build proceeds. Otherwise the inference basis is too weak;
   stop and rethink rather than ship confident nonsense.

Success metric for the shipped feature mirrors the ASR north star: chain-shaped
eval tasks in `gecko test` (multi-step suites) go from "agent flails" to
"first-plan-correct", measured in the recorded mode suites.

## 8. Build shape (after the probe passes)

- `gecko/graph.py` — build + serialize + query (pure, no I/O; ~the size of
  catalog.py, split if it passes ~300 lines)
- `catalog.py` — planner hook (flat search untouched; plans only when required
  inputs are unsatisfied)
- `mcp_server.py` — thin projection of the `plan` block (no logic)
- `sample.py` — recorded-mode plan chaining (output field → next input)
- Tests: probe corpus as fixtures; determinism test (same spec → identical
  bytes); poisoned-spec test (malicious `feeds`-bait fields stay quarantined
  to INFERRED with their basis visible)

Explicitly *not* in v1: corpus-weighted edges, vector similarity for `feeds`
inference, any UI. Cross-API edges are **phase 2, not a non-goal** — see §8.5.

## 8.4 Docs as input — the full Graphify analogue

Graphify runs on local docs; our analogue is not limited to OpenAPI. The
`gecko from-docs` pipeline already turns human docs pages into normalized
`Operation`s — and **the graph builder consumes normalized Operations, so
docs-derived surfaces graph for free**. What changes is trust, and it must be
explicit:

- **Node-level provenance joins edge-level provenance.** Operations originate
  as `SPEC` (OpenAPI, deterministic parse) or `DOCS` (from-docs extraction,
  inherently softer). A plan is only as trustworthy as its weakest node, and
  the plan's `explain` block must surface it: a chain through a `DOCS`-origin
  operation says so.
- **Same anti-poisoning posture, higher stakes.** Docs pages are the most
  attacker-writable input we ingest. `DOCS`-origin nodes never *upgrade* an
  INFERRED edge's confidence, and `feeds`-bait quarantine (§8 tests) runs on
  docs-derived corpora too.
- This is the wedge restated: the painful APIs — the ICP — are exactly the
  ones *without* a clean OpenAPI. If the graph only worked on well-specified
  APIs, it would work best where it is needed least.

## 8.5 Cross-API correlation — phase 2, the ICP-shaped payoff

The ICP is teams running **multi-API agents**, and their real chains cross
APIs (fetch a price from API A → act on API B; resolve an id in one system →
query it in another). Single-API plans (v1) prove the mechanism; cross-API
plans are what the customer actually runs. Phase 2 extends the same model, no
new machinery:

- **Cross-API `feeds` edges** use stricter bases than intra-API ones: shared
  *entity* identity (the same well-known resource — a token address, a ticker,
  a fixture id — appearing as one API's output and another's input), never
  bare name+type match (the generic-name over-linking risk squares across
  APIs; the §7 stoplist becomes mandatory, not advisory).
- **`DECLARED` hints get their strongest use here**: a provider or the
  customer declaring "our `fixture_id` is TxODDS's `FixtureId`" is one line
  of x-gecko metadata that beats any inference.
- **Per-workspace, never global** — the day-one model holds: correlations are
  computed inside a customer's own set of ingested surfaces. There is no
  public cross-API graph, no catalog. Two customers ingesting the same two
  APIs each get their own graph. (This also keeps the no-public-catalog
  discipline from becoming accidentally violated by a "helpful" shared graph.)
- **Probe extension before phase 2 builds:** a two-API falsifiable case with a
  known-true cross link and counting false cross links — same gate logic
  as §7, stricter bar (the cost of a wrong cross-API plan is an agent calling
  the wrong *system*, not just the wrong endpoint).

## 9. Open questions (decide at probe time)

- Should `x-gecko` sequence hints (provider-declared chains) be a third
  provenance class `DECLARED` — trusted more than INFERRED, less than
  EXTRACTED? (Leans yes; providers annotating chains is the for-providers
  story meeting the graph.)
- Plan caching: plans are derivable at ingest time for common intents —
  precompute per surface, or walk per query? (Measure at probe scale first;
  premature either way.)

## 10. Probe results (run 2026-07-19 — gate NOT yet passed, two bases falsified)

The §7 probe ran the same day this spec was written, offline, $0, against the
full TxLINE spec (18 operations) and Stripe `spec3.json` (587 operations, the
rich-API control). Two inference bases were tried; **both failed the gate**,
each teaching a different thing. Per the gate's own rule, the build does not
proceed on either — the findings define the v3 basis that must pass first.

| Basis | TxLINE | Stripe (control) |
|---|---|---|
| **v1** exact-name + type match | ✅ both known chains found, no hand-hints; the 169-edge eyeball set is dominated by genuinely valid flows | ❌ 66,984 edges — `created` alone produces 24,050; `status` 11,256; `type`, `currency`, `object` follow. Generic *state/filter* fields, not identities |
| **v2** id-shaped params only (path-param names or `*id`) | ❌ loses the `seq → stat-validation` chain — legitimate flow keys are not always ids | ❌ 64,699 edges — Stripe's bare `{id}` path param is **polymorphic** (every resource's `id`), so it matches every produced `id` field across unrelated resources |

**What each failure teaches:**

- Stripe's failure ⇒ identity needs **resource scope**. `Customer.id` feeds
  customer-path params and nothing else; a bare `id` name carries no entity by
  itself — the entity lives in the *parent object* of the produced field and
  the *resource noun* of the consuming path.
- TxLINE's v2 failure ⇒ legitimate flow keys are **not always id-shaped**
  (`seq`). What actually distinguishes `seq` from `created` is **statistical
  rarity**: `seq` is produced/consumed in a handful of places; `created`
  appears in thousands. Genericity is measurable from the graph itself — no
  hand-maintained stoplist required.

**The v3 basis (the build prerequisite):** a `feeds` edge requires

1. **entity naming** — the field name itself carries the entity
   (`fixtureId` = fixture + id), matched to a same-entity param; **or**
2. **scoped bare id** — a bare `id` field whose parent schema's object/resource
   matches the consuming operation's path resource noun; **and in all cases**
3. **statistical genericity demotion** — a name produced by more than a small
   fraction of the API's operations is auto-demoted to `low` confidence and
   excluded from plans (this replaces the §7 manual stoplist, which becomes a
   seed list only).

§7's gate re-runs with v3 on the same two specs plus the same eyeball
protocol. If v3 also fails on the control, the honest conclusion is that
lexical inference alone cannot carry `feeds` at rich-API scale and the design
must lean on `DECLARED` hints (§9) — which changes the product motion
(provider/customer annotation) and should be decided consciously, not slid
into.

## 11. Probe re-run with v3 — PASSED, build proceeds (2026-07-19)

The §7 gate re-ran with the v3 basis, same two specs, same eyeball protocol.
**v3 passes cleanly. The build is greenlit.**

| basis | TxLINE chains | Stripe (control) edges |
|---|---|---|
| v1 name+type | 1 of 2 | 66,984 |
| v2 id-shaped only | 1 of 2 | 64,699 |
| **v3** | **both found** | **337** (−99.5%) |

The v3 basis as implemented (three moves, all measurable from the surface, no
hand-maintained stoplist):

1. **Entity ids are the API's spine — exempt from genericity.** A name that is
   an entity id (`fixtureId` → entity `fixture`; a bare `id` scoped by its
   parent object) links only field→param of the *same* entity. `FixtureId`
   appearing in most TxLINE ops is not noise — it is the join key, and the
   entity scope already prevents over-linking. This was the v3-draft bug the
   probe caught: a naive genericity fraction demoted `FixtureId` on the small
   API and lost chain 1.
2. **Genericity demotes non-id names by produce OR consume frequency, floored.**
   `generic_t = max(4, ceil(0.03 · n_ops))`. The floor of 4 is load-bearing: on
   an 18-op API a pure fraction makes anything in ≥1 op "generic" (1/18 = 5.5%)
   and kills `seq` (chain 2). Consume-frequency is the other half — `limit` is
   *produced* by few Stripe ops but *consumed* by 381, so a produce-only check
   missed it; consume-frequency demotes it.
3. **Non-id flow keys must be id-shaped (number/string).** Drops boolean/enum
   false links (`shippable`, and similar) that share a name across unrelated
   ops. Took Stripe 374 → 337.

**Honest residue.** The 337 Stripe edges are dominated by legitimate
resource-named ids (`client_reference_id`, `transaction`, `product`, `payout`,
`payment_intent`, `location`, `meter` — Stripe names ids by resource, without an
`Id` suffix). A small residue of string-enum filters (e.g. `device_type`)
remains; a `feeds` *edge* is not a *plan* (a plan forms only when an agent's
intent needs that chain), and `DECLARED` hints (§9) plus the same type/entity
refinements shrink the residue further. This is well within the gate's bar
("both chains found AND a small fraction on the control"), which v3 clears by
two orders of magnitude versus v1/v2.

**Decision:** build v1 of the graph on the v3 basis (§8). The probe script is
`scripts/surface_graph_probe.py` (offline, $0, deterministic; needs the TxLINE
spec and a Stripe `spec3.json`). The `DECLARED`-hint fallback (§9) is NOT
triggered — lexical inference on the v3 basis carries `feeds` at rich-API scale.

## 12. Checkup revision (2026-07-19) — reordered build + the real-comprehension join

A five-lane read-only audit (architecture, comprehension, code+test, storage,
CI) ran after `gecko/graph.py` v1 landed. It changed the build order and
resolved the open questions. **The headline: the multi-API graph is not the
first thing to build — it is the fourth.** Three findings force the reorder:

1. **The graph is orphaned — it reaches no agent.** `build_graph`/`plan` are
   imported nowhere but their own tests; `catalog.search` has no planner hook,
   `mcp_server` surfaces no `plan` block. By our own "wired ≠ reaches the agent"
   rule, chain comprehension is currently dark. §5 is designed, not wired.
2. **Chain-correctness is unmeasured.** `fcc_eval.py` scores one tool pick;
   `test_graph.py` asserts plan *structure* but never *executes* a plan. The §6
   "a whole plan is falsifiable offline at $0" promise is unbuilt. We cannot
   honestly claim first-plan-correct for anything until this exists.
3. **Two inference bugs will fire on API #2** (must fix before any measurement,
   or they corrupt it): (a) the `endswith("id")` entity heuristic misfires on
   English words (`paid`→`pai`, `valid`, `void`, `uuid`, `grid`) and those take
   the entity-match branch, which — unlike the non-entity branch — skips the
   `_ID_TYPES` filter, so a boolean field can mint a *high*-confidence `feeds`
   edge; (b) `_required_inputs` sees only `path`/`query` params, so a join key
   carried in a **request body** (every "create X referencing Y's id" chain) is
   invisible — which kills exactly the multi-API mutate chains the ICP runs.

### The reordered plan
- **Phase 1 (now) — make the single-API chain real AND measured.** Fix the two
  bugs → build the **chain-FCC harness** (recorded-mode plan executor: run each
  `PlanStep` through the existing `prepare`/`call` path, thread step N's
  synthesized output field named by the edge into step N+1's param, and score
  the whole chain *well-formed* + *value-kind-correct* — the chain analogue of
  `fcc`, $0, no live calls, no new corpus) → seed it with the two known TxLINE
  chains → **wire `plan()` into `catalog.search`** and prove it reaches the
  agent with a direct MCP probe. This is also the demo of the single-API a-ha.
- **Phase 2 — de-risk the code the graph must extend.** Split `client.py` (721
  lines) before wiring plans through it; peel the spec→scheme derivation out of
  `access.py`; split `graph.py` into model/build/plan + an adjacency index.
- **Phase 3 — the one-way foundation decision.** Node ids and the `Node`
  dataclass gain a **`surface_id`** namespace (today's ids feed `content_hash`,
  so this is a one-way contract — get it right first, before any cross-API
  edge). `PlanStep` gains per-step surface/host (a cross-API plan is otherwise
  unexecutable — the agent won't know which API each step hits). **Compose N
  per-API graphs at query time — never merge into one** (genericity frequency
  is computed per-union today, which pollutes each API's inference).
- **Phase 4 — multi-API, gated by a two-API probe** (§8.5): known-true cross
  link found AND false-cross-link count under a *stricter* bar than §7, because
  the cost of a wrong cross-API plan is the agent calling the wrong **system**.

### The cross-API join is REAL comprehension, not string-match (the crux)
This is Graphify for API *surfaces*, and it is one of the hardest, most
defensible pains — so the join must be genuine entity identity, ranked by a
**trust ladder**, never coincidental name equality:

1. **`DECLARED` (highest)** — a provider's `x-gecko` hint or a customer saying
   "our `fixture_id` == TxODDS `FixtureId`". One line of metadata beats any
   inference. *(Resolves §9: yes, `DECLARED` is a third provenance class, ranked
   `EXTRACTED > DECLARED > INFERRED`.)*
2. **`EXTRACTED` entity identity** — the same **well-known resource** (a token
   address, a ticker, a fixture id) appearing as one API's typed output and
   another's typed input, matched on entity, surface-scoped on both sides.
3. **`INFERRED` name-match — demoted to `low`/quarantined across APIs.** Bare
   name+type equality is the *least* portable signal between two independent
   naming conventions; across APIs it silently under-links where it should chain
   and over-links where two APIs share a generic id name. It is never a
   cross-API plan's basis unless a `DECLARED` hint confirms it.

Every cross-API edge records `surface_id` on both endpoints, its basis, its
confidence, and the node-origin (`SPEC`|`DOCS`, §8.4) of its weakest node — so a
plan's `explain` block can show *why two systems were chained* and a reviewer
can audit it. This provenance ladder IS the anti-poisoning control extended to
the cross-API surface. **Per-workspace, never global** (§8.5) still holds.

*(Resolves §9 plan-caching: cache each surface's serialized graph keyed by
`surface_rev` — it is a pure function of the spec and already control-plane-clean
bytes (`serialize()`); compose the small N a workspace needs at query time. No
DB, no vectors — the evidence bars for either (a real multi-API false-link rate
over the §7 gate AND per-workspace op count past the lexical ceiling) are not
met, so both stay out.)*

## 13. Cross-API entity-identity foundation (Phase 3/4 design)

Answers the founder's foundation question: today's join is `fixture == fixture`
(exact normalized-name equality, `graph.py:408` — `_entity_of`/genericity are only
demotion filters on top). That holds intra-TxLINE because one team named
everything `FixtureId`; across two independently-authored APIs it fails both ways —
**over-links** shared generics (`id`, `status`, `currency`) and **cannot express**
the same entity under different names (`FixtureId` vs `matchId` vs `event_id`). The
foundation must stop keying on the *name* and key on the **value domain** the field
ranges over — the deterministic, naming-convention-independent expression of "same
entity."

### 13.1 The entity signature (deterministic, no model)
Per field/param, derived from the resolved schema the ingest already keeps but the
graph extractor discards (`_response_leaves`, `graph.py:141-143` — capture fix, not
a re-ingest):
- **`pattern`** (regex) — a shared rare regex (`^0x[a-f0-9]{40}$`, `^[A-Z]{3}$`) is
  near-conclusive same-value-domain — the **highest**-entropy signal.
- **`enum`** — set overlap over a rare domain (ISO-4217 currencies, chain ids),
  weighted by rarity/cardinality — high.
- **`format`** — `uuid`/`uri`/`email`/`ipv4` discriminating; `int32`/`date-time`
  common (low weight).
- **`example` shape**, **structural origin** (which resource/response it came from)
  — corroboration.
- **name-entity** (`_entity_of`) — the NAME rung, **corroboration only, never
  alone**.
All are surface constraints (never a payload value — invariant #1 intact) and
serialize deterministically, so the signature folds into `content_hash`
(`graph.py:203`) — a one-way contract, landed once with the §12 Phase-3
`surface_id` change, not incrementally.

### 13.2 The trust ladder (concrete, resolves §9 and §12)
```
tier =
  DECLARED          if an x-gecko / customer hint maps field↔param   → high, basis="declared:<ent>"
  EXTRACTED-entity  elif value_domain_score ≥ HIGH_BAR              → high, basis="sig:<signals>"
                      HIGH_BAR = ≥1 of {pattern-eq, enum-overlap≥τ, discriminating-fmt-eq}
                      AND (name-entity-eq OR resource-eq)            # value domain + a locator
  INFERRED-name     elif name-entity equal but NO value-domain signal → LOW / quarantined
  (no edge)         otherwise
```
**The rule the foundation guarantees: bare name-match across APIs is NEVER a
cross-API plan's basis.** A pair whose only agreement is the name is emitted
`INFERRED/low` — visible/auditable via `basis`, excluded from `plan()`
(`feeds_into(high_only=True)`) — upgraded only if a value-domain signal or a
DECLARED hint corroborates it. The ladder is per-scope via `surface_id`: the same
name-entity equality may carry `high` *intra*-API (naming discipline is a fair prior
within one team) but is demoted *cross*-API. One predicate:
`edge.src.surface_id == edge.dst.surface_id`.

### 13.3 The evidence gate for embeddings (mirrors §7 / §10-11)
The semantic/LLM tier stays OUT until a two-API probe (§13.4) measures that the
deterministic signature + DECLARED leave *real entity-synonym gaps* on the table
(same entity, value domains described inconsistently across specs, names diverge).
Keep-out conditions (any one → no embeddings): all known-true cross-links found
deterministically with zero high false cross-links (the v3-337 analogue); or the
residue is generic-id over-linking (fix = stricter deterministic bar, not
embeddings). Even when admitted, embeddings never mint a `high` edge — they propose
`INFERRED/low` candidates over descriptions for a human/provider to confirm into a
DECLARED hint. Semantic similarity is a *suggestion for annotation*, not a plan
basis — determinism + anti-poisoning preserved.

### 13.4 Falsifiable two-API probe (offline, $0, no model — build gate)
Mirror `scripts/surface_graph_probe.py`, stricter bar than §7 (cost of a wrong
cross-API plan = the agent calling the wrong **system**):
- **Pair:** Stripe + a second payments/FX spec sharing `currency` (ISO-4217 `enum`),
  with abundant confusable generics (`id`, `status`, `created`, `amount`) to stress
  false-linking. Fully reproducible from public specs, no keys, no network. (Alt: two
  Solana specs sharing a base58 token-mint `pattern`.)
- **Protocol:** build one graph per API (never merged — compose at query time, §12),
  compute cross-API candidates via §13.1, tier via §13.2.
- **Gate:** the known-true link (`currency`) found at `high` via EXTRACTED-signature
  **without** a DECLARED hint, AND **zero** high-confidence false cross-links (a bare
  shared `id` reaching `high` fails the gate). If the deterministic tier can't reach
  zero false-high, cross-API plans ship **DECLARED-only** until it can.

### 13.5 Honest residue (the product truth)
The deterministic tier fires only when specs *declare* `pattern`/`enum`/
discriminating `format` on their ids. Many painful long-tail APIs — the ICP — ship
**bare `type: string` ids with no constraints**; for those no deterministic signal
exists, name-match is correctly quarantined, and the **only** high-confidence
cross-API basis is a `DECLARED` hint. That is the honest floor, and it is why §12
ranks DECLARED highest and why the provider/customer annotation motion (§8.5) is
**load-bearing, not optional** — the hardest cross-API joins, on exactly the messiest
APIs, require a provider to declare the mapping. The comprehension frontier and the
provider-WTP motion are the same insight from two directions. If the two-API probe
shows most real cross-links need DECLARED, that is a **product finding, surfaced —
not an engine failure to paper over.**

One genuine capture gap remains before cross-API mutate chains work: request-body
fields are never nodes (`graph.py:366-393` walks only params + response leaves), so
"create X referencing Y's id" is invisible (already flagged §12-3b).

## 14. The authored-enrichment loop (founder, 2026-07-19) — Gecko on both sides

Gecko is not only the *reader* of OpenAPI surfaces — it helps **author** them
(`gecko from-docs`, provider onboarding). That closes a loop the cross-API
foundation (§13) can lean on:

1. **Author/enrich** — when we help build an `openapi.json`, we seed the
   value-domain signals §13.1 needs (`pattern`, `enum`, `format`, examples) and
   the `x-gecko` entity hints directly into the spec. The easier a surface is to
   comprehend, the easier the entity-join lands — so we *make* surfaces easy to
   comprehend rather than hoping they arrive that way.
2. **Comprehend → join** — richer spec ⇒ the deterministic tier fires instead of
   falling to the DECLARED-only floor (§13.5).
3. **Save the relationship** — once an entity equivalence is established
   (derived at high confidence, or human/provider-confirmed), it is persisted as
   a **DECLARED edge** in the workspace graph and never re-derived. Confirmation
   upgrades INFERRED → DECLARED, permanently, with its audit trail (the `explain`
   provenance §5/Task-3 carries end-to-end is what makes that upgrade reviewable).

**The guardrail (do not drift into the retired corpus):** "saved" means the
confirmed **relationship** — surface-level metadata, provenance-tagged,
per-workspace. NEVER observed traffic, call outcomes, or payload values (the
retired corpus: ≈0 lift, and it would break invariant #1). Save the relationship,
not the payload; per-workspace, never a global catalog.

This is the compounding edge of the *allowed* kind, and it is the Gorilla-LLM
discipline extended: the lift is in the **surface**, so we invest where the
surface is made — and every confirmed join makes the next comprehension cheaper.
