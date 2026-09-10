# Retrieval review: what is actually wrong with the product core

Written 2026-09-10, at the founder's request, after PR #529. Every number below is
reproducible offline at $0. Where I could not reconcile two measurements, I say so
rather than pick the flattering one.

The founder's framing was four claims. **One is right, one is right for the wrong
reason, one is backwards, and one is a category error.** Taking them in the order
that matters rather than the order they were asked.

---

## 1. The finding that blocks everything else

**You cannot currently evaluate any semantic retriever. The scorecard reports 0.00
for a perfect one.**

Four facts, each verified by reading the code, that compose into one defect:

| # | Where | What it does |
|---|---|---|
| 1 | `tests/test_golden_set.py:90` | CI-enforced invariant: every `paraphrase_no_overlap` goal shares **zero** tokens with its gold op's haystack, stopwords included |
| 2 | `catalog.py:171` | `score()` is set-intersection cardinality — `len(query_tokens & hay)` |
| 3 | `search.py:209` | `is_fallback = name not in lex_genuine`, and `lex_genuine` holds only lexical hits scoring `> 0` |
| 4 | `evaluate.py:219` | `genuine = [p for p, h in matches if not h.is_fallback]` — the `ranker` reading keeps only non-fallback hits |

Compose them. On a paraphrase query the gold op scores 0 lexically **by construction**
(1→2). So it is never in `lex_genuine`, so every fused hit carrying it is flagged
`is_fallback=True` (3). So the `ranker` reading drops it (4).

**An oracle dense arm that returns the gold at rank 1 for every query still measures
paraphrase recall@3 = 0.00.** Ship FAISS tomorrow, ship Voyage, ship a cross-encoder —
our own scorecard reports each one bought nothing.

The `is_fallback` contract has to be decided **before** any retriever is evaluated, and
the out-of-scope cost of loosening it measured first: a dense arm always returns
something, so the OOS floor may drop. Today `OOS = 1.00` on every row precisely because
`is_fallback` is lexically anchored. That is a real property being paid for, not an
accident — the question is whether it is worth its current price.

### The defect has already happened once, to a document we have been quoting

`private/2026-08-14-local-dense-probe.md` reports paraphrase@3 moving **above** zero with
dense — txodds 0.20 → 0.40, pegana 0.00 → 0.50. Under the chain above that is impossible.

**Settled by reproduction on 2026-09-10:** the probe quoted `with_fallback`, the reading
`evaluate.py:173` itself calls *"inflated, and not a ranker number"*. Re-running
`evaluate_golden` on the committed fixtures, split by archetype under both readings:

```
=== txodds ===                    ranker   with_fallback
keyword_echo              n=4       1.00            1.00
near_dup_disambiguation   n=3       1.00            1.00
paraphrase_no_overlap     n=5       0.00            0.20

=== pegana ===
paraphrase_no_overlap     n=4       0.00            0.00
```

The probe's own **lexical control row** reports txodds `para@3 = 0.20` — reproducible
today only under `with_fallback`. A lexical arm cannot exceed zero on tasks a CI
invariant guarantees share no token, so the control row settles it without needing the
probe script, which was a scratchpad and no longer exists.

So the masking is exactly as total as the code reads, and this stops being a prediction
about what the scorecard *would* do to a dense arm. **It already did it to one, in
August, and that document has been quoted since.**

### What survives, and what has to be re-derived

| claim | status |
|---|---|
| near-duplicate 0.77 → **1.00** | **safe to quote** — recorded with `fb-only = 0`, so no cell depended on a fallback-flagged hit |
| privy aggregate 0.80 → 0.93 | **do not quote** — an aggregate mixing a paraphrase bucket (which the fix zeroes) with a near_dup bucket (which survives) |
| birdeye aggregate 0.45 → 0.62 | **do not quote** — same defect |
| every paraphrase row | unmeasurable until the metric is fixed |

Per-archetype numbers under `ranker` are sound; the aggregates are recoverable from them,
but nobody has done it yet.

---

## 2. "Our lexical was returning zero, and it's our product core"

