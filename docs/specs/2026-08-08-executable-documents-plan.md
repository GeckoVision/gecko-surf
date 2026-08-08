# Executable documents — implementation plan

**2026-08-08.** The milestone: a derived Arazzo document that a runtime can actually
execute, because every value it binds resolves to the place that value really lives.

**Goal.** `gecko workflows <spec>` emits documents whose every runtime expression
resolves against the response schema it came from — verified offline, $0, before any
runtime sees one.

**Not the goal.** Making more documents executable. Two of the currently-"executable"
Pegana documents are `DELETE` and `PATCH` bound to element 0 of an array; making those
resolve would make them *destroy an arbitrary object the caller never named*. Executable
is only a milestone when correct is a precondition.

---

## The real defect

Not a formatting bug. Three layers each lose the same information:

| layer | what it drops |
|---|---|
| `ingest` | flattens nested response properties; the container path is not retained |
| `graph` | `Node.detail` carries the *parent object* for a field — not the full path, and nothing about arity |
| `arazzo._outputs` | emits `$response.body#/{field}`, assuming the field sits at the body root |

And the silent one, `chain_eval._find_field`: it DFSes into lists and returns the first
match **without recording that it descended through a list**. A join key inside a
collection binds element 0 and nothing says so. That is the same zero-value class as the
six "a thing that could not answer, answering anyway" bugs — here the missing
representation is *arity*.

Two reproductions, both on committed fixtures:

- **Pegana** — `list_webhooks` returns a top-level array; the join key is at `#/0/id`.
  We emit `#/id`, which resolves to nothing.
- **Birdeye** — the join key is at `data.items[].address`; the `data` wrapper is dropped
  at ingest and the field node carries `detail='items'`.

---

## REVISED after four reviews — the original plan was wrong

Four lanes reviewed this before code. Three of my claims did not survive, and the
correction makes the work **smaller and ungated**. The original text is superseded from
here down; what it got right was that the defect is real and that it starts at the graph.

### What I had wrong

1. **`_find_field` is not shared.** It is module-private with exactly **one** external
   caller (`chain_eval.py:229`). I called it shared and used that to justify a breaking
   change.
2. **"Prefer refusal on a collection" would retract a published number.**
   `getApiFixturesSnapshot` returns a top-level `type: array`
   (`tests/fixtures/txline_openapi.yaml:254`), so the flagship TxLINE chain reaches its
   join key *through* a list. Refusing there fails
   `test_txline_chain1_fixtures_to_odds_is_first_plan_correct` and retracts row 1 of
   `docs/benchmarks.md`. `chain_eval` is a **measurement harness, not a gate** — making a
   measurement refuse destroys the measurement.
3. **`ExplainEntry` is the wrong seam.** It is a per-plan projection with two
   construction sites, and `compose.py:173` builds one from `(op, field_name)` with no
   schema access — so every cross-surface entry would carry an empty pointer forever, and
   "empty pointer ⇒ refuse" would silently kill every cross-API document. The precedent
   already points the other way: `arazzo._locations(graphs)` reads a parameter's `in` off
   the graph's nodes rather than off `ExplainEntry`.

### The reframe that collapses the whole problem

Arazzo 1.0 has no `for-each` and no "which one" expression. So for a join key inside a
collection **there is no correct pointer to emit.** Emitting `/0/` does not fix the
element-0 defect — it *promotes* it from the control plane, where it affects a score, into
a published document a runtime will act on, stamped "verified resolvable".

The honest emission for `arity == "many"` is a **refusal**, exactly like the existing
`unresolved-parameter-location`.

That has a consequence worth the whole review: **it removes the security gate.** Both
Pegana `DELETE`/`PATCH` documents are element-0 bindings, so they stay refused on *arity*
grounds without anyone ruling on non-idempotent verbs. Pegana's executable count goes
2 → 0, which is the correct number and a better artifact than two documents that delete a
webhook nobody named.

### Revised, and this is the plan

