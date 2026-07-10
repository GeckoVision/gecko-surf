# context-hub adoption — RETRIEVAL & comprehension-quality slice

Date: 2026-07-09
Owner lane: `ai-ml-engineer` (intent→endpoint retrieval + first-call-correct eval)
Status: SPEC / DECISION. No implementation in this document.
Verdict on context-hub overall: **COMPLEMENT** — the one convergent overlap is the
lexical retrieval shell. This spec covers only that slice.

Parallel seam: progressive disclosure (chub's entry-point-then-`--full` fetch) is
scoped by the `context-engineer`'s spec, NOT here. This document owns *ranking
quality* (which op comes back and in what order); that document owns *disclosure
depth* (how much of the chosen op's surface is materialized). They meet at
`search_capabilities` → tool-def payload; neither should restate the other.

---

## 0. TL;DR for the founder (~10 lines)

- chub ships a real Okapi **BM25** (IDF + TF-saturation + length-norm + multi-field
  weights + inverted index). Our `catalog.py` scorer is a naive **token-overlap
  count** (summary double-weighted). BM25 is a genuinely stronger *lexical* ranker
  — not the same leg we already have.
- I measured our current substrate recall on the frozen golden sets:
  **txodds recall@3 = 0.67, pegana recall@3 = 0.60 — both already < 0.8.** So one of
  the two hybrid gate conditions is *already met* on the ranked substrate.
- BUT both surfaces are **18 / 26 usable ops (< 50)**, and below 50 ops `scale.py`
  shows the agent **every** tool (surface-all), so substrate rank is **decoupled
  from FCC** — the agent already sees the right op regardless of its rank. Lifting
  substrate recall@3 with BM25 buys a prettier metric, not a better first call, at
  our scale.
- chub itself is **pure lexical BM25 — no dense, no hybrid** — at package-registry
  scale (hundreds+ of entries). That is external corroboration for our "no vectors
  yet" stance, and it tells us the right *lexical half* of any future hybrid is BM25,
  not our overlap count.
- **Decision: don't-adopt-yet into the live path; adopt BM25 as the pre-committed
  lexical arm the moment the op-count gate (>50) fires.** Ship the falsification
  experiment (§4) so this stays a measured call.

Top 3 decisions in §5.

---

## 1. Does chub's BM25 + progressive disclosure measurably improve OUR retrieval and FCC?

### 1a. BM25 vs our current lexical scorer — is there a ranking delta?

Yes, there is a real algorithmic delta. They are not the same lexical leg.

Our scorer (`gecko/catalog.py`, `CatalogEntry.score`):

```
len(query_tokens & haystack_tokens) + len(query_tokens & summary_tokens)
```

Pure set-intersection cardinality. **No term frequency, no IDF, no length
normalization.** Every matched token counts 1 (summary tokens count twice). A token
that appears in every op ("api", "get", "fixture") contributes exactly as much as a
token that appears in one op.

chub's BM25 (`cli/src/lib/bm25.js`) adds four levers our scorer lacks:

| Lever | What it does | Why it can change rank at our scale |
|---|---|---|
| **IDF** `log((N-df+0.5)/(df+0.5)+1)` | down-weights ubiquitous terms, up-weights discriminating ones | on an odds/fixtures API "odds"/"snapshot" appear in many ops; IDF stops them from swamping the one rare disambiguating term |
| **TF saturation** (k1=1.5) | 2nd occurrence of a term adds less than the 1st | prevents a verbose description that repeats a keyword from out-ranking a precise summary |
| **Length norm** (b=0.75) | penalizes long fields | a 200-word description no longer beats a 4-word summary just by having more tokens |
| **Field weights** (id 4, name 3, tags 2, desc 1) | tunable per-field priors | lets the ranker prefer the intent-bearing field |
| **Identifier tokenizer** | splits `nodefetch`→`node fetch`, camel/alnum boundaries | see 1c — a concrete, scale-independent recall gap we do have |

Two honest caveats that blunt a naive port:

1. **chub's field-weight prior is inverted relative to ours and would HURT us.**
   chub weights `id: 4.0` highest because a chub entry id is a curated
   `author/name` package slug — the most intent-bearing field. Our operation ids are
   *auto-generated OpenAPI garbage* (`getApiOddsSnapshotFixtureid`). We already
   double-weight **summary** (the intent-bearing field for an OpenAPI op) and treat
   the operationId as low-signal. Adopting chub's weights verbatim would promote the
   junk field. Any BM25 adoption must re-map field weights to the OpenAPI reality
   (summary/description/tags high, operationId low) — the *algorithm* ports, the
   *priors* do not.

2. **Below the surface-all gate, better rank ≠ better FCC.** See 1b.

### 1b. Does the ranking delta reach first-call-correctness? (measured)

No — not at our current scale. This is the load-bearing finding.

I ran `evaluate_golden` (the existing recall@k/MRR harness) against the real specs:

```
txodds : pool=18 usable | recall @1=0.58 @3=0.67 @5=0.67 @20=0.83  MRR=0.645  OOS_pass=1.00
pegana : pool=26 usable | recall @1=0.60 @3=0.60 @5=0.70 @20=0.80  MRR=0.631  OOS_pass=1.00
```

These are the **substrate** numbers (`search_scored`, the pure ranked list). They
look mediocre. But `scale.py` (`SURFACE_ALL_MAX_OPS = 50`) already decided that below
50 usable ops the agent-facing path (`client.search` / `list_tools`) shows **every**
usable tool with **no top-k truncation** — precisely so Gecko is never worse than the
raw OpenAPI dump on a small/clean API. `test_surface_all.py` asserts recall = 1.0
through that agent-facing path.

Consequence: at 18 / 26 ops the agent sees the right op **regardless of its substrate
rank**. A BM25 upgrade that moves txodds recall@3 from 0.67 → 0.85 changes the *order*
of a fully-visible list; it does **not** change which tool reaches the agent, so it
**cannot move FCC below the gate**. The substrate metric is real, but it is not the
metric that gates the product below 50 ops.

Where BM25 *does* start paying: above `SURFACE_ALL_MAX_OPS`, surface-all turns off,
top-k truncation returns, and substrate rank once again decides what the agent sees.
That is exactly the op-count threshold in our hybrid gate — and exactly where a
stronger lexical ranker earns its complexity.

### 1c. The one scale-independent recall gap chub's tokenizer would fix

Our tokenizer `re.compile(r"[a-z0-9]+")` does **not** split camelCase or letter/digit
boundaries, so a camelCase operationId collapses to a single mega-token:

```
"getApiOddsSnapshotFixtureid" → {"getapioddssnapshotfixtureid"}
query token "odds" ∈ that set?  →  False
```

chub's `tokenizeIdentifier` / `splitAlphaNumeric` / `compactIdentifier` would split it
into `get api odds snapshot fixtureid`, so the operationId field would contribute
recall for "odds". This is real and scale-independent — but its blast radius is
narrow: our haystack already indexes **summary, description, path, tags** separately,
and intent normally matches there, not on the operationId. The gluing only bites when
an op has a thin/empty summary and the operationId is the sole signal. It is worth
isolating as its own experimental variable (§4, arm B) because it is the *cheapest*
chub lever and the only one whose benefit does not depend on crossing the op-count
gate.

### 1d. Progressive disclosure

Out of scope here (context-engineer's spec). Note only the seam: chub's
disclosure is `search → get entry-point → get --full`. Our analogue is
`search_capabilities → tool-def (question-shaped, auth-hidden) → prepare/call`. The
retrieval slice hands off a *ranked op id*; disclosure decides how much of that op's
surface to materialize. Ranking quality (this doc) and disclosure depth (that doc) are
independent knobs and must be measured independently — do not let a disclosure change
confound a recall-@k reading.

---

## 2. The evidence-gated hybrid direction — does chub move it?

Established gates (memory: `context-engineering-anthropic`, `agent-native-surface-design`):
**lexical → hybrid only when `usable_ops > 50` AND `recall@3 < 0.8`.** Both must hold.

Where each gate condition stands, measured:

| Gate condition | txodds | pegana | Met? |
|---|---|---|---|
| `recall@3 < 0.8` (substrate) | 0.67 | 0.60 | **YES — already below 0.8** |
| `usable_ops > 50` | 18 | 26 | **NO** |

So the AND is **false** → no hybrid. The binding, un-met condition is op-count, not
recall. This is important: the low substrate recall is *masked* by surface-all below
50 ops, which is why it is not a fire alarm today.

How chub's design refines (not reverses) the gate:

- **chub reaffirms lexical-first.** chub is a shipping product operating at
  package-registry scale (hundreds to thousands of entries) on **pure BM25, no dense
  arm at all.** That is a strong external datapoint that well-tuned lexical carries
  much further than our current 18–41-op surfaces. It does *not* argue for pulling the
  hybrid trigger earlier; if anything it argues the lexical ceiling is higher than we
  assumed.
- **chub sharpens what the lexical half of a future hybrid should be.** Our
  CE-canon already says "keep BM25 as the lexical half of any hybrid." Today our
  lexical half is *overlap-count*, not BM25. So the refinement is a **sequencing**
  rule, not a new gate: when the op-count gate fires, the first move is to upgrade the
  lexical arm to BM25 (chub-shaped, OpenAPI-remapped field weights) — *then* measure
  whether dense/RRF (already built in `dense.py` / `fusion.py`, gated OFF) adds
  anything on top. We may find BM25-alone recovers recall@3 ≥ 0.8 above 50 ops and the
  dense arm stays shelved.

Gate, restated (refined, not changed):

> Stay on lexical **overlap-count** while `usable_ops ≤ 50` (surface-all decouples
> rank from FCC — a stronger ranker is metric-cosmetic here). When `usable_ops > 50`
> AND substrate `recall@3 < 0.8`: **step 1 = swap the lexical arm overlap→BM25**
> (OpenAPI-remapped weights); re-measure. **step 2, only if BM25 alone leaves
> recall@3 < 0.8:** enable the dense+RRF hybrid already built behind the seam.

At our current 18–41-op scale we **adopt nothing new into the live path yet.**

---

## 3. Why "measure, don't vibe" — what would falsify "adopting chub's shape lifts recall@k / FCC"

The claim under test: *"adopting chub's retrieval shape (BM25 scorer and/or
identifier tokenizer) lifts recall@k and/or FCC on our golden sets."*

If true, a BM25 variant of `CatalogEntry.score` beats the overlap baseline on the
frozen sets with a consistent, non-noise margin. If false (the null we expect at this
scale), it does not — and we keep the overlap scorer in the live path and shelf the
BM25 implementation for the op-count gate.

Because the golden sets are tiny (txodds 14, pegana 12, flywheel 5 = 31 tasks), one
flipped task ≈ 7–8% swing. The decision rule must therefore demand a *consistent*
lift across sets, never a single-task flicker.

---

## 4. Eval plan (the falsification experiment)

**Nature: offline, $0, recorded-mode against the real committed specs.** No live call,
no vectors, no new dependency. This is a *comparison harness*, not a product change —
it writes only metric metadata to `private/` (control-plane; invariant #1).

### Arms (each is an alternate `score()` behind a flag; the live path is untouched)

- **Arm 0 — baseline (control):** current overlap-count scorer. Already measured:
  txodds recall@3 0.67, pegana 0.60.
- **Arm A — BM25 (OpenAPI-remapped weights):** port chub's Okapi BM25 (k1=1.5, b=0.75,
  IDF, TF-sat, length-norm) over our existing haystack fields; field weights re-mapped
  to OpenAPI reality (summary/tags high, operationId low — NOT chub's id=4). Same
  tokenizer as today, to isolate the *scoring* lever.
- **Arm B — identifier tokenizer only:** keep the overlap scorer, swap only the
  tokenizer to chub's `splitAlphaNumeric` + camel/alnum split. Isolates the
  operationId-gluing fix (1c) from BM25.
- **Arm A+B — BM25 + identifier tokenizer:** the full chub retrieval shape.

### Metrics (reuse `gecko.evaluate.evaluate_golden`, unchanged)

Per arm, per golden set (txodds, pegana, flywheel): `recall@{1,3,5,20}`, `MRR`,
`OOS_pass_rate`. Then run the agent-in-the-loop `fcc_eval` (Haiku, recorded) on
txodds + pegana to confirm the FCC prediction from §1b.

### Pre-registered decision rule (write it down before running)

Adopt an arm into the **live path now** only if ALL hold:

1. `recall@3` improves by **≥ 0.10 absolute on ≥ 2 of 3** golden sets, AND
2. **no regression** on the third set (Δrecall@3 ≥ −0.02), AND
3. **`OOS_pass_rate` stays 1.00** on both scored sets (a stronger ranker must not
   manufacture confident false positives on out-of-scope intents — the confidence
   floor is lexical-anchored per `fusion.py`), AND
4. it moves **FCC** on `fcc_eval` by ≥ 1 task on either surface.

Condition 4 is the trap, and it is expected to **fail at current scale**: below the
surface-all gate the agent sees every tool, so no rank change can move FCC. That
failure *is* the finding.

### The null result we accept (and expect)

> Arm A/A+B lifts substrate recall@3 (plausibly 0.67 → ~0.8+ on txodds) — but FCC is
> flat because surface-all shows every tool below 50 ops. Condition 4 fails.
> **Verdict: do NOT adopt into the live path.** Keep the overlap scorer; keep the BM25
> arm as tested, shelved code that becomes the lexical arm when `usable_ops > 50`.

If Arm B alone shows a recall lift on a thin-summary surface at zero FCC cost, that is
a *separately* adoptable, low-risk tokenizer fix (it cannot introduce false positives,
only add recall) — flag it, but it is not required and not urgent below the gate.

Falsification of the "adopt now" claim = failing condition 4, which we predict. A
result that *passed* condition 4 at 18–26 ops would contradict the surface-all
analysis and would itself be the surprising, investigate-first outcome.

---

## 5. Decision record — top 3 for the founder

1. **BM25 is a real ranker upgrade over our overlap-count — but DON'T adopt it into
   the live path yet.** At 18–26 usable ops, `scale.py` surface-all shows the agent
   every tool, so substrate rank is decoupled from first-call-correct. BM25 would buy
   a nicer recall@3 number, not a better first call. Evidence gate holds: lexical
   overlap stays while `usable_ops ≤ 50`.

2. **Adopt BM25 as the pre-committed lexical arm the moment the op-count gate fires
   (>50 ops).** That is where surface-all turns off and rank drives FCC again. Refine
   the hybrid gate to a *sequence*: step 1 swap overlap→BM25 (OpenAPI-remapped field
   weights — NOT chub's id=4 prior, which is inverted for auto-generated operationIds);
   step 2 enable the already-built dense+RRF hybrid only if BM25 alone leaves
   recall@3 < 0.8. chub running pure-BM25 at registry scale is evidence the dense arm
   may stay shelved even then.

3. **Ship the offline falsification harness (§4) before touching the ranker.** Four
   arms (baseline / BM25 / id-tokenizer / both) × three golden sets, reusing
   `evaluate_golden` + `fcc_eval`, all recorded/$0. Pre-registered rule with an
   explicit **null result we accept** (recall up, FCC flat → don't adopt). One cheap
   low-risk exception worth isolating: chub's **identifier tokenizer** fixes a real
   camelCase-operationId gluing gap (`getApiOddsSnapshotFixtureid` → no "odds" match)
   and can only add recall, never false positives — adopt it standalone if arm B shows
   any lift, independent of the BM25 decision.

Seam note: progressive disclosure is the `context-engineer`'s parallel spec; this doc
does not touch disclosure depth, only ranking quality. They meet at the
`search_capabilities` → tool-def handoff and must be measured independently so a
disclosure change never confounds a recall reading.