**Right observation, wrong diagnosis.** The zero is arithmetic, not a ranking failure.

`paraphrase_no_overlap` is a deliberately constructed **worst case**: the golden set
forbids the query from sharing any token with the target. Zero overlap ⇒ zero score ⇒
filtered out. A lexical ranker scoring above 0.00 on that archetype would mean the
invariant had broken.

Read the per-archetype table, never the aggregate — the aggregate blends a floor, a
ceiling and a middle over buckets of arbitrary size, so it moves when the *mix* changes:

```
                          B (overlap)   C (BM25)
txodds    keyword_echo         1.00        1.00
          near_dup             1.00        1.00
          paraphrase           0.00        0.00
birdeye   keyword_echo         0.93        1.00
          near_dup             0.67        0.67
          paraphrase           0.06        0.00
privy     keyword_echo         0.82        0.82
          near_dup             1.00        1.00
          paraphrase           0.00        0.00
```

Keyword echo is 0.82–1.00. The system is not broken at what lexical retrieval is for.

---

## 3. "We're not using a complete lexical system"

**Backwards. We built one, measured it, and it lost.**

Okapi BM25F — IDF, TF-saturation, length normalisation, OpenAPI-remapped field weights —
exists as arm C in `scripts/retrieval_arms_eval.py` and ships in `catalog.BM25Index`.
Pooled against the shipped overlap arm:

| metric | overlap (B) | BM25 (C) |
|---|---|---|
| recall@5 | **0.597** | 0.623 → *worse pooled* |
| MRR | **0.563** | 0.545 |
| near_dup recall@1 | **0.706** | 0.529 |
| keyword_echo recall@1 | 0.93 | **1.00** |

BM25 wins one bucket and loses two. It is the right tool at 500+ ops and the wrong tool
at 21. Not building it is not the gap; **it is built, and it is currently a net loss.**

---

## 4. "Why aren't we using FAISS + HNSW?"

**Category error.** FAISS and HNSW are approximate-nearest-neighbour *index structures*.
They trade recall for speed when you have millions of vectors. They do not produce
embeddings and they do not improve ranking — they make an existing vector search faster
and slightly worse.

Our live wired catalog is **21 cards**. The full Orquestra catalog is **4,534 programs**.
Brute-force cosine over either is microseconds. HNSW would add approximation error to a
search that is not slow.

What we lack is a **semantic arm at all**, not a faster index — and one already exists,
unwired, measured, offline and free.

### The local dense arm, measured

`BAAI/bge-base-en-v1.5` via fastembed — ONNX, 0.21 GB, no torch, no key, no network:

The one figure that survives the metric defect in §1, because it is recorded with
`fb-only = 0` — no cell in it depended on a fallback-flagged hit:

| set | ops | near-duplicate recall@3 |
|---|---|---|
| birdeye | 89 | 0.77 → **1.00** |

The probe's aggregate recall@3 figures (privy 0.80 → 0.93, birdeye 0.45 → 0.62) are
**not quoted here**: they blend a paraphrase bucket the metric fix zeroes with a near-dup
bucket that survives, so they carry the same defect and must be re-derived under `ranker`
before anyone repeats them.

`OOS` stays 1.00 on every row, by construction — the dense arm cannot certify a nonsense
query, because confidence remains lexically anchored.

The dense/RRF gate (`ops > 50 AND BM25 recall@3 < 0.8`) **fires on birdeye today**
(ops=89, 0.50). It is the one surface where the spec's own condition says dense is
justified, and the arm that would serve it is not wired.

---

## 5. Where the bottleneck actually is: the cards, not the retriever

`gecko/vocab_gap.py`, measured on the founder's own phrasing across the six wired intents:

```
usdg          0 cards
convert       0 cards
stablecoin    0 cards
usdc          1 card    (metadao_ico.plan_fund — not a swap)
swap          2 cards
```

A user stating their constraint the way users state it — *"I hold USDG, buy me a
coffee"* — **cannot reach a swap through any ranker**. The words are absent from the
haystack, so every arm scores zero by arithmetic. `tests/test_retrieval_eval.py:436`
names the debt exactly: `meteora.swap`'s card *"describes the IMPLEMENTATION and never
the words a person says."*

