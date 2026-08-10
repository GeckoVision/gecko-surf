# Graph assessment — 2026-08-06

Author: `graph-engineer`. Scope: `gecko/graph.py`, `gecko/correlate.py`,
`gecko/program_graph.py`, `gecko/provenance.py`, `gecko/find_start.py`,
`gecko/surface.py`, plus `compose.py`, `retrieval_eval.py`, `chain_eval.py` and their
tests/fixtures. **Evaluation only — no production code was changed.**

Every number below was produced by running the shipped code against committed fixtures
(`tests/fixtures/golden/privy_openapi.json`, `tests/fixtures/pegana_openapi.json`,
`tests/fixtures/txline_openapi.yaml`) and the packaged Orquestra golden set. Probes were
throwaway scripts; none are committed.

> **Correction pass — 2026-08-07, on `origin/main` @ `c0a96d9`.** Several figures in the
> first draft did not reproduce. Corrected in place, with the wrong value kept visible so
> nobody re-derives from it:
>
> * the **privy graph-shape row** (§1) — the original 8,171 / 11,800 / 3,704 was wrong;
> * the **retrieval baseline** (§4) — now recall@1 0.74 / recall@3 0.89 / MRR 0.81;
> * **W2 / §2(b) / R5** — the "18 of 39 plans" figure was wrong *and* rested on an
>   unstated plan enumeration. Re-measured; the effect is much smaller than claimed and
>   **R5 is now a recommendation NOT to make the change**. See §2(b).
>
> Not re-measured in this pass: the `correlate` tier-2 counts in §2(a)/W1 and the derived-
> domain counts in R2. Those describe the code **before** PR #313 (R1/R8) and #323 (R2)
> landed and should be read as historical. R4 (#320), R6 (#321) and R3 (#324) have also
> landed since the draft.

---

## Executive summary

Three things are genuinely good and should not be touched: the **API-surface provenance
model** (`graph.py`), the **cross-API DECLARED-only gate** (`correlate.py` +
`compose.py`), and the **`find_start` named-or-corroborated floor**. I tried to break all
three and could not.

Three things are wrong in ways that will cost someone money or credibility:

1. **`correlate`'s tier 2 fires with no name match at all**, and on real specs 95% of
   everything it reports as `plan_eligible` is a non-join-key: timestamps, money
   amounts, mode enums. Privy: 479 of 498 plan-eligible links. Pegana: 59 of 68.
   This is a textbook **join-key-quality** failure and the single highest-value fix.
2. ~~**The planner has no notion of side effects.** 18 of the 39 plans `graph.plan()`
   produces on the Privy fixture…~~ **CORRECTED 2026-08-07.** The planner still has no
   notion of side effects, and "call `createUser` so you can then call
   `getUserByCustomAuthId`" is a real plan the shipped code emits. But **18 of 39 was
   wrong**: the real figure is **14 of 25** privy supplier choices (**19 of 69** across
   every committed fixture). More importantly, only **2** of those 14 are decidable by a
   preference term — in the other 12 the mutating op is the *sole* candidate, including
   all three `getUserBy*` cases that motivated the finding. The proposed fix does not fix
   the example that motivated it. See §2(b); the verdict is now **do not change
   `_resolve`**.
3. **The program graph carries no provenance at all**, and `find_start` reconstructs it
   by hand at read time. `program_graph.py` — named in the charter as a
   provenance-carrying module — has zero provenance fields. Consequence: an account
   with no recipe silently ships as `extracted`, and `find_start` and
   `jupiter_landing.py` currently disagree about the same accounts.

One area is honestly sound and I want to say so plainly: nothing I could construct made
an INFERRED fact wear an EXTRACTED costume in the API graph, and nothing made a
cross-API name or signature match reach `plan_eligible`. Invariants #1 and #5 hold.

---

## 1. What kind of graph is this, honestly?

### The API surface graph — a real, typed, content-addressed property graph

`gecko/graph.py:47-48` declares the whole vocabulary:

```
NodeKind = Literal["operation", "param", "field", "resource"]
EdgeKind = Literal["consumes", "produces", "on", "feeds"]
```

Three of the four edge kinds are structural restatements of the spec (`consumes`,
`produces`, `on`, all `EXTRACTED`, `graph.py:748/768/775`). Exactly **one** edge kind is
derived and therefore interesting: `feeds` (field → param), minted in two phases —
`DECLARED` from an entity hint (`graph.py:796-817`) and `INFERRED` from the v3 basis
(`graph.py:819-899`).

It is content-addressed (`serialize()`/`content_hash()`, `graph.py:494-527`), namespaced
per surface (`_ns`, `graph.py:442-447`), and every edge carries `provenance` + `basis` +
`confidence` (`graph.py:106-114`). Planning is a bounded backward walk over `feeds`
(`_resolve`, `graph.py:553-637`) with a real cycle guard (`graph.py:584`) and a depth
budget. This is a genuine dependency graph with lineage on the edges. It is the strongest
artifact in the repo.

Measured shape (**re-measured 2026-08-07 on `origin/main` @ `c0a96d9`** via
`Surface.from_spec(<fixture>).graph`; the first draft's privy row — 8,171 / 11,800 /
3,704 (1,287 / 2,417) — did not reproduce and is withdrawn):

| fixture | ops | nodes | edges | feeds (high / low) |
|---|---|---|---|---|
| privy (golden) | 159 | 6,308 | 8,879 | 2,646 (802 / 1,844) |
| pegana | 41 | 292 | 352 | 90 (32 / 58) |
| pegana_p0 | 43 | 351 | 421 | 102 (33 / 69) |
| txline | 18 | 809 | 946 | 147 (136 / 11) |

(The pegana and txline rows moved slightly too: #321 lit up the bare-`id` REST chain, so
pegana gained 3 high `feeds` edges — the +3 R6 predicted.)

### The program graph — a different model that borrows the word "graph"

`gecko/program_graph.py` has **no `Node`, no `Edge`, no `provenance`, no `basis`, no
`confidence`, and no content hash**. Its types are
`SeedBinding(seed_name, encoding, kind, bound_to)` (`program_graph.py:45-55`),
`AccountRef` (`:57-68`), `InstructionGraph` (`:71-87`), `ProgramGraph` (`:90-104`).

`SeedBinding.kind` looks like provenance and is not: it is `"account" | "argument" |
"unresolved"`, i.e. *which namespace of this instruction the value comes from* — not
*where the fact came from*. Grep confirms the module never imports `gecko.provenance`.

So the `seeds-from` edge is the only edge kind in Gecko that cannot answer "from what
basis?" — which is precisely the charter's "done" test. The information exists:
`orquestra_comprehend.comprehend_project` computes a per-PDA `ProgramProvenanceTier`
(`orquestra_comprehend.py:199-217`) — and then **discards it**, because the packaged
config schema deliberately has no provenance key (`orquestra_comprehend.py:86-92`).
`find_start` re-derives it heuristically at read time from hand-authored
`StartSpec.recovered` maps and overlay presence (`find_start.py:409-456`).

### Are they one model or two? — Two. Do not unify them.

They are two models sharing English vocabulary, and I recommend **against** unifying
them into one `Node`/`Edge` type. The reasons:

- The joins are structurally different. The API graph's join is *name/entity identity
  across independently-authored operations* — inherently uncertain, hence the ladder.
  The program graph's join is *positional binding within one instruction* — a seed named
  `authority` binds to the account slot named `authority`, which is either true or not.
  There is no confidence to express.
- The uncertainty in the program surface lives one level up, in *where the recipe came
  from* (IDL vs source vs overlay vs nothing), which is a **node** attribute, not an edge
  attribute. Forcing it onto an `Edge` type designed for `field → param` buys nothing.
- The planning algorithms are different and both correct for their domain: backward
  best-first search with a cost function (API) vs. Kahn topological sort (program).

**What *should* be unified is the discipline, not the dataclasses**: the program graph
needs per-recipe provenance and an honest cycle/unresolvable flag. That is
recommendation R3/R4 below, and it is a ~40-line change, not a refactor.

---

## 2. Judged against the established discipline

### Where Gecko already matches decades-old good practice

**Provenance on edges, with a closed vocabulary.** This is the W3C PROV / data-lineage
pattern done correctly: `provenance` (class) + `basis` (the specific justification) +
`confidence` (rung), with the vocabulary closed in one module (`provenance.py`). The
`EXTRACTED`-vs-derived split is exactly the "asserted vs. inferred" distinction that
lineage systems get wrong constantly. `tests/test_graph.py:81` locks it. This is better
than most production lineage systems I have read.

**Content addressing for drift.** `serialize()` → `content_hash()` (`graph.py:494-527`)
with node/edge sorting and a canonical JSON encoding is the standard Merkle-ish
technique for "same input → same artifact, drifted input → reviewable diff". Correct,
and `tests/test_graph.py:74` pins it.

**Topological ordering with a documented cycle policy.** `_derivation_order`
(`program_graph.py:224-262`) is Kahn's algorithm with a stable declaration-order tiebreak
— textbook, and the right choice for determinism.

**Refusing rather than truncating.** `plan()` returns `None` when the chain does not
close (`graph.py:618`); `cross_plan` refuses over its step cap rather than truncating
(`compose.py:203-204`). This is the correct failure mode for a derivation graph and it
is rarer in practice than it should be.

**Separating the trusted from the merely-declared.** `graph.declared` (provider ∪
customer) vs `graph.confirmed` (customer only), with `compose.cross_plan` gating on
`confirmed` (`compose.py:145-153`), is a proper two-level trust model. `correlate`'s
`provider-declared-unconfirmed` candidate state (`correlate.py:252-263`) is the same
idea reported honestly.

### Where Gecko is doing something ad hoc that a known technique does better

**(a) Join-key quality is proxied by "shared constraint identity" instead of
cardinality/uniqueness — and that proxy is inverted for enums.**

`_sig_corroborates` (`graph.py:284-302`) treats an equal `enum` hash as corroboration.
But the discipline here is settled: a join key needs **high cardinality and low
selectivity variance**. An `enum` is the *opposite* — a bounded, tiny domain. Two fields
sharing an enum is evidence they are **not** a join key. Ironically the code comment at
`graph.py:51` states the intent ("drops bool/enum links") but an enum with
`type: string` passes `_ID_TYPES` unchanged.

Measured on the committed Privy fixture, `correlate_surfaces(privy, privy)`:

```
total 2,918 | plan_eligible 498 | candidates 2,420 | by_tier {3: 0, 2: 479, 1: 19}
tier-2 signals: pattern-eq 330, enum-eq 149   — all 479 have entity = None
```

Samples of what "plan-eligible" currently means:

```
archiveWallet.chain_type          -> createWallet.chain_type            (enum-eq)
getEarnSummary.total_allocated_converted -> createCustomOrder.amount    (pattern-eq)
```

and on pegana, all 59 of its tier-2 links are the same shape:

```
audit_detail.created_at  -> csv.from   (format-eq: date-time)
list_alerts.detected_at  -> csv.to     (format-eq: date-time)
```

`chain_type` is a mode flag. `total_allocated_converted → amount` is a money quantity
matching a decimal-string pattern. `detected_at → csv.from` is a timestamp meeting a
date range. **None of these is a join key.** 538 of the 566 plan-eligible links across
the two fixtures (95%) are of this class.

The known technique that fixes it is not exotic: **require the join to rest on identity,
and treat the value domain as a corroborator only** — which is exactly what the module
docstring already promises (`correlate.py:20-22`, "a shared discriminating pattern/enum/
format **corroborates a name match**"). The implementation does not do that:
`correlate.py:265-274` returns tier 2 with `ent2 = None` and `plan_eligible = True`. The
docstring and the code disagree, and the code is the wrong one.

Secondary: `date-time` in `_DISCRIMINATING_FMT` (`graph.py:64-66`) is the charter's own
"rarity is not distinctiveness" trap one level down. A timestamp is high-cardinality
(rare values) and names nothing (every API has twenty of them).

**(b) The planner optimises for path length and ignores effect class.**
*(rewritten 2026-08-07 — the original numbers were wrong and the conclusion has flipped)*

`_resolve`'s preference key is `(cost, provenance_rank, src_op_node)`. Dependency-
resolution literature has had "prefer the pure/read producer over the effectful one"
since make. Gecko has no such term. **That much is still true.** The original claim —
"ops with a plan: 39, plans sourcing an input from a POST/PATCH/DELETE op: 18" — did
**not** reproduce, and it never stated its plan enumeration, which turns out to be where
most of the disagreement lived.

**Method (stated, because the denominator is enumeration-dependent).** Two enumerations,
both over the shipped path (`Surface.from_spec` → `graph.plan`, `max_ops=3`), on every
committed OpenAPI fixture:

* **A — nothing satisfiable.** For each op, `plan(g, op, frozenset())`. A *plan* = a
  non-`None` result with >1 step.
* **B — leave-one-out.** For each op and each of its required non-auth inputs, assume the
  agent supplies all the *others*. This is the enumeration that reproduces a count near
  the draft's, and it is the more generous one.

| enumeration | plans (all fixtures) | sourcing from POST/PUT/PATCH/DELETE |
|---|---|---|
| A — nothing satisfiable | 33 | 7 |
| B — leave-one-out | 69 | 19 |
| B, privy alone | 25 | 14 |

So the honest headline is **14 of 25 on privy / 19 of 69 repo-wide**, not 18 of 39.

**The number that actually decides the fix is neither of those.** A preference term can
only change an outcome where the planner had ≥2 surviving candidates that tie on cost and
provenance rank and *disagree* on effect class. Replaying `_resolve`'s own candidate loop:

```
privy: 25 supplier choices with >=1 candidate
       12  sole candidate is mutating   -> a preference term CANNOT help
        2  contested (read-only alternative at equal cost+rank) and the mutation WON
       11  read-only anyway
every other committed fixture: 0 contested
```

The two contested cases, and they are the entire addressable population:

```
POST createCustodialWallet needs provider_user_id: picked POST createAccount
                                                   over GET listWallets (equal cost+rank)
POST createKrakenUser      needs external_id:      picked POST createIntentBoundSigningKey
                                                   over GET listWallets (equal cost+rank)
```

**The motivating anecdote is in the *un*addressable 12.** `getUserByCustomAuthId ←
createUser` has exactly one surviving candidate. So does `getUserByFarcasterId`,
`getUserByTelegramUserId`, both `initiateFiat*` ops, and both `swap*` ops. Reordering
cannot touch them; only *refusing* could, and refusing drops the plan.

**Why the sole-candidate case dominates, structurally.** It is not producer scarcity:
`custom_user_id` has a fan-in of 22 high `feeds` edges. It is the **`cost` term**. Under
`max_ops=3` the sub-resolve budget is 1, so a candidate survives only if it needs no
inputs of its own — and on a POST-heavy API the input-free operations are precisely the
`create*` ones. The mutating preference is a *consequence* of minimising path length, not
of the tie-break. A tie-break cannot reach it.

**And HTTP method is a weak effect-class signal on the very spec that motivated this.**
19 of privy's 105 non-`GET` operations are read-shaped by name (`getUserBy*` ×16,
`listFiatTransactions`, `searchUsers`, `getWalletByAddress`); 5 of the 14 flagged privy
plans source from `POST listFiatTransactions`, which is a read. The mislabel is
directionally safe (it can only prefer a `GET` over a POST-read, never a mutation over a
read) but ~36% of the flagged population is method noise.

"Create a user so you can look the user up" remains a genuine wrong-but-plausible plan.
The correction is that **the ordering preference is not where it comes from**, so
changing the ordering preference is not the fix. See R5.

**(c) The consumer-side entity chain has two unreachable branches, and it costs the
single most common REST chain.**

`graph.py:830-836`:

```python
p_ent = (
    _entity_of(p.name)
    or (n if is_path else None)
    or (rnoun if is_path else None)
)
```

For a path param literally named `id`, `_entity_of("id")` returns `None` (no parent), so
the second branch returns `"id"` — and `(rnoun if is_path else None)` can **never** be
reached, because `_norm(p.name)` is always truthy for a real param. The producer side
computes `_entity_of("id", parent) = "customer"`. `"customer" != "id"`, so no edge. The
`scoped-id:` basis at `graph.py:857` is consequently dead — grep finds it in no test and
no output.

Reproduced directly:

```
GET /customers -> {title: Customer, properties: {id}}
GET /customers/{id}
=> feeds edges: []   plan: None
```

That is the canonical REST list→get chain, missed entirely. It is a **recall** bug, not a
precision bug, so it is lower-severity than (a) and (b) — but it is a systematic blind
spot in the most common API shape there is.

**(d) The DECLARED edge does not record whether a human vouched for it.**

`graph.py:808-816` mints every DECLARED `feeds` edge with `basis=f"declared:{d_ent}"`,
identically whether the entity came from provider-authored `x-gecko` (untrusted spec
text) or from the customer confirm store. The distinction exists at the *graph* level
(`declared` vs `confirmed`, `graph.py:481-490`) and `compose.cross_plan` reads it
correctly — but an `Edge` or an `ExplainEntry` in an intra-API plan cannot tell you which
one it was. A reviewer reading a plan's explain block sees `DECLARED / declared:widget`
and cannot answer "who said so". That fails the charter's fourth "done" question.

Blast radius is bounded — this only affects intra-API plans, which is arguably the
provider's own prerogative — but the provenance is thinner than it reads.

### Infrastructure I am explicitly not recommending

**No graph database.** The largest graph measured is 8,171 nodes / 11,800 edges, built
in well under a second, and it is rebuilt from the spec deterministically rather than
mutated. There is no traversal we cannot do in a list comprehension and no persistence
problem that is ours (that is `data-engineer`'s lane). A graph DB would add an
operational dependency and destroy invariant #6 (pure, no I/O) for zero measured gain.

**No vector store.** `find_start`'s own instrumentation is the argument against it: on
the 32-row golden set the semantic-flip evidence is **2 misrank + 1 vocabulary_gap of 27
wired-gold rows** (corrected 2026-08-07; the draft said 2 vocabulary_gap), against 1
coverage_gap that argues for wiring, not vectors. The evidence gate is doing its job and
it says "not yet".

---

## 3. The real weaknesses

Ordered by how much a wrong answer costs.

### W1 — `correlate` tier 2 mints wrong-but-plausible plan-eligible links at scale

`correlate.py:265-274`. Fires on signature alone, with `ent2 = None`, and returns
`plan_eligible = True`. It also escapes genericity demotion, because `_demote_generic`
only touches tier 1 (`correlate.py:419`). Evidence and magnitude in §2(a).

Mitigating fact, stated for honesty: **these links do not reach `graph.plan()`.** The
executable planner requires a normalized-name match (`graph.py:826-828`) and only ever
uses the signature to append `+sig` to a basis. The damage is confined to the correlation
*report* — `Surface.correlate`, `find_correlations`, and the `plan_eligible: 498` figure
in the summary. That figure is ~96% noise, and it is the number most likely to end up in
a demo or a deck.

Also reachable from a **poisoned spec**: `_domain_signal` (`graph.py:226-242`) derives the
`base58` value domain from a field's *description prose* (`_BASE58_HINTS` includes
`"pubkey"`, `"solana address"`). Verified:

```
ledger_ref  ("the ledger reference (pubkey)")   -> sig string|base58||
batch_tag   ("opaque batch tag, a solana address") -> sig string|base58||
_score -> tier 2, INFERRED, ('format-eq',), medium, plan_eligible = True
```

Two unrelated names become a plan-eligible correlation from attacker-controllable
description text. And the derived domain is written into the *format* slot, so a reviewer
reading `format-eq` cannot tell it came from prose rather than a declared `format:`
keyword. This is not a ladder violation — the rung is still `INFERRED` — but the **basis
is misreported**, which is the same failure one level down. On the real Privy fixture the
derived domain already misfires: `transaction_hash` is classified into the same value
domain as `input_token` (a tx signature and a mint are not the same domain).

### W2 — the planner will tell an agent to mutate state to obtain an id

§2(b). ~~18 of 39 Privy plans.~~ **14 of 25 privy supplier choices / 19 of 69 repo-wide
(corrected 2026-08-07).** The severity claim stands as far as it goes — the plan is
well-formed, type-correct, and would pass `chain_eval`; it just creates a user, or an
intent-bound signing key, that nobody asked for.

What changed is the *shape* of the weakness, and therefore its owner. It is **not a
ranking bug**: 12 of the 14 have no alternative to rank against. It is a **reporting**
gap — a plan that must mutate to obtain an id does not say so anywhere a reader or a
policy gate can see it. `PlanStep` carries `method`, but nothing in `Plan`, `ExplainEntry`
or the projections asserts "this chain has a side effect you did not ask for". Making
that visible is the honest fix and it is a different change with a different owner
(`context-engineer` for what the agent sees, `staff-engineer` for what a plan asserts).
Downgraded accordingly: still real, no longer the second-most-costly item.

### W3 — the program graph's honest-gap guarantee has three holes

**W3a — a cycle in seed dependencies is silently rendered as a plausible order.**
`_derivation_order` (`program_graph.py:247-261`) falls back to declaration order on a
cycle and sets no flag anywhere. Verified with a two-PDA cycle:

```
derivation_order: ('a', 'b')      a.resolvable = True   b.resolvable = True
to_json()["derivation_order"] = ["a", "b"]   # no cycle marker
```

An orchestrator ingesting that derives `a` first, which needs `b`. It fails at build
time, far from here — the exact failure mode the charter calls the worst this codebase
can produce. Same hole in `derivation_order_for` (`program_graph.py:265-295`), which is
what `find_start` uses. A self-seeded PDA (`a` seeded by account `a`) is likewise emitted
as `resolvable=True` with order `('a',)`.

The loop is bounded, so there is no hang — the guard against *hanging* exists; the guard
against *lying* does not.

**W3b — an account with no recipe defaults to `extracted`.** `find_start._account_step`
(`find_start.py:409-456`) ends in `return DeriveStep(account=name, provenance="extracted")`
for any account it knows nothing about. Verified:

```python
_account_step("totally_made_up_account", None, recovered={}, overlay_pdas=frozenset(), overlay_why={})
# -> DeriveStep(account='totally_made_up_account', provenance='extracted', note='', resolver=None)
```

`extracted` means "straight off the surface" — an affirmative claim. Absence of knowledge
and a plain IDL-declared account are indistinguishable. A typo in a hand-authored
`StartSpec.accounts` ships as a confident `extracted` step.

**W3c — `cross_surface` is unreachable from `find_start`.** `AccountProvenance` has four
values (`provenance.py:73`) and `_account_step` can emit only three. Live consequence:

```
jupiter/route derive plan: {'extracted': 8, 'recovered': 1}
  token_program, user_transfer_authority, user_source_token_account,
  user_destination_token_account, destination_token_account, destination_mint,
  platform_fee_account, program   -> all 'extracted', no note
```

Meanwhile `jupiter_landing._label_provenance` (`jupiter_landing.py:153-166`) correctly
labels the route legs `cross_surface`, and `tests/test_jupiter_route_landing.py:119`
asserts 16 of them. **Two code paths, one ladder, contradictory answers for the same
accounts.** The structural cause is the same as W3b: `find_start` re-derives provenance
by hand instead of reading it off the artifact, because
`orquestra_comprehend.comprehend_project` computes it and throws it away
(`orquestra_comprehend.py:199-249`).

### W4 — the bare-`id` REST chain is invisible

§2(c). Recall gap, two dead code branches, one dead basis string.

### W5 — scale and ordering hazards that are currently latent, not live

- **Entity-spine edge growth.** Rule 1 is exempt from genericity by design. On Privy
  (**re-measured 2026-08-07**; the draft's 491 / fan-in 24 / 1,184 op→op / ~7.4 per op
  are withdrawn): `entity:wallet` alone mints **381** of the 802 high `feeds` edges, the
  largest param fan-in is **22** (`custom_user_id`, `fid`, `telegram_user_id`), and the
  projection collapses to **717** distinct op→op high edges over 159 ops (**~4.5 per
  op**). Nothing is wrong, but there is no bound, and the supplier chosen among equals is
  picked alphabetically by node id — deterministic but arbitrary. *Correction: that
  alphabetical tiebreak is **not** the mechanism behind W2. §2(b) shows the `cost` term
  is: it eliminates 21 of the 22 `custom_user_id` suppliers before the tiebreak is ever
  consulted, leaving only the input-free `createUser`.*
- **`_resolve` has no memoisation.** Safe at `max_ops=3` (budget 2). Raising `max_ops`
  makes the search combinatorial in fan-in. Worth a comment, not a change.
- **`correlate._generic_names(a.graph)`** is computed from surface A only
  (`correlate.py:443`) and applied to all links. Correct today only because every
  non-`a is b` pair is cross-surface and returns early — fragile if the pairing ever
  changes.

### What is untested that would hurt most if wrong

1. No test asserts a plan never sources an input from a mutating operation — because no
   such rule exists (W2). *Still true, but see R5: the measured population is 19 of 69
   plans and only 2 are rankable, so the missing artifact is a visibility marker on the
   plan, not a rule.*
2. No test covers a **cycle** in `_derivation_order` / `derivation_order_for` (W3a).
   `tests/test_program_graph.py:47` tests the acyclic dependent case only.
3. No test asserts that an account with no recipe is *not* tagged `extracted` (W3b).
4. No test pins a bound on `correlate`'s `plan_eligible` count for a real spec — which is
   why W1 has been invisible.
5. `scoped-id:` basis (`graph.py:857`) appears in zero tests and zero outputs.

---

## 4. Evaluation honesty

### What is measured today

**`find_start` retrieval eval** (`gecko/retrieval_eval.py`, 32 committed golden rows,
`gecko/providers/configs/orquestra/find_start_golden.jsonl`). Current, reproduced:

```
recall@1 0.74 | recall@3 0.89 | MRR 0.81  (over 27 wired-gold rows)
causes: hit=28, misrank=2, vocabulary_gap=1, coverage_gap=1
floor check: 4/4 out-of-scope intents honestly rejected; 0 false accept(s)
```

*(Re-measured 2026-08-07 on `origin/main` @ `c0a96d9` via
`gecko.retrieval_eval.evaluate_golden()`. The draft's `recall@3 0.85 | MRR 0.80` and
`hit=27, vocabulary_gap=2` are stale — one vocabulary_gap row became a hit; recall@1 and
the floor are unchanged. Note `evaluate_golden()` takes no argument: passing the golden
**text** raises `TypeError` from `Path(...)`.)*

This is the best-designed measurement in the repo. Three things it gets right that most
retrieval evals get wrong: the recall denominator excludes un-retrievable golds so wiring
is not conflated with ranking; the miss-cause vocabulary is **closed** and pre-commits
which causes count as evidence for a semantic flip; and out-of-scope rows measure the
*floor* (precision) separately from the ranker.

**Correlation** has `tests/test_e2e_correlation.py` (a synthetic 2-API pair, asserting
the §13.6 cross-API gate holds through the aggregator) and `tests/test_correlate.py` (12
unit tests of the ladder).

**Chains** have `gecko/chain_eval.py` and `tests/test_chain_eval.py` — two TxLINE chains,
plus a type-mismatch and a missing-source-field negative.

### What the measurement does *not* cover

**a) Correlation precision on real specs is not measured at all.** The two committed
correlation tests are *construction* tests: they assert the code refuses what it is
designed to refuse, on fixtures built to make it refuse. The one measurement over real
specs — `scripts/two_api_probe.py` (Stripe × Adyen), whose result is what justified the
DECLARED-only decision — requires spec files that are **not committed**, so it cannot be
re-run as a regression. Nothing in CI would have caught W1. Nothing would catch it
getting worse.

