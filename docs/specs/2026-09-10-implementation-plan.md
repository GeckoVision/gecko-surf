# The next four, and how each one starts

Founder directive, 2026-09-10: *"we are not using graphs in our favor — and we can take fast
decisions with less effort."* It is not a preference. The graph is how he found that TxLine
was the only surface appearing in our numbers, and that an external agent cannot use the
call graph we build because it is hidden. Two findings neither session got to by reading
code.

## The method: every task opens with a subgraph

Before implementing or reviewing anything below, cut a **task subgraph** from
`graphify-out/graph.json` with `scripts/task_subgraph.py`.

`graphify-out/` is gitignored (`.gitignore:54`) and the slices are large — the 0a slice is
684 KB — so the artifact stays local and **the command goes in the PR body**. That is the
right way round: the slice is regenerable from the command, and a command is reviewable
where a 684 KB JSON blob is not.

Two rules, both learned the hard way today:

1. **A plain-words query returns a neighbourhood, not a mechanism.** *"Which modules build
   or enumerate the MCP tool list an agent sees"* returned 420 nodes and truncated on the
   token budget. The peer's independent attempt routed through `AgentApiClient` at 189
   edges and read the same way. Seed from named symbols and expand deliberately.
2. **Record the selection rule beside the slice.** A subgraph whose seed set and traversal
   rule are undocumented is a claim nobody can challenge — the same failure as the August
   probe's `with_fallback` column. `{nodes, links}` plus the rule that produced them, or it
   does not ship.

The subgraph is not decoration. Its job is to answer, before code is written: *what already
does this, what will this change reach, and what reads it that I have not thought about.*

---

## 0a — Orquestra through the existing arms, unlabelled

**Why first.** `scripts/retrieval_arms_eval.py` contains **0** occurrences of `orquestra`,
`program`, `solana` or `find_start`. Its sets are txodds, pegana, privy, birdeye. The
harness whose gate decides whether we adopt a retriever cannot see the Solana surface at
all. Every retrieval number we hold — including two I quoted as general and have corrected
to showcase-only — describes four HTTP surfaces.

There are **two** functions named `evaluate_golden`: `gecko/evaluate.py:157` (showcase; 7
`archetype`, 4 `is_fallback`) and `gecko/retrieval_eval.py:352` (Orquestra's 52 rows; **0
of each**).

**Scope.** Run program cards through the arms that exist, with the metrics that exist,
reported per set. No new labels, no new metric. An honest *"here is Orquestra under the same
arms"* number nobody could have tuned.

**Done when:** `retrieval_arms_eval.py` reports an Orquestra row beside the four, and the
dense/RRF gate condition is evaluated against it rather than assumed from birdeye.

## 0b — archetype labels, separately and reviewed

**Not part of 0a, and the reason is sharp.** `paraphrase_no_overlap` is CI-enforced to share
zero tokens with its target (`tests/test_golden_set.py:90`). Applying that label is not
describing a row — it is **choosing what the lexical arm is guaranteed to fail**, on the
surface whose numbers will then justify buying a retriever. That is #528's symbol-adding one
level up, except the eval learns its answers from the party who wants the answer.

Same trust class as the value-domain review in #529: an affirmative, reviewed, per-row claim,
argued on its own merits and never shipped as harness plumbing.

## 1 — the reachability gate

**Five instances in one week** of a capability that works and cannot be found: the dense arm
built and unwired; `get_surface_graph` served but absent from `list_tools` (verified —
`search_capabilities`, `get_capability` and `query_docs` are enumerated, it is not);
`is_fallback` measuring while masking; unlisted surfaces (#515, fixed as an incident); and
Orquestra missing from the harness above.

**The rule is not "everything must be enumerated"** — that would undo the clutter #515
deliberately cleaned. Three of the five were the same defensible trade for the same reason,
a budget: a whole-graph dump on Stripe is ~337 edges; the catalog would be cluttered; an
embedding call on the hot path costs latency and a dependency.

**The rule: anything not enumerated must declare that it is hidden, and why, somewhere a
test can read.** Hidden on purpose passes. Hidden by omission fails. Today they are
indistinguishable, and that is the defect.

`gecko/endpoint_score.py` `findable` (#527) already answers *"handed this thing's own words,
can it be found"* for program cards. The gate extends it to enumeration, and to MCP tools
rather than cards.

## 2 — Raydium CLMM

Last, and it now walks into a repo that can tell it what is missing: R1–R6 in the ingest
gate, `check_seed_endianness` to flag the big-endian `amm_config` before it ships a wrong
address (**8/8 big-endian on chain, 1/8 little — and that one is index 0, where both
encodings are the same two zero bytes**), `endpoint_score` for each of its 38 instructions,
and a `value_domains` slot awaiting its reviewed claim.

---

## What this plan will not do

- **Quote a retrieval number without its scope.** "BM25 is a net loss" and "the dense gate
  fires on birdeye" are showcase-only. Said plainly because I stated both as general and a
  peer published them before either of us asked what they were measured over.
- **Add token symbols as card terms.** Measured in #528: it would inflate recall on 11 of 52
  golden rows by teaching the eval its answers and push the floor down on 2.
- **Adopt FAISS or HNSW.** Index structures that trade recall for speed at millions of
  vectors. Our wired catalog is 21 cards; the full one is 4,534 programs. We lack a semantic
  arm, not a faster index.

Written argument for all of it: `docs/specs/2026-09-10-retrieval-review.md` (#530).
