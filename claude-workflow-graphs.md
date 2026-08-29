```markdown
# Claude Code Instructions: Building Workflow Graphs & Dynamic Workflows

**Copy-paste this entire document (or relevant sections) into Claude Code as system context, a CLAUDE.md rule, or a direct prompt.**  
These instructions teach Claude how to design, generate, and execute **workflow graphs** using Claude Code’s native **dynamic workflows**. Follow them exactly.

---

## 1. Core Mental Model (Read This First)

A **graph** is a plan for AI work that answers two questions only:
1. Which jobs need to happen?
2. Which job must wait for which other job?

### The only two primitives
- **Node** = one bounded unit of work  
  - One agent  
  - One clear job  
  - One defined input → one defined output (the “contract”)  
  - Examples: “research competitor X”, “review this single file for missing auth”, “run the unit tests and return pass/fail + logs”

- **Edge** = a real dependency  
  - An edge exists **only** if the output of node A is required as input by node B.  
  - If no real data flows, there is **no edge**. The two nodes can (and should) run in parallel.

**Critical test – the Fake-Edge Test**  
For every “A then B” in a linear workflow, ask:  
> “Does B actually need anything that A produced?”  

If the answer is **no**, delete the edge. Those two nodes become parallel.

A linear chain of 40 steps has 40 sequential failure points and latency = sum of all steps.  
The same work drawn as a graph usually has only 3–5 real dependency layers and finishes at the speed of the slowest layer.

---

## 2. The One Pattern That Pays for Itself: The Diamond

Almost every high-value workflow is a **diamond**:

```
          Planner / Router
                 │
     ┌───────────┼───────────┐
     │           │           │
  Worker₁     Worker₂     Workerₙ   ← fan-out (parallel, independent)
     │           │           │
     └───────────┼───────────┘
                 │
            Reducer (plain JS – free)
                 │
            Verifier(s)  ← fresh context only
                 │
            Synthesizer  ← final strong model
```

### Execution order inside Claude Code dynamic workflow
1. **Fan-out** – spawn N independent sub-agents in parallel (`pipeline()` or multiple `agent()` calls).
2. **Reduce** – ordinary JavaScript (Array.filter, Map, JSON, etc.). Zero tokens.
3. **Verify** – independent agents with **clean context windows**. Never share chat history with the workers.
4. **Synthesize** – one final agent that receives only the verified, reduced data.

---

## 3. Importance of Anchors (The Part Almost Everyone Misses)

Topology alone does **not** buy truth.

A graph full of agents checking other agents can still be completely wrong if every node is reading reports written by other models. Everything becomes consistent… and false.

**Anchors** are nodes that **cannot be argued with**:

| Anchor Type                  | Example                                      | Why it works                          |
|-----------------------------|----------------------------------------------|---------------------------------------|
| Real executed tests         | `npm test` / `pytest` exit code + full logs  | Machine reality, not a claim          |
| Frozen business rules       | Hard-coded constants, schema, policy files   | Optimizer cannot weaken them          |
| External ground truth       | Live API response, bank transaction, Git SHA | Outside the model’s context           |
| Deterministic code          | Regex, static analysis, linter results       | No LLM involved                       |

**Rule**: Every critical path in the graph must terminate (or be gated) by at least one anchor.  
If the graph only ever talks to itself, it is just an expensive self-grading loop.

Always place anchors **after** verification and **before** synthesis.

---

## 4. How to Properly Run a Dynamic Workflow in Claude Code

### Trigger words (use any of these)
- “Use a **workflow** …”
- “Run this as a **dynamic workflow**”
- “**ultracode**”
- Built-in: `/deep-research`, `/effort ultracode`

### What Claude actually does
1. Writes a short JavaScript orchestration script (you can view the raw script).
2. The script uses special functions: `agent()`, `pipeline()`, schemas, variables, branches, loops.
3. Coordination lives in **code**, not conversation → intermediate results cost **zero** context tokens.
4. Sub-agents run with isolated contexts (and optionally their own worktrees).
5. You approve the plan before any agents start.
6. Successful workflows can be saved with `/workflows` → re-run by name forever.

### Do I need a different agent description for every agent?

**No.**

Claude Code dynamic workflows generate the system prompts and role descriptions on the fly inside the orchestration script.  
You only need to describe the **roles and contracts** clearly in your initial prompt. Claude fills in the rest.

**Best practice** (include this in every workflow prompt):

```
For every node:
- Give it a short, precise role name
- Define exact input schema
- Define exact output schema (JSON preferred)
- Specify which model to use if needed (Haiku for cheap workers, Opus for judgment)
- Never share context between a worker and its verifier
```

You do **not** pre-register dozens of custom agents. The dynamic workflow creates them at runtime.

---

## 5. Ready-to-Paste Examples

### Example 1 – Decision-grade research desk (classic diamond + anchors)

```
Use a workflow.