**b) `chain_eval` measures well-formedness and value-*kind*, not join correctness.** Its
own docstring is honest about this: "well-formed (the shared caller guard) AND
value-kind-correct". A plan that says "create a user to look a user up" is well-formed
and kind-correct. Type consistency is a necessary and very weak condition for a join
being right. (The specific Privy `createUser` chain happens to fail `chain_eval` — but on
missing required body fields, i.e. by luck, not because the harness understands the
semantics.)

**c) The chain golden set is two chains on one API.** `graph.plan()` is exercised
end-to-end against exactly the TxLINE fixtures. ~~The 39 plans the Privy fixture produces
are unmeasured.~~ Corrected 2026-08-07: privy produces **3** plans under enumeration A
and **25** under leave-one-out (§2(b)); none of them is in any golden set, so the point
stands — they are unmeasured *for correctness*, only now with a real denominator.

**d) The retrieval golden set measures the router, not the derive plan.** Every row scores
"did we return the right (program, instruction)". No row asserts anything about the
`derive_plan` the start point carries — its ordering, its provenance tags, or its flagged
gaps. W3a/W3b/W3c are all downstream of retrieval and entirely outside the eval.

**e) 32 rows over 5 programs is small, and the floor's robustness is corpus-size
dependent.** `_distinguishing_terms` (`find_start.py:518-539`) uses a DF ceiling of
`len(cards)/2` = 5.5 over 11 cards. I probed the trap the charter names and could not
break it — `"move my usdc into sol"`, `"get me out of hyUSD into USDC"`,
`"put usdc into a token"` and `"deposit sol and usdc"` all correctly return `no_start`
with GUESS-labelled candidates. That is a genuine pass. But it rests on a threshold that
is a function of a corpus of 11, and no golden row currently pins the two-common-terms
case.

