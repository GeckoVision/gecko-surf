# CI for agents — the harness component that does not exist yet

**Status:** design, written 2026-08-16 from measurements taken the same day.
**Scope:** an architecture, not an implementation plan. Every number below was observed.

---

## 1. The asymmetry this exists to close

LangChain's *Anatomy of an Agent Harness* puts it cleanly: **Agent = Model + Harness. If
you're not the model, you're the harness.** The harness is everything around the model —
system prompts, tools and their descriptions, bundled infrastructure, orchestration, hooks
— and its listed components exist so an agent can **verify its own work before declaring it
done**: *"Tools like browsers, logs, screenshots, and test runners give agents a way to
observe and analyze their work. This helps them create self-verification loops."*

Now compare the two kinds of agent we actually ship harnesses for:

| | coding agent | API / on-chain agent |
|---|---|---|
| workspace | filesystem | — |
| general tool | bash | — |
| safe place to try | sandbox | — |
| "did it work?" | test runner, logs, screenshots | — |
| undo | `git revert` | **none — the call IS the commit** |
| what it ships with | all of the above | a tool list |

A coding agent is allowed to be wrong. It writes, runs the tests, reads the failure, fixes
it, and only then says done. **Being wrong is cheap and reversible, which is what makes the
loop work at all.**

An API or on-chain agent gets a tool list. There is no sandbox where the call is free, no
test that says the tool works, and no rollback — money moved, the email sent, the tokens
transferred. Verification cannot come after, so it has to come *before*, and nothing in
today's harness provides it.

**That box — Observe & Verify, for actions that cannot be undone — is what this spec
builds.**

## 2. Why the surface's own word is not enough (measured, today)

Orquestra is the best case available: a well-built catalog of 4,400+ Solana programs, an
atomic API, a flow engine, honest framing in its own security model. We ran its surface
against the chain.

Every instruction reported its compute cost as the FIRST `consumed` line in the simulation
logs. In any transaction with a CPI the inner program returns first, so:

```
Program BUYux...    invoke [1]
Program Tokenkeg... consumed 105 of 173022     <- reported
Program BUYux...    consumed 36399 of 200000   <- charged
```

`simulate_instruction` and `estimate_flow` both answered **105** for a transaction mainnet
charges **36,399** for — 347x under, on the number an agent would size a compute budget
from, across every flow whose transaction touches SPL Token. The risk verdict was `high`
for all of them, because it counted the fee payer, which is a writable signer on every
transaction ever made.

None of this is a criticism of the build. The transaction was correct: right accounts,
fresh blockhash, clean simulation, and 36,399 in its own logs. **The composition was right
and the description of it was wrong**, and no amount of care inside the composer catches
that, because the composer is the thing being described.

We only saw it because we hold every prediction against what the chain charges — 20+
mainnet transactions, all exact. That is the entire mechanism, and it is not clever: it is
an outside measurement.

> Fixed upstream in `berkayoztunc/orquestra#9`, merged and deployed within the hour. The
> scorecard in §4 was re-run against the fix and shows 5/6 honest.

## 3. The four components

### 3.1 The Sandbox — where a call is free

Every tool in a catalog gets an environment in which calling it costs nothing and lands
nothing, with the same shape as the real call.

We already have both halves and they are the same code path by construction: `recorded`
mode synthesizes a response from the schema for HTTP surfaces, and surfpool gives a
mainnet fork for programs. What is missing is that they are ours, not the integrator's.

**The contract: the sandbox ships AS A TOOL, beside the real one.** An agent that mounts a
catalog gets `try_<tool>` next to `<tool>` — identical inputs, identical result shape, a
`sandbox: true` marker, and no possibility of spend. That is the "write code, run tests"
half of a coding harness, for actions.

This is what "the moment you integrate, everything is already built" means concretely: an
integrator does not stand up a fork, fund a wallet, or find test data. It is there.

### 3.2 The Probe — a runnable test per tool

For each tool, a small generated suite:

* **inputs that must work** — a canonical call that should build and simulate clean;
* **inputs that must REFUSE** — the wrong-shaped, the ambiguous, the out-of-scope. A tool
  that accepts everything has no contract;
* **the intents** — the same capability asked for in user vocabulary, to test whether an
  agent can *find* it;
* **the assertion** — not "it returned 200" but "the number it reported is the number the
  chain charged".

Today our golden sets are hand-written per surface and frozen by sha256. Generating them
per tool, at catalog scale, is the open work (§6).

The discipline that makes a probe worth anything is one we already enforce: **a guard is
only a guard if the assertion is unreachable when the property is false.** Every probe must
be mutation-tested — break the thing, confirm the probe goes red — or it is decoration.

### 3.3 The Score — what the probe measured

Per tool, five questions with measured answers:

| | question |
|---|---|
| **BUILD** | can a call be constructed from the surface alone? |
| **SIMULATE** | does it survive the chain's own judgement, unsigned and free? |
| **HONEST** | is the number the surface REPORTS the number the chain CHARGES? |
| **REACHABLE** | can an agent asking in user words find this tool? |
| **REFUSES** | does it refuse what it should, or does it guess? |

REACHABLE is the one everybody skips and it is where we have the sharpest evidence: lexical
retrieval scores **0.00** on paraphrase intents across four independent APIs. A tool can
build perfectly and be invisible to the agent that needs it. A score that omits retrieval
is measuring a tool nobody will call.