| | work | lane | gate |
|---|---|---|---|
| **R1** | `source_pointer` + `arity` on the **field `Node`** (schema-space: container path, no index). `Arity` declared in `graph.py` beside `NodeKind`/`EdgeKind` — **not** in `provenance.py`; it is structural, not a trust ladder, and needs no anti-poisoning review | `graph-engineer` | none |
| **R2** | `chain_eval._find_field` **records the path it took** and returns arity. Nothing refuses. `ThreadOutcome` gains two fields. TxLINE stays green and now carries `arity="many"` as an honest annotation | `software-engineer` | none |
| **R3** | `arazzo._pointers(graphs)` mirroring `_locations(graphs)`; new `RefusalKind` `unresolved-output-arity`. `arity == "many"` ⇒ refuse, never `/0/` | `graph-engineer` | none |
| **R4** | `derive_candidates` reads `feeds_into(high_only=True)` — it currently ignores `edge.confidence` entirely, a live control bypass (34 of 36 Birdeye candidates rest on edges the planner quarantines) | `software-engineer` | **security-approved** |
| **R5** | write targets excluded via `enforce.is_write_method`, structurally — not a score penalty, which is measurably re-rankable | `software-engineer` | **security-approved** |
| **R6** | `key_is_dangerous` applied in `_response_leaves` as it already is in `_request_body_params` — provider-controlled response keys currently ride unguarded into emitted documents and the content hash | `defi-security-engineer` | is the gate |

**Do not do:** the `ingest.py` edit (the container path is already available inside
`_response_leaves`' own walk, merely discarded) · repurpose `Node.detail` (`correlate.py:422`
reads it as the parent entity for the bare-`id` join rule) · a version bump for the hash
change (`surface_rev` hashes the spec and is unaffected; `content_hash()` is referenced
only in three tests).

### Known traps, each already cost someone

- **`_field_id` collides.** `parent = title or parent` is the container *label*, so
  `/data/items/0/x` and `/meta/items/0/x` produce the same node id. Adding a pointer to a
  colliding id makes it *stably* wrong, which is worse than unstably wrong.
- **`unknown` degrades at the read boundary.** Every closed vocabulary here that held was
  enforced at a write gate, not a type. Missing arity defaults to `unknown`, never `one`;
  unrecognised raises; test the rehydrate, not just the build.
- **Numeric segments prove data-plane origin.** A stored pointer must be index-free; the
  serializer introduces position at render time or refuses.
- **`Confidence` is already declared three times** with different members
  (`graph.py:46`, `correlate.py:69`, `docs_reader/models.py:18`). Do not add a fourth type
  beside it without noting that.

### What this is NOT

Not executability. **R1–R3 ship representation only** — the graph learns where a value
lives and how many there are, and the serializer refuses what it cannot honestly express.
No document becomes newly executable. That is deliberate: "documents a runtime can
execute" walks toward orchestrator, and the measured value here is the refusal.

Sequenced: R1 → R2 → R3 sequentially (overlapping files), R4/R5 after
`feat/ranked-workflow-derivation` merges, R6 independently and first if anyone is free.

### Still blocked on a person, not a lane

**W5 — has any design partner been shown the output?** Zero evidence anyone wanted derived
workflows. R1–R3 are worth doing regardless: arity-unrepresented is the **seventh**
instance of this repo's named root cause — *the type has no representation for "not
evaluated", so that case falls into the zero value* — and it sits under a published
benchmark. But no further workflow engineering until a partner reacts.

---

## Original plan (superseded — kept for the reasoning, not the sequencing)

## Global constraints

Every task inherits these.

- **Harness before fix.** Each task's first step is a failing test resolving a pointer
  against a *committed spec's response schema*. No task begins with an edit.
- **Control plane.** Pointers and arity are structure, never values. No response payload
  is stored or emitted at any point.
- **Never widen a provenance tier to make a document executable.** If a pointer cannot
  be established, the honest output is a refusal, not a guess.
- **`arity` is a closed vocabulary**, declared once and imported everywhere:
  `one` · `many` · `unknown`. `unknown` is a real member and must not degrade to `one`.
- **Determinism.** Same spec in, same pointers out.

---

## M1 — the graph carries where a field lives

**Files:** `gecko/graph.py` (`ExplainEntry`, field-node construction), `gecko/ingest.py`
(`_response_leaves`), `tests/test_graph_pointer.py` *(new)*

**Produces:** `ExplainEntry.source_pointer: str` (an RFC 6901 JSON Pointer relative to
the response body) and `ExplainEntry.arity: Arity`.

1. Failing test: for `list_webhooks` on the committed Pegana spec, the entry for `id`
   has `source_pointer == "/0/id"` and `arity == "many"`. Run it; it fails on a missing
   attribute.
2. Failing test: for Birdeye's `get-defi-v2-tokens-new_listing`, `address` has pointer
   `/data/items/0/address` and `arity == "many"`.
3. Failing test: a field genuinely at the body root has `arity == "one"` and a pointer
   with no numeric segment.
4. Retain the container path through `_response_leaves` — it already walks the schema;
   the path is discarded rather than absent.
5. Add `arity` to the closed type module; never redeclare it locally.
6. Run all three. Commit.