### Would a change that improved the golden set be trusted?

**For `find_start` routing: yes, with one caveat.** The eval replays through the
unmodified router, classifies into a pre-committed closed vocabulary, and reports both
directions (recall *and* false accepts). It is hard to game without visibly moving the
false-accept or coverage-gap counts. The caveat is size: at 27 scoreable rows, one row is
3.7 points of recall@1, so a change of "+1 row" is noise, and the set is small enough
that tuning against it is a real risk. Any change that moves recall by less than ~3 rows
should be treated as unmeasured.

**For correlation and chain quality: no.** There is no set to improve. A change to
`_score` or `_sig_corroborates` today would be validated by 12 unit tests that encode the
author's intent, not by any measurement of what the rules admit on real input. That is
precisely how 479 plan-eligible timestamp/enum links shipped without anyone noticing.
This is the biggest evaluation gap in my lane.

---

## 5. Evolution recommendation — ranked, smallest first

### R1 — tier 2 must require a name-entity match (and drop enum + date-time as bases)

**Problem:** W1. 95% of reported plan-eligible correlations on real specs are
non-join-keys.

**Smallest change:** in `correlate._score` (`correlate.py:265-274`), return tier 2 only
when `src.name_entity == dst.name_entity` is truthy; otherwise fall through (signature
alone becomes a quarantined candidate at most). Separately, in
`graph._sig_corroborates` (`graph.py:284-302`) remove the `enum` equality rung, and
remove `date-time` from `_DISCRIMINATING_FMT` (`graph.py:64-66`). This makes the code
match the docstring it already ships (`correlate.py:20-22`).