REFUSES is a capability claim, not a nicety. "It refuses a purchase that would pay the buyer
back" is a stronger statement about a surface than "it returns 200".

### 3.4 The Watch — rerun on drift

A score is a photograph. Surfaces move: an IDL is upgraded, a spec is edited, a store's menu
changes, a program is redeployed.

Orquestra's own goal G7 is *"IDL change → affected flows re-verified or **quarantined**
within 1 hour."* Quarantine is detection with a graceful failure. A score says **what**
broke and **which** of the five questions now answers differently — which is the difference
between "something changed" and "your swap flow now reports a cost it will not charge".

## 4. What a run looks like — real output, one catalog entry

`let_me_buy`, every instruction, against mainnet, nothing signed:

```
instruction                build  sim    reported   actual  honest  risk
──────────────────────────────────────────────────────────────────────────
make_purchase              True   True      36399    36399  True    high
mark_as_delivered          True   True      34858    34858  True    medium
update_details             True   True      25476    25476  True    medium
update_telegram_channel    True   True      25473    25473  True    medium
add_product                True   True      27039    27039  True    medium
delete_product             False  False       None     None  False   None
                           └─ Error: Missing required accounts: system_program

  builds+simulates : 5/6
  reports honestly : 5/6      (0/6 before #9)
```

Two findings from one run of one program: a reporting defect across the whole catalog, and
`delete_product` uncallable from the surface as published. Neither is visible by reading the
IDL, the docs, or the code. Both are obvious the moment something outside the surface asks
it to prove itself.

**Multiply by 4,400.** Orquestra says, honestly, "can construct valid transactions for the
vast majority of them (full coverage testing is still ongoing)." That sentence is where this
product lives.

## 5. Why the score cannot be built by the party being scored

This is the structural claim, and it is the one that makes the component durable rather
than a feature someone absorbs.

* **The catalog cannot grade itself.** Its simulation is computed FROM its own bytes;
  asking whether it attests them is asking whether a value equals itself. Today's evidence
  is not hypothetical: six instructions reported a wrong number, and one hour after an
  outside measurement the fix was merged and deployed.
* **The signer cannot grade the action.** Custody protects the KEY, never the ACTION. No
  custody provider on the market can express *"sign only if this hash matches a simulation
  someone else performed."*
* **The agent cannot grade itself.** It is the thing under test.

Every participant is disqualified by their own position. That is not a gap somebody closes
by trying harder; it needs a party whose only job is the verdict.

**And it is the lesson of the piece we already lost.** We hardened a signer-MCP; a vendor
shipped a product-shaped signer and it was designed out — correctly. A *piece* gets
replaced. A *score* cannot be produced by the thing being scored.

## 6. What we have, and what is missing

| component | status |
|---|---|
| Sandbox — recorded mode (HTTP) | **have** — one code path with live, differs only at the transport edge |
| Sandbox — surfpool fork (chain) | **have** — proven, $0, used for the five-scenario demo |
| Sandbox packaged AS A TOOL (`try_*`) | **missing** — the integrator still builds their own |
| Probe — golden sets, frozen, archetype-split | **have, hand-written** — 4 surfaces |
| Probe — generated per tool at catalog scale | **missing** — the central open problem |
| Probe — mutation discipline | **have** as practice, not as infrastructure |
| Score — BUILD / SIMULATE / HONEST | **have**, demonstrated §4 |
| Score — REACHABLE | **have** the measurement, not wired into a per-tool score |
| Score — REFUSES | **have** the refusals; no coverage metric over them |
| Watch — drift series | **have** the N-confirmed drift machinery for programs |
| Watch — as a hosted, scheduled service | **missing** |
| Catalog-scale ingestion | **missing** — we run one surface at a time |

The honest summary: **we have the instruments and none of the factory.** Everything in §4
was produced by hand today. The product is that run, unattended, per tool, on every change.

## 7. Open questions, to be answered before building

1. **Does a provider want a score they did not ask for?** A catalog's incentive is reach —
   "4,400 programs, the vast majority work." A score converts that into a number that may
   be lower. Whether that reads as help or as an audit is not knowable from here, and #9 is
   a cheap early signal.
2. **Who pays?** Repo canon is that developers never pay and revenue is provider-side, flat
   per API. A per-call verification meter is the only API-world unit that grows on its own —
   and charging for it contradicts that canon directly. This gets decided, not argued.
3. **Does REACHABLE belong in a provider's score?** It measures the agent's retrieval as
   much as the surface. A provider may reasonably say it is not their defect.
4. **What is the honest floor for a generated probe?** A hand-written golden set carries
   human judgement about what SHOULD refuse. A generated one may only test what the schema
   already says, which is the weaker half.
5. **Does the sandbox tool double the surface?** `try_*` beside every tool doubles the tool
   count an agent must search — and retrieval at ~200 tools already degrades badly. It may
   need to be a mode on the existing tool rather than a second one.

## 8. First step

Not the factory. **One catalog entry, unattended.** Take `let_me_buy`, generate the probes
rather than hand-writing them, run all five questions per instruction on a schedule, and
publish the score. If that holds for six instructions without a human, the same loop is
what runs for 4,400.

The measurement in §4 already exists. What it lacks is a machine that takes the surface as
input and produces it without us.
