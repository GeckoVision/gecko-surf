# What to build for Orquestra, from reading their codebase

**Status:** plan, 2026-08-18. Built from a graph of `packages/worker/src` (1,021 nodes,
2,728 edges, 57 communities) plus live measurement against their catalogue. Every claim
below is either a code citation or a number we took ourselves.

---

## 1. What they are building, from what they commit

The last twenty commits are not ambiguous:

| theme | commits | examples |
|---|---|---|
| identity / enrichment | 5 | Helius program identity, icon URLs, categorisation backfill |
| Telegram admin | 3 | control panel, sync reports |
| frontend / analytics | 3 | hero stats, program logos |
| security | 2 | SSRF allowlist, IDL visibility |
| RPC reliability | 2 | failover, backfill timeout |

And one that says it alone:

> `fix(analytics): home hero used wrong IDL count (796 pipeline-coverage vs 4,495 real catalog)`

**They are working on catalogue CREDIBILITY.** Logos, categories, verified identity, a
bigger accurate count. The program page carries "No registered developer — auto-imported
from Solana on-chain data" beside a **Request Developer** button. The problem being solved
is *"this must not look like an empty scraped directory."*

That is the frame anything we build has to fit. Not "here is a gap you have" — **"here is
the axis of credibility you are not covering, measured."**

## 2. What they already have — and it is more than we assumed

Reading the code rather than guessing:

* **A real audit trail for flows.** `services/flow-runs.ts` writes `id, version_hash, lane,
  status, inputs_hash, outputs_json, error_json, latency_ms, rpc_calls, external_calls`.
  That is a better run record than we have.
* **Usage analytics.** `services/analytics.ts` aggregates by date, by `tool_id`, by
  `project_id`.
* **A risk heuristic on every built instruction.** `tx-builder.ts`: `high` when a writable
  signer is mutated or transfer-shaped args are present.
* **Simulation that decodes the Anchor error.** `simulate_instruction` runs with
  `sigVerify=false` and maps the on-chain log back to the IDL's `errors[]` table — richer
  than our `{err, compute_units}`.

**Anything we propose that they already have is noise.** Simulation, run history and usage
counts are all theirs, and in two of three cases better than ours.

## 3. The three things the code does not do

**a. No agent identity.** `routes/mcp.ts:1213` — *"stateless mode: no session headers, no
auth requirement."* Every call across 4,511 programs is anonymous. They can count calls;
they cannot attribute them, rate them, or tell one agent from a crawler. Their own
adoption reading has the same shape ours does: most "clients" are indexers.

**b. No outcome, only volume.** `flow_runs` records the status of a flow *they* authored.
An agent calling `build_instruction` directly leaves no record of whether the transaction
was correct, landed, or was ever signed. The catalogue knows how often a tool was called
and never whether it worked.

**c. No content check on IDL text.** The only escaping in the codebase is
`services/search.ts:escapeToken` — FTS query escaping, not content sanitisation. IDL
`description` and doc strings reach the agent verbatim.

## 4. What we measured, including the result that does not help us

Two numbers, both taken this week against their live surface, both arms reading the IDL
*they* serve:

| | typed seeds | derivable accounts |
|---|---|---|
| their surface | 49/734 — 6.7% | 7/784 — **0.9%** |
| with our graph | 706/734 — 96.2% | 386/784 — 49.2% |

And the one that argues against our favourite pitch:

> **Anti-poisoning scan: 20 programs, 17,968 strings, ZERO hits.**

Their catalogue is clean. We cannot tell them they have a poisoning problem, because
measurably they do not. What is true is narrower and still worth saying: the catalogue
**auto-imports IDLs from an open source**, anyone can deploy a program carrying any text
in its `description`, and nothing between that text and the agent looks at it. That is a
structural exposure with a current value of zero — a monitor, not an incident.

## 5. What to build, in one sentence each

Ordered by how well it fits what they are already doing.

**① Agent-readiness score, per program.** The same shape as the logos and categories they
are shipping now, on the axis nobody covers: of this program's accounts, how many can an
agent derive without guessing; of its instructions, how many can it complete. We have the
harness (`scripts/partner_delta.py`, `gecko/score.py`) and the numbers above.
*Why it cannot come from them:* a score produced by the party being scored is not a score.

**② Outcome telemetry, not volume.** They log flow runs; the missing row is "an agent
called `build_instruction` for program X and the result did / did not land." A minimal
outcome record beside the existing `flow_runs` table, using their schema, turns 4,511
programs from a count into a ranked list of what to fix.
*Why it fits:* it is one table next to a table they already wrote.

**③ Content watch on ingest.** Run the sanitiser at auto-import, store a verdict per IDL
version. Today every verdict is clean, and saying so publicly is worth more to a catalogue
fighting the "scraped directory" perception than any warning would be.
*Why it cannot come from them:* same reason as ①, plus it is our only genuinely
non-obvious code.

## 6. How to present it

Not "you are missing X." Their own pattern — they merged our PR #9 on compute units and
have #11 open — is that a working patch gets read and an opinion does not.

So: **build ①, run it over their catalogue, and send the ranked list with the method
attached.** The conversation is then about their number, not our product. If the number
is bad, it is the most useful thing anyone has given them this month. If it is good, we
have learned that agent-usability is not the bottleneck and should stop claiming it is.

## 7. What this plan does not establish

* **Nobody asked for any of it.** Same status as everything else we have built this month.
* **Nothing here is technically exclusive.** They could build ① ② ③ in a sprint each. The
  only non-replicable part is *who* produces the number, which is a position, not a moat.
* **Our own read layer is still missing.** We cannot answer "is the sale open" either, and
  ② is where that gap would first hurt us.
* **The 0.9% closes as our upstream PR merges.** The durable claim is the class of defect,
  not this instance.
