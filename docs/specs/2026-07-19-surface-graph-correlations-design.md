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
| `field --feeds--> param` | **INFERRED** | recorded basis: exact-name + type match; name-variant match (`fixtureId`/`FixtureId`); never fuzzy-semantic in v1 |
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

Explicitly *not* in v1: cross-API edges, corpus-weighted edges, vector
similarity for `feeds` inference, any UI.

## 9. Open questions (decide at probe time)

- Should `x-gecko` sequence hints (provider-declared chains) be a third
  provenance class `DECLARED` — trusted more than INFERRED, less than
  EXTRACTED? (Leans yes; providers annotating chains is the for-providers
  story meeting the graph.)
- Plan caching: plans are derivable at ingest time for common intents —
  precompute per surface, or walk per query? (Measure at probe scale first;
  premature either way.)
