# What their MCP can do, what it cannot, and which half is ours

**Status:** measured 2026-08-17 against the two live MCP servers and `orquestra@c9dd085`.
**Companion:** `2026-08-16-ingestion-graph-surface.md` (the architecture) and
`2026-08-16-ci-for-agents.md` (why a score comes from outside).

---

## 1. The sorting rule

Every gap below is sorted by one question, and it is not "who is better at it":

> **Is this an evolution of their product, or a missing piece?**

An *evolution* makes the existing thing better along the axis it already has — faster
discovery, stricter publishing, a richer language. It belongs to whoever owns that axis,
and for the Flow Engine that is Orquestra. Building it ourselves would be forking a
roadmap.

A *missing piece* is a different job that the existing thing structurally cannot do from
where it stands — usually because it would have to be its own witness. Those are ours,
and they stay ours no matter how good their product gets.

The rule is not a negotiation. Applied honestly it hands most of the visible defects
back, and keeps a smaller set that nobody else is positioned to build.

## 2. Two servers, eighteen tools

The Flow Engine's buyer surface is a **second MCP server**, separate from the atomic one.
Both return an empty `instructions` string on `initialize`.

| server | tools |
|---|---|
| `https://api.orquestra.dev/mcp` | `search_programs`, `list_instructions`, `list_pda_accounts`, `derive_pda`, `build_instruction`, `simulate_instruction`, `simulate_transaction`, `fetch_pda_data`, `get_program_data`, `read_llms_txt`, `get_ai_analysis` |
| `https://api.orquestra.dev/flow/mcp` | `list_flows`, `get_flow_metadata`, `estimate_flow`, `get_flow_schema`, `validate_flow`, `simulate_flow`, `publish_flow` |

## 3. What is possible today

All verified by calling them.

| an agent can | tool | notes |
|---|---|---|
| find a program by an exact keyword | `search_programs` | `buy` → Let Me Buy, `mining` → Ore |
| read instructions, args, accounts | `list_instructions` | |
| read PDA-derivable accounts + seed schemas | `list_pda_accounts` | |
| derive a PDA | `derive_pda` | |
| build an unsigned instruction | `build_instruction` | |
| simulate an instruction | `simulate_instruction` | returns CU + a risk level |
| simulate raw wire bytes | `simulate_transaction` | `sigVerify: false` throughout |
| read and IDL-decode an account | `fetch_pda_data` | |
| scan program accounts | `get_program_data` | requires a filter |
| read generated docs / analysis | `read_llms_txt`, `get_ai_analysis` | |
| browse and read published flows | `list_flows`, `get_flow_metadata` | 24 published, all `instruction` tier |
| run a published flow, unsigned | `estimate_flow` | **accepts an `rpcUrl` override** |
| read the FDL grammar + node catalog | `get_flow_schema` | *"This server does NOT generate flows"* |
| statically validate a draft flow | `validate_flow` | no RPC, no writes |
| compile **and run** a draft flow | `simulate_flow` | **accepts an `rpcUrl` override** |
| publish a proven flow | `publish_flow` | requires `INGEST_API_KEY` — not open |

**The `rpcUrl` override is the most important row in this table** and §6 is built on it.

## 4. What is not possible, and where each half goes

