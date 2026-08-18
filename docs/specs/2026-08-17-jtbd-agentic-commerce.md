# The job, in the agentic-commerce era

**Status:** research + direction, 2026-08-17. External sources cited inline; internal numbers
are this session's measurements.
**Companions:** `2026-08-17-mcp-capability-split.md`, `2026-08-16-ci-for-agents.md`.

---

## 1. What actually changed

Agentic commerce is usually described as a new *buyer*. The operationally important change is
narrower: **the buyer no longer reads.**

A human integrator reads the docs, makes a call, notices the number is odd, and asks. That
loop is how every wrong API description in history has been absorbed — quietly, by a person,
before it mattered. An agent has no such loop. **The surface's self-description IS the
interface**, taken as ground truth, and acted on.

Three external facts frame the stakes:

* x402 processed roughly **165 million agent transactions** in its first months, and card
  rails fail structurally at machine scale — interchange compresses at sub-cent tickets,
  chargebacks assume a human dispute window, settlement runs on banking hours
  ([Bitontree](https://www.bitontree.com/agentic-commerce-ai-agents-payments-ap2-x402),
  [Eco](https://eco.com/support/en/articles/14839400-what-is-agentic-commerce-the-2026-guide)).
* Static fraud rules collapse: legitimate agents look like compromised accounts — rapid
  sequential orders, unusual velocity — so valid transactions get blocked
  ([Signature Payments](https://signaturepayments.com/agentic-commerce-in-2026/)).
* **When an agent transaction goes wrong, who bears responsibility is unresolved** — merchant,
  consumer, agent platform, or model provider
  ([PromptHalo](https://www.prompthalo.ai/feeds/blog/agentic-ai-commerce-compliance-challenges-2025-2026)).

That third one is the commercial opening. An unresolved liability question creates demand for
**evidence about what was knowable before the action** — which is exactly what a rehearsal and
a score produce, and neither a rail nor a marketplace can.

## 2. Two adjacent categories, both crowded, neither ours

This is the part that had to be checked rather than assumed, because "we test agents" would
walk straight into a full market.

**Agent evaluation** — DeepEval, Maxim, Openlayer and peers. Metrics are `PlanQuality`,
`PlanAdherence`, `ToolCorrectness`; the discipline is asserting on the whole run — tool calls,
order, retrieved context, final state. Practitioners report evaluation at **60–80% of
development time**
([Openlayer](https://www.openlayer.com/blog/agent-testing-complete-guide-validating-ai-systems),
[Maxim](https://www.getmaxim.ai/articles/top-5-ai-agent-evaluation-platforms-in-2026/)).

**MCP surface scoring** — mcpscore ("Lighthouse for MCP", 41 rules over tool names, titles,
descriptions, JSON-Schema validity, TLS, OAuth/PKCE posture), MCP Scoreboard (63,205 public
servers), Agent Ready, Apify's Agent-Readiness Checker. A 100-server stress test found the
median MCP server is not production-ready, and that the top decile is separated by schemas,
idempotency, cancellation and quota tracking
([mcpscore](https://mcpscore.dev/), [MCP Scoreboard](https://mcpscoreboard.com/),
[stress test](https://www.digitalapplied.com/blog/mcp-server-reliability-100-server-stress-test-study)).

Now look at what each one's **authority** is:

| | unit under test | authority it compares against |
|---|---|---|
| agent evaluation | the **agent's behaviour** | a human-written expectation |
| MCP surface scoring | the **surface's shape** | a conformance checklist |
| **this** | the **surface's claims** | **execution against the chain** |

`ToolCorrectness` asks whether the agent picked the right tool with the right parameters —
**against a spec that may itself be wrong**. A mocked tool is ground truth by assumption.
mcpscore can certify that an instruction has a good description and a valid JSON Schema
while every one of these is true of it:

* the reported compute is **347× under** what the chain charges;
* an error code sits in the IDL's error table and is **raised nowhere in the program**;
* the derived address is well-formed and belongs to **somebody else's account**;
* **8 of 24** published flows require an input no agent can know.

Every one of those is a defect we measured this session. **Not one is visible to either
category**, because both grade the document and neither executes the claim.

**The white space: nobody's authority is reality.** For most APIs that is a rational
omission — you cannot execute a stranger's endpoint cheaply, repeatedly, or safely. On-chain
you can: simulation is free, the chain is a public oracle, and a fork is a place to be wrong.

## 3. The job

> **"I am pointing my agent at a surface I did not build. Before it acts, tell me whether what
> the surface says about itself is true — and give me somewhere to find out that costs
> nothing."**

Two halves, and they are one job:

* **The harness** — somewhere to be wrong. A coding agent has a filesystem, a test runner and
  `git revert`; an on-chain agent has a tool list and the call *is* the commit. The rehearsal
  restores the loop: fund, prepare, sign, land, read what moved, reset.
* **The CI** — the rehearsal, run without a human, on every change, with the answer published.
  One run is a debugging session; the same run unattended, on every instruction, is a score.

Neither alone is the job. A sandbox with no verdict is a place to guess more cheaply. A score
with no execution is the checklist that already exists in two crowded markets.

## 4. What the harness + CI can deliver that nothing else does

Ranked by how hard each is to copy.

1. **A second opinion on an address.** A wrong-but-well-formed seed mapping compiles clean,
   simulates clean, and points at the wrong account. Proven by mutation: both corruptions
   compiled 8/8 and ran 8/8 through the composer's own engine; only an independent derivation
   caught them. This cannot be produced from inside the composer at any level of effort.
2. **What a transaction moves.** Not which accounts are writable — the deltas. The measured
   surfaces compute risk from mutability flags and a regex on the instruction name, so a
   purchase that pays the buyer back scores identically to a real one.
3. **Declared versus raised.** An error code in an IDL reads as a guarantee. `StoreNotEmpty`
   is declared and never raised; `delete_store` simulated success at 16,893 CU against a store
   holding two products and 20 receipts. Seeing this requires reading the source, which an
   IDL-derived surface has no input that could show it.
4. **Reachability.** A tool can be perfect and invisible. Lexical retrieval scores **0.00** on
   paraphrase intents across four independent APIs; on the measured catalog, 5 of 5 natural
   phrases return "No programs found". A score that omits retrieval is grading a tool nobody
   will call.
5. **Drift, with a shape.** A score is a photograph. `mark_as_delivered` moved 34,858 → 34,137
   CU between two runs, and the model explains it. Saying *which* of the five answers changed
   is the difference between "something changed" and "your flow now reports a cost it will not
   charge".

## 5. The direction, and the milestone to aim at

**Direction: be the party whose authority is execution.** Not a rail (no funds, no cut), not a
marketplace (we render a surface its owner owns, and never rank owners against each other),
not a firewall (no classifier, no accuracy percentage). The three-verb table gains a fourth
row, and it is the one nobody occupies: **claims get VERIFIED.**

**The milestone: a surface a provider publishes, whose every claim was executed.**

Concretely — a page a provider hands their developers that says, per instruction: it builds,
it simulates, the number it reports is the number the chain charges, an agent asking in user
words can find it, it refuses what it should — each answer measured, each carrying its origin,
re-run on change, and **rehearsable in one click by the developer reading it**. The provider
owns the page. We own the method.

That milestone is worth aiming at for a reason beyond the product: **it is the same artifact
for both sides.** The provider sells trust with it; the agent builder tests against it; the
liability question in §1 is answered by it. One measurement, three buyers, and only one of
them has to pay — which is consistent with the standing rule that developers never pay and
revenue is provider-side and flat.

**Sequenced from here:** the rehearsal (building now) → the score as declarative shapes a
provider can dispute → ingestion for an arbitrary program (two named blockers) → the hosted
page. Not the fork: that is surfpool's, and we host one only until the Foundation's lands.

## 6. What this does not establish

Recorded so the direction is not read as a validated business.

* **Willingness to pay is still not validated.** It has been the decider for months and no
  paid customer has answered it. Everything above is a coherent job; none of it is demand.
* **The provider's reaction to an unrequested score is unknown.** One upstream fix merged in
  an hour, which is a signal and not a market.
* **Both adjacent categories could extend toward us.** An eval vendor adding a real sandbox,
  or a scorer adding execution, is a plausible move. What is not copyable quickly is the
  comprehension underneath — the seed recovery, the value domains, the provenance ladders —
  which is why the graph, not the score, is the asset.
* **The measurement is one catalog and 60 programs.** Half of PDA-bearing programs need no
  human; the tail is unmeasured.