**How we would know it worked:** committed before/after counts on the two fixtures.
Predicted: privy `plan_eligible` 498 → 19; pegana 68 → 9. Recall cost on those fixtures:
**zero real joins lost** — every one of the 538 removed links is a timestamp, a money
amount, or a mode enum. Add a regression test pinning `plan_eligible` for the privy
fixture so this can never silently regress again.

**What it breaks:** `correlate`'s tier-2 rung becomes intra-API-name-match-plus-signature
only. `tests/test_correlate.py:165` (`test_intra_signature_tier2_but_cross_signature_not`)
will need its intra case to carry a name entity. `tests/test_graph_foundation.py:111`
touches enums only for signature capture, not corroboration, so it is unaffected. No
executable plan changes — `graph.plan()` never used tier 2.

**Review:** `defi-security-engineer` — **yes**, advisory. It is a *tightening*, but it
touches the basis semantics that the anti-poisoning story rests on, and it is the natural
moment to also decide R2.

### R2 — stop letting prose mint a value domain that reads as a declared format

**Problem:** W1's poisoning path. `_domain_signal` (`graph.py:226-242`) reads
`description` text and writes the result into the *format* slot, indistinguishable from a
spec-declared `format:`. Already misfires on real input (`transaction_hash` classified as
base58).