| an agent cannot | why | evolution (theirs) | missing piece (ours) |
|---|---|---|---|
| find a capability from natural language | `search.ts:77` ANDs every token, no stopwords — 5 of 5 phrases return "No programs found" | **fix the operator** — weighted OR + minimum match | **the intent layer** — BM25 over names/tags still cannot reach *"charge my customers every month"* → Subscriptions. We measure 0.00 on paraphrase across four APIs |
| get a real test account of a given type | `find_real_account` is authoring-only (`flow-author-agent.ts:85`) | **expose it** — it is their fixture supply | — |
| know a built call is *correct*, not merely accepted | a derived address is whatever the seeds say; from inside the flow there is no second opinion | — | **the independent derivation.** Not an evolution of a composer — the other party |
| know what a transaction *moves* | no `preBalances`/`postBalances`/`preTokenBalances` anywhere in the worker; risk = mutability flags + a name regex | — | **deltas, caps, destination control.** They non-goal custody; this is the verdict layer |
| trust an error code | `StoreNotEmpty` is in `let_me_buy`'s IDL error table and **raised nowhere in the program** — `delete_store` simulated success at 16,893 CU on a store with two products and 20 receipts | — | **declared-vs-raised.** Requires reading the source; an IDL-derived surface has no input that could show it |
| recover a seed the IDL dropped | `resolve.pda@1` receives raw seeds with no IDL in scope | — | **seed recovery from source** (the #4057 rescue) |
| try a call for free | hosted server → mainnet; a fork on `127.0.0.1` is unreachable from their edge | — | **the $0 fork.** A different infrastructure category from an isolate |
| undo | the call is the commit | — | — (nobody can; it is why verification moves *before*) |
| know a flow does what its metadata says | `compiler.ts:238`: *"Output-wiring validation is deliberately out of scope"* | **check outputs against the graph** | **semantic fidelity** — does the graph do what `meta` claims |
| author a flow | *"This server does NOT generate flows"* | **their authoring agent**, when they choose to expose it | — |
| loop, or call another flow | no `map.over@1`, no `flow.call@1` | **both node types** | — |
| know a call needs another call first | no prerequisite relation exists in FDL | **the field** (additive) | **the relation** — which read populates which input, from the graph |
| see a score | none exists | — | **the score.** Structurally not producible by the party being scored |

**Theirs, collected:** the search operator, exposing `find_real_account`, output-wiring
validation, `map.over` / `flow.call`, the lints on the publish path, node output schemas,
metadata hygiene, a lifecycle that transitions, and three additive FDL fields.

**Ours, collected:** the independent derivation, what a transaction moves, declared-vs-raised
error codes, seed recovery from source, the $0 fork, the intent layer, prerequisites as a
graph relation, and the score. **Evaluation and CI for agents is the name for all of it.**

Note the shape of the first row: one problem, split down the middle. Fixing the operator is
theirs and it takes recall from zero. Crossing the paraphrase gap is ours and no operator
fix reaches it. Most rows in this table are that shape.

## 5. The measurement behind "ours"

Not an argument — a run. 60 programs pulled from their catalog, our graph built over each:

```
  programs sampled       : 60
  graph built            : 55        4 native (no IDL) · 1 crash
  instructions covered   : 1,380
  PDA accounts           : 1,948
  of those UNRESOLVABLE  : 123  (6% — each needs a human today)
  programs fully clean   : 14 / 27 with any PDA
```

Half of PDA-bearing programs need no human at all. The failures concentrate — 59% of the
123 sit in three programs. Two named blockers, both actionable:

* **Our parser crashes on legacy Anchor IDLs.** Bonkswap dies at `gecko/pda_extract.py:428`
  with `TypeError: string argument without an encoding`, because a pre-0.30 const seed is
  `{"kind":"const","type":"string","value":"bonkswapstatev1"}` rather than 0.30's byte
  array — 52 of that program's 109 seeds. One IDL in 27 here, but this sample is their
  **top 60 by usage** and the 4,400-program tail skews older. Ours to fix.
* **Native programs have no IDL at all.** SPL Token, Associated Token Account, Token-2022,
  Address Lookup Table — the four most-called programs on Solana, outside the graph
  entirely. A decision, not a patch.

## 6. The loop this makes possible, end to end

The `rpcUrl` override plus a local bun bridge into their real `compile()` and `run()` means
**their engine will execute against an RPC we choose.** Proven today:

```
starting surfpool fork of mainnet …
fork up at http://127.0.0.1:8899
ran ok: True | rpc_calls: 2
simulation success : True
compute units      : 42494          ← mainnet charges 36,399 for the same call
receipts PDA       : H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V
```

Their FDL, their compiler, their interpreter, their resolvers — our fork as the RPC, our
graph as the judge, $0, unlimited, nothing landing. That is the harness a coding agent has
and an on-chain agent does not.

```
  1. author        agent writes FDL from get_flow_schema          THEIRS
  2. validate      validate_flow — structure, refs, cycles        THEIRS
  3. run           their engine, our fork via rpcUrl              COMPOSED
  4. judge         our graph: address, deltas, provenance, gaps   OURS
  5. score         BUILD · SIMULATE · HONEST · REACHABLE · REFUSES OURS
  6. publish       publish_flow, only what scored                 THEIRS
```

Steps 1, 2 and 6 are already theirs and already work. Step 3 works because they accepted an
`rpcUrl` parameter. Steps 4 and 5 are the missing piece, and they are the whole product.

**And the honest caveat, which belongs in the score rather than the pitch: a fork is not
mainnet.** The same call costs 42,494 CU on the fork and 36,399 on mainnet. A fork is a
*behavioural* sandbox — it will tell you an account was wrong, a seed was wrong, a signer
was missing — and it is **not** a compute oracle. Any score that reports a fork's compute
as mainnet's is doing the thing we caught someone else doing.

## 6a. Hosting the fork — the part that makes it a product

A fork on `127.0.0.1` is a script. A fork on a public URL is an endpoint, and their worker
will call it. Measured today, `simulate_flow` with `rpcUrl` set:

| target | result | what it proves |
|---|---|---|
| default | run succeeded, real blockhash | baseline |
| `api.devnet.solana.com` | `403 Forbidden — your IP or provider is blocked` | **they reached a host we named**; devnet refused their egress IP |
| an unroutable host | Cloudflare `530 / 1016` | they attempted the connection |

**There is no allowlist on `rpcUrl`.** That gives two distribution paths:

**(a) Passive — zero integration by anyone.** A builder keeps using Orquestra's own MCP and
passes `rpcUrl: https://fork.<ours>`. Their agent, their flow, their engine, our fork. They
do not install us, and they get a free sandbox by typing one parameter.

**(b) Active — they point at our MCP.** `try_<instruction>` beside the real tool, the whole
loop behind it: author → validate → run on the fork → judge against the graph → score.
Same inputs, same result shape, `sandbox: true`, no possibility of spend.

### What hosting actually costs, honestly

Two tiers, and only one of them is cheap:

| tier | what it serves | isolation | cost |
|---|---|---|---|
| **stateless** | `simulateTransaction` only — every call a read against the fork's snapshot | **shared**; simulation does not mutate state, so one process serves everybody | one process + lazily-fetched mainnet accounts |
| **stateful** | sent transactions, so a *sequence* can be tested — buy a water, then mark it delivered | **per session**; one user's send is visible to the next | a process per session, torn down after |

The round trip needs only the stateless tier, and that is what ships first.
`--host 0.0.0.0` plus `--block-production-mode transaction` (no idle slot churn) is the
shape. **A method allowlist in front is not optional**: surfpool exposes a full RPC
including `sendTransaction`, and an open one is somebody else's validator to abuse.

Two honesty constraints ride along. A shared fork **ages** — it holds the mainnet state of
whenever it was started, so its snapshot time is a fact the score has to state, not a
detail. And §6's caveat stands: **a fork is not a compute oracle.**

### The finding this rests on is also a finding we owe them

Their own design says `external.http` targets an allowlist registry and *"SSRF is excluded
by construction, not by filtering."* `rpcUrl` is a caller-supplied URL their worker fetches,
and it is not covered by that posture. On a Worker the blast radius is modest — no VPC to
pivot into, and the response must parse as JSON-RPC — but it contradicts a stated invariant,
and the reproduction is two lines.

**And it cuts against us, which is exactly why it goes in writing now.** If they close it
with a strict allowlist, path (a) closes with it. The honest move is to report it *and*
propose that the allowlist be registrable, so a builder can nominate a trusted fork —
which is better for them than a blanket URL parameter and keeps the sandbox reachable. What
we do not do is sit on a security finding because it is load-bearing for a channel of ours.

## 7. What this changes about the build

The round trip already proves steps 3 and 4 on one program. What turns it into something a
builder points at:

1. **The fork as a tool, not a script.** `try_<instruction>` beside the real one, same
   inputs, same result shape, `sandbox: true`, no possibility of spend.
2. **The score as declarative shapes**, so a provider can read and dispute it rather than
   take our word.
3. **Ingestion for an arbitrary program** — the 60-program run is a measurement harness,
   not a product. The two blockers in §5 gate it.
4. **The kit** — one page a provider hands their developers: the graph, the score, runnable
   FDL, and a sandbox that already works.

## 8. What goes to them, as one PR rather than eight

Every Track-A item stands alone, which is exactly why they should not arrive as eight
separate patches. One PR, one reproduction each, in the order they cost him least:

`search.ts` operator · lints on the publish path · output wiring · node output schemas ·
metadata hygiene at publish.

The three additive FDL fields (`provenance` on a node, `reason` on an input, `prerequisite`)
go separately, because they are a proposal and not a fix, and because each one only earns
its place once we can populate it from the graph for more than one program.