Task: Research the competitive landscape for [PRODUCT] and produce a decision-grade positioning report.

Graph:
1. Planner node – break the question into 5–7 independent research angles.
2. Fan-out: one research agent per angle, running in parallel. Each returns structured findings with sources.
3. Reduce: plain JS deduplicate and cluster findings.
4. Verifier nodes (fresh context each): 
   - Fact-checker: does every claim have a real, dated source?
   - Currency checker: is the data still valid in 2026?
5. Anchor: only findings that survive verification + have live URLs or official docs move forward.
6. Synthesizer (Opus): write the final report using only anchored findings.

Show me the script first, then run it. Cap at 8 research agents.
```

### Example 2 – Codebase-wide security audit

```
Use a workflow on this repository.

Goal: Find every route / endpoint missing proper authorization.

Graph:
1. Discoverer node – list all routes/endpoints (use codebase tools).
2. Fan-out: one reviewer agent per route (max 30 parallel). Each returns {route, missingAuth: bool, evidence}.
3. Reduce: filter only candidates where missingAuth === true.
4. Independent verifier agents (fresh context): try to disprove each candidate.
5. Anchor: only issues where a real code path can be demonstrated (file + line) survive.
6. Final synthesizer: ranked report of confirmed missing-auth findings.

Never let a reviewer and its verifier share context. Use cheaper models for reviewers.
```

### Example 3 – Refactor sweep with test anchors

```
Use a dynamic workflow.

Task: Migrate all usages of [OLD_API] to [NEW_API] across the entire codebase.

Graph:
1. Planner – identify every file that still uses OLD_API.
2. Fan-out: one implementer agent per file (isolated worktrees).
3. After each implementer: run the project’s test suite as an Anchor node.
4. Only files whose tests still pass move to the merge stage.
5. Final synthesizer: produce a single PR-ready summary + any remaining failures.

If any test anchor fails, route feedback only to that specific file’s implementer. Do not re-run the whole graph.
```

### Example 4 – Discovery loop of unknown size (bounded)

```
Use a workflow with a bounded loop.

Task: Hunt for every instance of [BUG_PATTERN] in the repo.

Start with a broad searcher.
Whenever a real instance is confirmed by an anchor (actual failing test or static analysis), spawn additional focused searchers.
Stop when no new confirmed instances appear for two consecutive rounds or after 15 total agents.
Final output: complete list of confirmed locations + reproduction steps.
```

---

## 6. Mandatory Rules Claude Must Follow

1. Always show the generated JavaScript script and wait for explicit approval before spawning agents.
2. Worker and Verifier must never share context or conversation history.
3. Every critical path must contain at least one **Anchor** (real test run, external data, frozen rule).
4. Use plain JavaScript for all reduce / filter / dedupe steps (zero tokens).
5. Prefer schemas (`agent({ schema: ... })`) so every node returns structured data.
6. Cap fan-out on first runs (e.g. “max 10 parallel agents”) until cost is understood.
7. On interruption, the workflow is resumable – continue from the last checkpoint.
8. After a successful run, offer to save the workflow with a short name.

---

## 7. When NOT to Use a Graph

- Single-file / single-function changes
- Pure exploratory / “I don’t know what I’m looking for yet” work
- Tasks where every step truly depends on the previous one
- When you want to approve every intermediate step yourself

In those cases just use a normal loop or single agent.

---

**End of instructions.**  
Claude: when the user says “build a workflow graph”, “use a dynamic workflow”, or “ultracode”, follow the rules and patterns above exactly. Always start by drawing the nodes + edges + anchors, then generate the script.
```