**Smallest change (two options, pick one):**
- (a) Restrict `_domain_signal` to the **example-shape** channel only (`_BASE58_RE` match
  on a spec-authored example) and drop the `_BASE58_HINTS` prose scan. The example is
  structured data, not free prose.
- (b) Keep the prose channel but mark it: emit `base58~` (or a distinct slot) so
  `_sig_signal` can report `format-eq~derived` rather than plain `format-eq`.

I lean (a): it is smaller, it removes an attacker-controllable channel entirely, and the
example channel is the one that actually carries the Pegana/Birdeye/Jupiter mint join.

**How we would know:** count nodes carrying a derived `base58` domain before/after on the
privy + pegana fixtures (today: 10 and 3). Assert `transaction_hash` no longer carries it.

**What it breaks:** any join that today rests only on a prose hint. On the two fixtures,
after R1 this is zero plan-eligible links.

**Review:** `defi-security-engineer` — **yes, mandatory.** This is squarely "could an
inferred fact pass as a declared one".

### R3 — the derive plan must not default to `extracted`

**Problem:** W3b + W3c. Absence of knowledge is reported as an affirmative surface claim,
and `find_start` contradicts `jupiter_landing` about the same accounts.

**Smallest change:** in `find_start._account_step` (`find_start.py:409-456`), the terminal
branch must distinguish "this account is a known non-PDA slot named by the IDL"
(`extracted`) from "we hold nothing for this name". The cheapest honest form: return
`extracted` only when the account name appears in the program config's known account set,
and `flagged` otherwise, with note `"no recipe and not named by the surface"`. Separately,
let a provider `StartSpec` declare `cross_surface` accounts so `find_start` and
`jupiter_landing` agree.

