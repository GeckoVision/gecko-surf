# The ingestion graph surface — one view model, four projectors

**Status:** design, written 2026-08-16 from an audit of `orquestra@c9dd085` and its live
catalog the same day. Every number below was measured, not estimated.
**Scope:** architecture and work split. Not an implementation plan.
**Companion:** `2026-08-16-ci-for-agents.md` — this is the machine that spec asked for.

---

## 1. Why now

We shipped a scorecard by hand and it found two defects in an hour: a compute number
reported 347× under, and an instruction uncallable as published. The fix to the first was
merged upstream within the hour. That is the whole argument for an outside measurement,
and it is also the whole limitation: **everything in that run was hand-written.**

Meanwhile the surface it measured moved. Orquestra shipped a Flow Engine — FDL documents
compiled to plans and executed deterministically — and published 24 flows. The design
targets 4,000. The gap between what that engine can express and what an autonomous agent
needs to act is now measurable, and it is where this belongs.

**The thesis in one line: the graph is the product, and every format is a rendering of it.**
Arazzo, FDL, an MCP tool list, a scorecard — four projections of one view model. Build the
view model once, honestly, with provenance on every claim, and the formats are cheap.

## 2. Two tracks, deliberately separate

They have different owners, different timescales, and different failure modes. Conflating
them is how a contribution turns into a competitor.

| | Track A — contribution | Track B — the surface |
|---|---|---|
| what | fixes and additive proposals to Orquestra | our ingestion graph + cloud surface |
| owner | Berkay merges; we supply patch + reproduction | us |
| risk if wrong | a rejected PR | a product nobody points at |
| dependency | none — each item stands alone | Track A's §5 fields make the FDL projector lossless |

Track A is not a favour and it is not a wedge. Every item is a defect we hit while
measuring, with a reproduction. That is the only honest basis for sending someone a patch.

## 3. Track A — what is broken

Each item is `file:line`, an observed symptom, and who it hurts.

**A1 — natural-language search returns nothing.** `services/search.ts:77`, `buildFtsQuery`
joins tokens with `.join(' AND ')` and does not drop stopwords. `"buy a coffee at a bar"`
requires `a`, `at`, `coffee`, `bar` to all match. Measured: **5 of 5 natural-language
intents return "No programs found"**, while every single keyword works (`buy` → Let Me Buy,
`mining` → Ore, `subscription` → Subscriptions). The FTS5 index and BM25 weights beneath
are sound; only the operator is wrong. Weighted OR plus a minimum-match threshold.

**A2 — the publish path checks less than the authoring loop.** `routes/flows.ts:125`
publishes on `compile()` alone, and `publish: true` is the default. `lintReferences` and
`lintDeliverable` — the checks that catch a dangling `$ref` and a flow that builds no
transaction — live in `agents/flow-lint.ts` and run only inside FlowAuthorAgent. Anything
published by hand or by another tool skips both. Two imports.

**A3 — declared outputs are never checked against the graph.** `compiler.ts:238` states it:
*"Output-wiring validation is deliberately out of scope for this MVP pass."* Observed
consequence: the output key is `transactions` on 22 published flows and `tx` on 2. A
consumer that reads the field name breaks on the two.

**A4 — one instruction is uncallable as published.** `delete_product` on `let_me_buy` fails
to build: `Missing required accounts: system_program`. Found on the scorecard's first run;
invisible from the IDL, the docs, or the code.

## 4. Track A — what is weak but not broken

**A5 — node output shapes are prose.** `flow-engine/schema-docs.ts` describes each node's
output as an English string; `agents/flow-lint.ts:25` scrapes field names out of it with a
hand-rolled brace parser and **skips any node whose shape did not parse**. It exists because
a production run died on `Cannot read properties of undefined (reading '_bn')`. A real
output schema per node deletes the failure class and the parser together.

**A6 — metadata hygiene at publish time.** Measured across all 24 flows: `sideEffects: null`
on 24/24 though ATA creation routinely spends rent; 7 required inputs with no description
(including `inputMint`, `outputMint`, `vault0Mint`); 16 intent values over 24 flows mixing
verbs with raw instruction names, so filtering `buy` cannot reach a flow filed under
`make_purchase`; `orca` and `orca_whirlpool` as two protocols. All enforceable where the
flow is written.

**A7 — the lifecycle is decorative.** `services/flow-publisher.ts:73` moves `draft →
published` in one UPDATE. `validated`, `simulated`, `canary`, `stale` appear in migration
`024_flows.sql`'s CHECK constraint and nowhere else in the worker. `flow_health` was
designed and never created, so goal G7 — *IDL change → re-verified or quarantined within
one hour* — has no implementation.

