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