**How we would know:** `jupiter/route`'s plan stops claiming `extracted` for the route
legs; a synthetic `StartSpec` with a phantom account name yields `flagged`, not
`extracted`. Add both as tests.

**What it breaks:** `jupiter/route`'s derive plan gains flagged/cross_surface entries;
the CLI rendering (`find_start.py:808-816`) already handles both. Anything asserting an
exact provenance histogram for jupiter will need updating.

**Review:** `defi-security-engineer` — **yes, mandatory** if `cross_surface` becomes
emittable from a provider-authored `StartSpec`, since that is a ladder value crossing into
a new producer. The `extracted → flagged` tightening alone is not a ladder change.

### R4 — flag the cycle instead of emitting a plausible order

**Problem:** W3a. A cyclic (or self-seeded) PDA dependency is emitted as a confident
derivation order with `resolvable = True` and no marker.

**Smallest change:** `_derivation_order` (`program_graph.py:224-262`) already detects the
cycle (`if not progressed`). Have it return the residual cycle members alongside the
order, mark those `AccountRef`s `resolvable = False`, and surface a `cycle` key in
`InstructionGraph.to_json()`. `derivation_order_for` should propagate the same signal so
`find_start` can emit those accounts as `flagged`.

**How we would know:** the two-PDA-cycle IDL and the self-seeded IDL from this assessment
become failing-then-passing tests; `to_json()` carries the cycle.