This is the same shape as the paraphrase archetype, except **nobody chose it**.

### The trap on fixing it

PR #528 measured that adding token symbols as card terms would inflate recall on
**11 of 52** golden rows by teaching the eval its own answers, and push the floor down
on **2** out-of-scope rows. Do not write symbols into cards without reading #528. The
lever is a better *blurb* — the words a person says — scored against a golden set that
did not learn them.

---

## 6. Scale is not the problem either

Measured 2026-09-09: adding all 38 Raydium CLMM instructions as distractors — including
its own `swap` and `swap_v2`, colliding with the wired Orca card — moved recall@1,
recall@3 and MRR by **0.000**. What moved was false accepts, 2 → 3.

Ranking does not collapse at scale. **The floor does.** Same conclusion as the August
N-curve (19 → 4,511 programs cost 6 points of recall@1).

---

## 7. The ingestion graph

353 nodes across 16 modules, extracted from a 5,953-node graph of `gecko/`.
`graphify-out/` is gitignored, so the artifact is not in this PR — regenerate with
`/graphify gecko`, then filter nodes whose `source_file` stem is one of the sixteen
modules below. The spine, by cross-module edge count:

```
                    ┌──────────┐
                    │  ingest  │  ← the root: everything reads Operation
                    └────┬─────┘
        ┌────────────┬───┴────┬─────────────┐
     tools(16)   catalog(8) surfacedoc(5) evaluate(8)
        │            │          │
        └──────┬─────┘          │
            search ──► dense ───┘
               │  └─► fusion(3)
               ▼
            client ──► caller(21), search(16), dense(9),
                       catalog(8), sample(7), tools(7)
```

Two things the shape says:

**`ingest` is load-bearing and has no semantic layer above it.** `tools`, `catalog`,
`surfacedoc` and `evaluate` all read it directly. Whatever words `ingest` fails to put
into an `Operation` are absent from every downstream arm simultaneously — which is
exactly the vocabulary gap, visible as topology.

**`search` is the only place the two arms meet** (catalog 8, dense 5, fusion 3). That is
the right shape: one fusion point, injected seams either side. The architecture is
sound. It is unwired, not misdesigned.

---

## 8. The dogfood test: would our own graph have found §1?

We sell "ask the graph before you act." So the question is not rhetorical. I built a
5,953-node graph of `gecko/` and asked it the question §1 answers.

**It would not have found it.** It gets close, then fails in the way we sell against.

### It has the pieces and the connectivity

Every concept in the chain is a node — `is_fallback`, `.score()`, `FusedHit`,
`evaluate_golden()` — and they connect:

```
.score() -> evaluate_golden()   (4 hops)
   .score() -> .search_scored() -> Catalog -> AgentApiClient -> evaluate_golden()
```

### And the path is worthless

It routes through `AgentApiClient` — **189 edges**. A path through a hub that everything
touches is not evidence of a causal chain; it is evidence that everything touches the
hub. `Operation` is 123 edges, `Receipt` 99. The graph says *these things are in the same
neighbourhood*. It does not say `is_fallback` **masks** dense hits from the ranker
reading. That came from reading four specific lines.

### Then the part that stopped the experiment

Asked in the words a person would use — *"does anything hide dense results from the
scorecard"*:

```
nodes reached: 1697        (noise: matched 'from', 'anything', 'results')

mask       reaches    0 of 5953 nodes
hide       reaches    0 of 5953 nodes
suppress   reaches    0 of 5953 nodes
```

**Zero.** The mechanism *is* in the corpus — `search_rationale_76` carries the docstring
that explains it — and it is unreachable by the question.

That is `usdg -> 0 cards`, in our own repository, measured an hour after we documented it
in someone else's. **Our graph has our vocabulary gap.**

### What it would need to pass