**A8 — missing node types.** 15 registered. No `map.over@1` (bounded fan-out) and no
`flow.call@1` (composition, the design's declared unit of reuse). The arity wall, again:
Arazzo has no for-each either, and its own answer is index-0 binding. Two independent
workflow languages hitting the same limit says the gap is in the category.

## 5. Track A — the three additive fields

These need data we produce, which is why they are proposals rather than patches. Each is
justified by a defect in the live catalog.

| field | what it carries | the defect it closes |
|---|---|---|
| `provenance` on a node | where this account/seed claim came from (extracted · recovered · cross-surface · flagged) | a wrong-but-well-formed PDA derives a valid address and nothing downstream can catch it |
| `reason` on an input | why this is an input: caller-choice · safety-required · unresolved | `subscriptions-subscribe` requires `expectedCreatedAt`, which is **correct** (the program checks it so a merchant cannot change terms) **and** unsupplyable. FDL cannot say which |
| `prerequisite` relation | read this before you can call that | the same case: the fix is not a resolver, it is showing the agent the plan's real terms first |

All three are additive and ignorable by an existing compiler.

## 6. Track B — the graph

### 6.1 The library surface

A builder imports it the way they'd import any ingestion library — dotted namespaces, one
purpose each:

```python
from gecko.ingest import from_idl, from_openapi, from_docs   # sources → a surface
from gecko.graph  import build, Node, Edge                    # surface → the view model
from gecko.score  import measure                              # graph → measured answers
from gecko.project import to_fdl, to_agent_surface, to_scorecard, to_sandbox
```

`gecko.graph` and `gecko.program_graph` already exist and carry most of this. What is new
is `gecko.ingest` as a front door, `gecko.score` as a first-class stage, and `gecko.project`
as the rendering layer.

### 6.2 One vocabulary, two builders — and what must not be merged

We have two graph types today: `gecko.graph` (`Node`/`Edge`, with value-domain `sig`,
`arity`, `source_pointer`, `verify`) for API surfaces, and `gecko.program_graph`
(`ProgramGraph`/`InstructionGraph`/`AccountRef`/`SeedBinding`, with `resolvable`, `cycle`,
`derive_from`, `origins`) for programs. `gecko.provenance` is already the shared spine —
every ladder in the system is declared there once.

**Decision: unify the emitted document, not the dataclasses.** `ProgramGraph`'s own
docstring records why — `origins` deliberately does not live on `PdaNode` because it would
change the frozen node's equality, which the merge rule and the config round-trip both
depend on. Merging the types to make a diagram tidy would break invariants that 2,400 tests
rest on, to no one's benefit. The convergence point is the serialized surface document.

### 6.3 What a node carries that a spec format does not

| carried | why it cannot be dropped |
|---|---|
| provenance per account **and per seed** | a claim with no origin cannot be disputed, and a score nobody can dispute is marketing |
| `resolvable=False` + `cycle` | the honest gap. Dropping an unresolvable account fails at sign time, far from here |
| value-domain signature | the only way to machine-check that a mint input is a mint. FDL's input types are `pubkey`/`u64`/…/`bps` — there is no `mint` |
| arity | whether a path passes through a collection — the for-each question both Arazzo and FDL answer by pretending it does not arise |
| prerequisite | the cross-call relation that turns an uncallable flow into a callable one |
| the measured verdict | BUILD · SIMULATE · HONEST · REACHABLE · REFUSES, each with its origin |

### 6.4 The four projectors

1. **`to_fdl`** — runs on Berkay's engine. Composition, not adoption.
2. **`to_agent_surface`** — the tool defs and instructions an agent mounts.
3. **`to_scorecard`** — the honesty layer the provider shows its developers.
4. **`to_sandbox`** — `try_*` against a $0 fork (Track B phase 2; the buyer path).

Because all four render one object, the FDL we emit and the score we publish cannot
disagree. That property is the reason for the architecture.

## 7. Why a graph and not a spec — "buy a water", end to end

The founder's test case, and the one we have ground truth for. An agent is told *buy a
water*. What has to be true for it to act without a human:

| step | what it needs | FDL | Arazzo | our graph |
|---|---|---|---|---|
| intent → instruction | paraphrase retrieval | — | — | measured (and lexical scores 0.00 on paraphrase, so this is honest about failing) |
| which store? | refuse to guess among candidates | — | — | shipped |
| "a coffee" → which coffee? | name the resolver, refuse to be it | — | — | shipped |
| receipts PDA | seed recovered from source, not IDL | seeds, no origin | — | seed + provenance tier |
| mint → ATA | value domain says this is a mint | `pubkey` | — | `solana-token-mint` |
| derivation order | dependency-first, cycles named | strata | — | order + `cycle` |
| what it moves | balance deltas | **none** | — | sol/token deltas, caps |
| the window | ~40–48 s, blockhash-bound | — | — | expiry budget, shipped |
| verdict before signing | message-hash binding | — | — | shipped |

FDL carries roughly half of one column. That is not a criticism of FDL — it is a *flow*
language, and flows are the half it carries well. It is the argument for why the graph is
the artifact and FDL is an export.

Confirmed by grep across the whole worker: **no `preBalances`, no `postBalances`, no
`preTokenBalances`, no `accounts` config on `simulateTransaction`.** The risk report a
caller is meant to approve on (`services/tx-builder.ts:426-474`) is computed from account
mutability flags and a regex on the instruction name. It says which accounts are writable;
it never says what moves, or to whom. A purchase that pays the buyer back scores identically
to a real one.

## 8. The cloud surface

`surfcall-cloud` is one README and one commit today — greenfield. A provider points it at
an IDL, an OpenAPI URL, or a docs page and gets a page they hand their developers:

- the graph, browsable — what calls what, what derives from what, in what order;
- the score, per instruction, each answer measured and each claim carrying its origin;
- runnable FDL for anything already on Orquestra, so a builder copies and it executes;
- `try_*` against a $0 fork, so a first call costs nothing and lands nothing;
- drift watch — re-run on change, and say *which* of the five answers moved.

**We never list.** We render a surface its owner already owns — overlay keyed to Orquestra's
`projectId` where the provider is on his catalog, standalone where they are not. The moment
we publish our own catalog we become the marketplace this repo exists not to be.

## 9. Round-trip, the gate on everything

Phase 1 is not "emit FDL." It is one program we know cold, proving:

```
our graph → to_fdl → POST /flows compiles → estimate → PDA matches chain → CU matches ours
```

The last two are the point. A seed mapping that is wrong but well-formed produces a valid
address and a clean compile, and **that is the one failure Orquestra's stack cannot catch**
(`resolve.pda@1` receives fully-resolved raw seed values with no IDL in scope —
`resolvers/pda.ts:6`). So the round-trip asserts the derived account against chain state,
never merely that it compiled.

Known risk: our `SeedBinding` kinds must map onto his (`pubkey`/`string`/`bytes`/`u8…u128`/
`i8…i128`). Helper-seeded roots are where we expect the mismatch, and they are exactly the
seeds only we recover.

## 10. What we have, and what is missing

| piece | status |
|---|---|
| program graph — instruction↔PDA, derivation order, cycles | **have** (`program_graph.py`) |
| API surface graph — value domains, arity, correlation | **have** (`graph.py`, `correlate.py`) |
| provenance ladders, single source of truth | **have** (`provenance.py`) |
| seed recovery from source (the IDL cannot carry it) | **have** (`pda_extract.py`) |
| simulate → Receipt, deltas, caps | **have** |
| scorecard, six cases, one program | **have, hand-written** (`scripts/catalog_ci.py`) |
| `gecko.ingest` front door | **missing** |
| `gecko.score` as a stage | **missing** — measurement exists, not as a stage over a graph |
| `to_fdl` projector | **missing** |
| declarative shapes instead of hardcoded probes | **missing** — the scorecard is six literal cases |
| hosted surface | **missing** — empty scaffold |
| catalog-scale ingestion | **missing** — one surface at a time |

Honest summary, unchanged from the companion spec: **we have the instruments and none of
the factory.** What is new is that we now know the shape of the factory.

## 11. Open questions

1. **Does the FDL projector emit flows we publish, or flows the provider publishes?** The
   first makes us an author in his catalog with an ingest key. The second is slower and
   keeps the ownership line clean. Leaning second; not decided.
2. **What is the honest floor for a generated probe?** A hand-written golden set carries
   human judgement about what *should* refuse. A generated one may only test what the schema
   already says — the weaker half. Unresolved since the companion spec.
3. **Does `REACHABLE` belong in a provider's score?** It measures the agent's retrieval as
   much as the surface. A provider may fairly say it is not their defect. It is also the
   axis where we have the sharpest evidence.
4. **Who pays, and for what?** Repo canon: developers never pay, revenue is provider-side
   and flat. A build kit fits that. A per-call verification meter does not, and it is the
   only unit that grows on its own. Decided, not argued.
5. **Do we adopt SHACL, or a smaller shape language of our own?** SHACL is the standard for
   declarative constraints over a graph and it makes a score something a provider can read
   and dispute. It also drags in RDF vocabulary that nothing else here speaks.

## 12. First step

Not the factory, and not the cloud. **One program, round-tripped.** Take `let_me_buy`, build
the graph we already build, project it to FDL, publish it through his compiler, and assert
the derived PDAs and the compute units against the chain. If that holds, the view model is
real and every projector after it is rendering. If it does not, we learn it on six
instructions instead of four thousand.

Track A ships in parallel and independently — A1 and A2 are each a few lines, both have
reproductions, and neither waits on anything here.