**What it breaks:** `InstructionGraph.to_json()` gains a key — additive, so an
orchestrator ignoring it is unaffected. `AccountRef.resolvable` changes for cyclic
accounts, which is the point.

**Review:** none needed — this is invariant #3 being enforced, not relaxed.

### R5 — ~~prefer a read-shaped supplier over a mutating one~~ → **WITHDRAWN**

**Verdict, 2026-08-07: do not make this change.** The original recommendation rested on
"18 of 39 Privy plans", which did not reproduce (§2(b)). Re-measured, the change is not
worth its cost.

**Measured effect of both placements**, simulated against the shipped candidate loop over
every committed fixture. `effect = 0` for `GET`/`HEAD`, `1` otherwise:

| placement | privy mutating-sourced | flips | note |
|---|---|---|---|
| shipped `(cost, rank, node)` | 14 / 25 | — | baseline |
| P1 `(cost, effect, rank, node)` — tie-break only | 12 / 25 | 2 | strictly safe, changes nothing else |
| P2 `(effect, cost, rank, node)` — the draft's "before `cost`" | 12 / 25 | 2 + 1 synthetic | **actively worse**, see below |

Four reasons to withdraw:

1. **It fixes 2 of 69 plans repo-wide, and 0 outside privy.** Every other committed
   fixture has zero contested choices.
2. **It does not fix the case that motivated it.** `getUserByCustomAuthId ← createUser`
   is sole-candidate. So are 11 of the other 13 privy cases.
3. **The draft's own placement ("before `cost`") is harmful.** On
   `tests/fixtures/graph_poisoned.json` P2 flips
   `getWidgetStats/widgetId: POST trackEvent (cost 1) → GET getWidget (cost 2)` — but
   `getWidget` itself sources `widgetId` from `trackEvent`. The mutation is not removed;
   it is pushed one level deeper, out of the top-level explain entry, and the plan grows
   an op. That is the opposite of the goal: it *hides* the side effect.
4. **It puts a weak signal into the one ranking function that is currently 100%
   structural.** HTTP method mislabels 19 of privy's 105 non-`GET` ops (§2(b)).

**What to do instead:** make the side effect *visible* rather than silently reordering it
away — carry the producer's method/effect onto `ExplainEntry` (or a `Plan`-level marker)
so a chain that mutates to obtain a read says so in every projection and to every policy
gate. That is invariant #3 (an honest gap beats a plausible guess) applied to plans, it
addresses all 19 cases rather than 2, and it cannot change plan content.

**Note for whoever revisits this.** The effect-vs-provenance ordering question the draft
flagged for `staff-engineer` is currently **unmeasurable**: every committed fixture has
**0 DECLARED `feeds` edges**, so `_PROV_RANK` is a constant in every plan choice we can
observe. It only bites once a customer confirms an entity hint *and* a DECLARED mutating
supplier competes with an INFERRED read-only one at equal cost. Do not pick that ordering
without a fixture that exhibits it.

**Review:** n/a — nothing to review; no change proposed.

### R6 — light up the bare-`id` REST chain (two-sided resource-noun scoping)

**Problem:** W4. The most common two-call REST chain produces no edge.

**Smallest change:** two symmetric fixes.
- Consumer side (`graph.py:830-836`): when a path param normalizes to `"id"` and yields no
  entity of its own, scope it by the path's resource noun — i.e. make the currently-dead
  `rnoun` branch reachable.
- Producer side: when a response field normalizes to `"id"` and has no parent object
  title, scope it by the producing op's resource noun.

This also revives the `scoped-id:` basis (`graph.py:857`), which should then be used for
these edges so they are auditable as a distinct class.

**Measured effect** (simulated offline against the shipped rules, no code changed):

| fixture | new HIGH edges | verdict |
|---|---|---|
| pegana | **+3** | all correct: `list_webhooks.id → delete_webhook{id}`, `→ patch_webhook{id}`, `patch_webhook.id → delete_webhook{id}` |
| pegana_p0 | +3 | same three |
| privy (golden) | **+0** | no false positives — privy's bare-`id` producers all carry parent titles |
| txline | +0 | — |

Small, precise, zero false positives on the fixtures I have.

**How we would know:** the reproduction case in this document (`GET /customers` +
`GET /customers/{id}`) becomes a failing-then-passing test; the +3/+0/+0 table becomes a
regression assertion.

**What it breaks:** more `feeds` edges, therefore more plans where there were none. Since
this is a **recall** change it can only *admit*, never remove — so it must be reported
with what it newly admits (the table above), and re-measured on any spec added later. Do
R5 first: without an effect preference, new edges are new opportunities for W2.

**Review:** none for the ladder (INFERRED stays INFERRED). This is the change most likely
to manufacture wrong-but-plausible edges on a spec I have not seen, so it should land last
and with the widest fixture sweep.

### R7 — carry program-surface provenance on the artifact instead of re-deriving it

**Problem:** the structural cause of W3b/W3c. `comprehend_project` computes an accurate
per-PDA `ProgramProvenanceTier` and discards it; `find_start` reconstructs it by hand from
`StartSpec.recovered` maps that a human maintains in a second place.

