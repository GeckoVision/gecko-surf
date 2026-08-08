# Entity-named intent: a measured retrieval limit, not a bug

**Status:** investigated, **no code change recommended**
**Found:** post-deploy probe of the hosted Pegana surface, 2026-08-08
**Lane:** retrieval quality (`ai-ml-engineer`) crossing into value domains (`graph-engineer`)

## The observation

For the question we publish everywhere — in `llms.txt`, on `gecko-101`, in the README —
the tool the docs then tell the agent to call ranks **fourth**:

```
search_capabilities(query="is USDC pegged right now")

1. peg_feed          score 3    all 68 assets, no parameters
2. peg_feed_by_mint  score 3
3. state_by_mint     score 2
4. state             score 2    ← the one the docs call
```

## Why

Every candidate matches on **exactly one** query token: `peg`.

```
query tokens after fold + stopword drop:  ['now', 'peg', 'right', 'usdc']

peg_feed   hay ['peg']  summary ['peg']  op_id ['peg']   = 3
state      hay ['peg']  summary ['peg']  op_id []        = 2
```

The ranking is decided entirely by the operationId double-count — by whether the word
"peg" happens to appear in the operation's *identifier*. Nothing about the question
separates them.

**The discriminating token is invisible.** `usdc` appears in **no operation's scored
surface** on this API. It is a parameter *value*, not surface vocabulary, and the
haystack is `summary + description + path + tags + operationId + blurb`. The one token
that says "I want a single named asset" contributes zero. `now` and `right` are
temporal filler that no operation's text contains either.

## It is not a correctness failure

`peg_feed` **answers the question.** Verified against the live surface: it returns 68
assets including USDC with its full peg record. An agent that calls the top-ranked tool
gets a correct answer on the first call.

The cost is precision, not correctness — 68 assets where 1 would do. Real, worth
recording, and much smaller than "our flagship example misroutes."

## What was measured

Against the **frozen** 12-task Pegana golden set (`sha256`-pinned so intents cannot be
edited after seeing results), plus the live query:

| variant | top-1 | the live query |
|---|---|---|
| baseline (what ships) | 6/11 | `peg_feed`, `peg_feed_by_mint`, `state_by_mint` |
| **A** — parameter names folded into the haystack | 6/11 | **unchanged** |
| **D** — the pinned blurbs in `tests/fixtures/golden/blurbs/pegana.json` | 6/11 | **unchanged** |

**Both candidate fixes measured to zero effect.** A is inert because the query contains
no `symbol` token. D is inert because `peg_feed`'s blurb carries the same `peg`
vocabulary as `state`'s — both rise together and the gap is preserved.

### A contaminated measurement, recorded on purpose

A third variant — a hand-written blurb on `state` — *did* fix it, moving top-1 to 7/11
and putting `state` first. It is discarded, because the blurb was authored **after**
seeing the query and contained the query's own literal tokens (`"...pegged or depegged
right now"`). It measured nothing except that copying a question into a document makes
that document match the question.

This is the same circularity named in `docs/ghostcommit-safeguard.md` about the attack
corpus. It is easy to walk into while trying to fix something.

## Why the golden set did not catch this

The frozen set has a `near_dup_disambiguation` case that **passes**:

> "get the current peg **state** for this asset" → `state` ✓

It passes because the intent echoes the operation's own vocabulary — it hands the
scorer the discriminating token. The real user question does not: people ask "is USDC
pegged", not "get the current peg state". **The disambiguation archetype is a keyword
echo in disguise**, and the set contains no case where the query names a concrete
entity whose value appears nowhere in the surface.

Adding this query to the golden set was considered and rejected: with `peg_feed` a
correct answer, honest `expect_ops` would have to include it, and the case would pass
trivially while measuring nothing.

## What would actually separate them

Not text similarity. A semantic tier does not obviously help — "is USDC pegged" is
close to *both* operations, because both are about the peg state of assets.

What separates them is knowing that `USDC` is an **entity value** and that `state`
*accepts* that entity (`symbol`, path, required) while `peg_feed` takes no parameters
at all. That is value-domain reasoning over the graph, not lexical or dense retrieval —
the `solana-token-mint`-style machinery applied to an intent rather than to a join.

It is also where the false-positive risk lives. Treating any short uppercase token as a
symbol would misfire constantly, and the standing rule from the value-domain work is
that **a wrong join is worse than a missing one**.

## Recommendation

1. **Change nothing in the scorer now.** One query, two inert candidate fixes, and a
   top-1 result that is correct.
2. **Keep it as a measured negative** feeding the semantic-tier evidence gate — with
   the caveat above that a dense tier is not obviously the answer.
3. **Open question for the graph lane:** should an intent token that classifies as a
   value domain lift operations that accept that domain as a parameter? That is a real
   design question with a real FP cost, and it belongs to `graph-engineer` /
   `token-engineer`, not to a scoring tweak.
4. **Reproduce with:** `tests/fixtures/pegana_openapi.json` + the frozen golden set. All
   numbers here are offline and $0.