| # | Gap | What it costs us today |
|---|---|---|
| 1 | **Hub penalty in path explanation** | Every "why does X connect to Y" answers "through `AgentApiClient`". Betweenness makes hubs the answer to everything, which makes them the answer to nothing. |
| 2 | **Direction and edge semantics** | Built undirected with `references`/`uses`. "A gates B" and "B is passed to A" are the same edge. The mechanism *is* the direction. |
| 3 | **Retrievable rationale** | The why is captured as prose on `rationale` nodes and is not reachable by the words a person asks with. Same lever as the cards — and the same trap: not a synonym table. |

Item 3 is the one to be careful with. Making rationale retrievable by rewriting it toward
expected questions is how we would teach our own eval its answers — the §5 trap, one
level up.

---

## 9. The last inch: the pattern behind all three findings

`gecko/graph.py` is 1,071 lines. It builds a deterministic, content-addressed graph from
ingest's `Operation`s, carries provenance on every edge, and walks it to plan chain-shaped
intents (`fixtures/snapshot` -> `odds/updates` via `FixtureId`). Twelve modules import it.
It is exactly "what this API is, and the best next call."

It is exposed over MCP as `get_surface_graph`. Its own docstring:

> *"hidden: callable by name, **not enumerated in list_tools**"*

An agent that does not already know the door exists will never open it.

### Three instances, one shape

| capability | state |
|---|---|
| local dense arm — measured, $0, gate fires on birdeye | built, **not wired** |
| surface graph — 1,071 lines, provenance, chain planning | built, served, **not advertised** |
| `is_fallback` scorecard | built, and it **hides** what it measures |

Three modules, three weeks, one pattern: **we close 95% of the distance to the consumer
and stop.** `54ce7a7 fix(serve): unlisted surfaces — served, advertised nowhere (#515)`
is the fourth instance, already fixed once as an incident rather than as a rule.

### The mechanism, so it scales

1. **A reachability gate in CI.** For every capability the product claims, assert a cold
   agent can reach it: it appears in `list_tools`, its description contains the words a
   user would say, and a golden intent routes to it. A capability no enumeration mentions
   fails the build. This generalises #515 from an incident to a standing rule.
2. **Un-hide `get_surface_graph`, then measure the cost.** The stated reason for hiding is
   token budget — already solved by the scoped projection. Enumerate it, measure the
   delta, let the number decide instead of the caution.
3. **Wire the surface graph into `search`.** Retrieval ranks operations independently
   today. The graph knows which op supplies which. A hit whose supplier is already in the
   plan should not rank like an orphan. *The best next call* is the thing we sell, and it
   currently cannot influence which call is proposed first.
4. **Ask the graph first, and log the answer.** Every review, fix or plan opens with a
   graph query; the query and its answer go in the PR. When the graph cannot answer, that
   failure is the finding — as it was here.

## 10. What to do, in order

1. **Fix the metric first.** Decide the `is_fallback` contract and measure the OOS cost
   of loosening it. Until this lands, every retrieval number — including the local dense
   probe's paraphrase rows — is uninterpretable. Nothing else on this list is worth
   doing before it.
2. **Re-derive the probe's aggregates** under `ranker`. The per-archetype numbers are
   sound; the aggregates that have been quoted since August are not.
3. **Wire the local dense arm** behind the existing `DenseIndex` seam. It is measured,
   offline, $0, CI-runnable, and the gate already fires on birdeye. The one win that
   survives the metric defect: near-duplicate 0.77 → 1.00.
4. **Author the blurbs**, using `vocab_gap.py` as the worklist — the words users say,
   not the implementation. Read #528 first.
5. **Leave BM25 where it is.** Revisit at 500+ ops with a fresh measurement.
6. **Do not adopt FAISS or HNSW.** Revisit if a single surface exceeds ~100k vectors.
7. **Land the reachability gate** (§9). It is the only item here that prevents the next
   instance rather than fixing the last one.
8. **Give the graph a hub penalty and directed edges** (§8) before relying on it for
   review. Until then it locates neighbourhoods, not mechanisms — useful, and not what
   we claim.

## What not to do

Do not add a synonym table mapping `usdg → mint`. It would improve the number while the
agent still cannot tell a user which pool converts their balance. `vocab_gap.py`'s
docstring already refuses this, and it is right to.