**Smallest change:** add an optional `origin` field to `PdaNode` (or a sibling
`{account: tier}` map in the packaged program config), populate it in
`comprehend_project` where the tier is already computed
(`orquestra_comprehend.py:199-217`), and have `find_start._account_step` read it before
falling back to the hand-authored maps.

**How we would know:** for each wired program, `find_start`'s per-account tags equal
`comprehend_project`'s tiers. That equality becomes a test, and the hand-authored
`recovered` maps shrink to *notes* (the human explanation) rather than being the *source
of truth* for the tier.

**What it breaks:** the packaged config schema changes (additive, defaulted). Every
packaged Orquestra config needs regenerating. This is the largest item here and should
only be done after R3 has made the read-time behaviour honest — R3 fixes the lie, R7
removes the opportunity for it.

**Review:** `staff-engineer` (config schema is the engine/adapter seam) and
`defi-security-engineer` (a provenance tier becoming a data-file field means the tier is
now settable by whoever writes the config — that is a real trust-boundary question and it
is the reason to do this deliberately rather than casually).

### R8 — a committed correlation precision fixture

**Problem:** §4(a). There is no measurement that would have caught W1, and none that will
catch its regression.

**Smallest change:** a `tests/` assertion that pins `correlate_surfaces(privy, privy)`'s
`summary` — total, plan_eligible, and by_tier — for the already-committed Privy fixture,
plus the same for Pegana. Not a new corpus; two numbers over fixtures we already ship.

**How we would know:** the counts are pinned, so R1's effect is visible as a diff and any
future widening has to justify itself in the same commit.

**What it breaks:** nothing. Any rule change now has to state what it newly admits, which
is the charter's own working rule made mechanical.

**Review:** none.

---

## Ordering

R1 + R8 together (the fix and the measurement that proves it), then R2, then R3, then R4,
then ~~R5~~, then R6, then R7 last.

R1/R2/R3/R4 are all *tightenings* — they can only remove or downgrade claims, so they are
safe to land quickly. ~~R5 changes plan content.~~ R6 is the only one that admits new
edges and should land with the widest sweep. R7 is a schema change and should be
sequenced deliberately.

**Status 2026-08-09:** all Series-2 recommendations are resolved except one.
R1+R8 landed (#313), R2 (#323), R3 (#324), R4 (#320), R6 (#321). **R5 is withdrawn**
on re-measurement — see R5. **R7 is pending — design in progress**: it is a
config-schema change on the engine/adapter seam (`staff-engineer`) that makes a
provenance tier a data-file field, so the `defi-security-engineer` gate applies
*before* code, per the R7 review note above. R7 is the only outstanding item from
this document.

*(Previous status line, 2026-08-07, superseded: "R1/R8 landed (#313), R4 (#320),
R6 (#321), R2 (#323), R3 (#324). R5 is withdrawn on re-measurement — see R5. R7
remains open and is now the only outstanding item from this document.")*

## Things I checked and found sound — do not "fix" these

- **`feeds` is never `EXTRACTED`.** Locked by construction and by
  `tests/test_graph.py:81`. I could not produce a counter-example.
- **Cross-API is effectively binary.** `correlate._score`'s cross branches
  (`correlate.py:224, 252-263, 283-296`) and `compose.cross_plan`'s `confirmed` gate
  (`compose.py:145-153`) agree, and `tests/test_e2e_correlation.py` locks the agreement
  through the aggregator. A poisoned provider `x-gecko` cannot mint an executable
  cross-surface plan.
- **The planner's cycle guard and depth budget.** `graph.py:584` + the `budget`
  parameter. Correct, and `plan()` refuses rather than truncates.
- **`find_start`'s named-or-corroborated floor.** I attacked it with four
  two-common-noun intents carrying no naming term; all four correctly returned `no_start`
  with GUESS-labelled candidates. The floor gates only runnable cards
  (`find_start.py:744`), which is the right scope.
- **Content addressing and determinism.** Byte-identical rebuilds, sorted serialization,
  namespace as a one-way hash input.
- **`_ID_TYPES` as a hard gate on rule 1.** The `paid`/`valid`/`uuid` entity-heuristic
  misfire is correctly neutralised by the id-shape filter (`graph.py:854`), and
  `tests/test_graph.py:125` pins it.

## Reproducing the numbers

Every figure came from throwaway probes against committed fixtures. The load-bearing
ones, in order of importance:

1. `correlate_surfaces(Surface.from_spec("tests/fixtures/golden/privy_openapi.json"), same)`
   → `summary`, then group tier-2 links by `basis.signals` and count those with
   `basis.entity is None`.
2. **(corrected 2026-08-07)** Plan counts are enumeration-dependent — state the
   enumeration or the number is meaningless. **A:** for each op,
   `graph.plan(g, op, frozenset())`, count non-`None` with >1 step. **B (leave-one-out):**
   for each op × each required non-auth input, `graph.plan(g, op, <the other required
   names>)`. Then check each `explain` entry's `source_op` method against
   `POST|PUT|PATCH|DELETE`. For the number that decides a ranking change, replay
   `_resolve`'s candidate loop directly (`g.feeds_into(param_node)` →
   `g._resolve(src_op_node, sat, budget=1, visited={op_node})`, mirroring
   `plan(max_ops=3)`) and count choices where ≥2 candidates tie on `(cost, rank)` and
   disagree on effect class.
3. `build_graph` over a two-op synthetic spec (`GET /customers` producing a titled
   `Customer.id`, `GET /customers/{id}`) → assert `feeds == []`.
4. `build_program_graph` over a two-PDA IDL where each seeds from the other → assert
   `derivation_order == ('a','b')` with both `resolvable=True`.
5. `find_start._account_step("phantom", None, recovered={}, overlay_pdas=frozenset(), overlay_why={})`
   → `provenance='extracted'`.
6. `gecko.retrieval_eval.evaluate_golden()` → the recall/cause table.
