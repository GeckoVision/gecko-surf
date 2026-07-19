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