**Done:** the graph can answer "where does this value live, and is there one of them"
for every join key in both fixtures.

---

## M2 — the serializer reads it instead of assuming

**Files:** `gecko/arazzo.py` (`_outputs`, `_expression`), `tests/test_arazzo.py`

1. Failing test: the emitted output expression for the Pegana chain is
   `$response.body#/0/id`, not `$response.body#/id`.
2. Failing test: when `source_pointer` is empty — the field's location could not be
   established — the step is **refused**, not emitted with a root-guess. This is the
   load-bearing one: today's fallback silently produces a plausible wrong pointer.
3. Replace the f-string with the pointer from the entry.
4. Re-run the whole `test_arazzo.py` suite plus the official-schema conformance check;
   a changed expression must not break Arazzo validity.
5. Commit.

**Done:** every emitted expression comes from the graph, and an unestablished location
produces a refusal rather than a guess.

---

## M3 — arity stops being assumed

**Files:** `gecko/chain_eval.py` (`_find_field`), `tests/test_chain_eval.py`

1. Failing test: `_find_field` over a list-shaped response reports that it descended
   through a collection — it must not return a bare value that looks singular.
2. Change the return to carry the path taken, or refuse on a collection when the caller
   asked for a scalar. **Prefer refusal**: "there are many of these and you did not say
   which" is the honest answer, and the caller can then be explicit.
3. Re-run every suite that touches chain evaluation — this function is shared, and the
   change is deliberately breaking for callers that were silently taking element 0.
4. Commit.

**Done:** no code path in the repo can bind element 0 of a collection without saying so.

---

## M4 — the proof

**Files:** `tests/test_workflows_executable.py` *(new)*

1. For every document `gecko workflows` emits on both committed fixtures, assert each
   runtime expression **resolves against that operation's response schema** — walk the
   pointer through the schema, offline, no network, no synthesized payload.
2. Assert the refusal documents still fail Arazzo validation (the existing negative
   control must survive).
3. Assert the count of executable documents is **stated**, not implied — the CLI already
   reports refusals; it must now also report how many were verified resolvable.

**Done:** we can say "these documents are executable" and point at the check that proves
it, on committed fixtures, for $0.

---

## Gated — must not start before the security verdict

Routed to `defi-security-engineer`; nothing below is written until it returns.

- **G1 — destructive targets.** 6 of the top 10 Pegana candidates are state-changing
  (`DELETE`, `PATCH`, 4× `POST`). `derive_candidates` scores `DELETE` exactly like `GET`.
  Whether a *derived* document may target a non-idempotent verb at all is a verdict, not
  a preference.
- **G2 — the intra-surface join.** Birdeye has seven components named `address` across
  four value domains, so auto-confirming by name would assert a trader's wallet is a
  token mint. The strict xfail in `tests/test_workflows.py` holds until the rule is
  stated.

**M1–M4 are independent of both** — a pointer must be right whether or not the target is
ever allowed to be a `DELETE`. That is why this plan starts there.

---

## Next week — milestones and deliveries

| | milestone | delivery | gate |
|---|---|---|---|
| **1** | **Pointers are real** (M1+M2) | every emitted expression comes from the graph; unestablished → refusal | none |
| **2** | **Arity is represented** (M3) | nothing in the repo binds element 0 silently; the `_find_field` item closes with two reproductions | none |
| **3** | **Executable, proven** (M4) | a check that resolves every pointer against the schema, on both fixtures, offline | none |
| **4** | **The rules are written** (G1+G2) | the security verdict lands; the xfail is removed by the PR that states the rule | **security** |
| **5** | **The provider artifact** | `gecko workflows` output a design partner actually opens — index + resolvable documents + the floor | 1–4 |

**Sequencing that is not negotiable:** M1 → M2 → M3 → M4. G1/G2 run in parallel and
merge before milestone 5. Nothing outward-facing ships before M4, because until then
"executable" is not a claim we can make.

**What we may say while this is in flight** — unchanged from the routed queue: Gecko
derives and ranks which workflows an API can offer, deterministically, and **refuses
rather than emitting a plausible-looking plan**. Not "runnable", not "ready to run",
until milestone 3 closes.

---

## The JTBD this serves

A provider ships a reference, an `openapi.json`, and often an MCP server. All three
describe calls **one at a time**. None says which call produces the value another one
requires — so every agent that touches the API reconstructs that by guessing, and the
guesses that type-check are the expensive ones.

The job is: *tell an agent which of these calls feed which, where each value actually
lives, and which ones I cannot establish at all.* Milestones 1–3 are that sentence
becoming true. Milestone 5 is it becoming something a provider can open.
