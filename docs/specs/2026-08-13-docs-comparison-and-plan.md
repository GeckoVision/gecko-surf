# Docs comparison and improvement plan

**2026-08-13. Internal spec. Not publishable, not partially publishable.**

The founder pointed at `https://docs.sylph.ai/` (the AdaL CLI docs, by SylphAI) and asked
whether there is anything worth stealing for ours.

**Naming rule, binding.** This file is the one place their name may appear. Nothing adopted
out of this spec may reference them by name in `../gecko-docs`, in the README, in a commit
message, or in a changelog entry. If attribution is ever needed outward, the phrase is
**"another docs site we reviewed."** Every sentence below that could plausibly migrate
outward is already written in that form.

**Steal rule, binding.** Mechanisms only. No prose of theirs is reproduced here and none
may be adapted. If an idea only works with their words, it is not adoptable and it is not
in the table.

**Honesty rule, binding.** The publishable mainnet compute-unit figures are **36,508**,
**36,399**, and **22,527** — those three and no others. The fork figures (86,669 / 81,964)
are fork figures and a fork is never mainnet. The signature count of record is **sixteen**,
taken from `../gecko-docs/mainnet.mdx:102`. Any docs change that needs a claim outside that
set is a regression, not an improvement, and is rejected in §3 with the reason stated.

---

## 0. What reached this node, and what did not

Per rule 3 of `docs/specs/2026-08-12-consolidated-plan.md:325` — *every ruling node
re-measures its own subject first-party and never rules from a relayed table* — the
provenance of each input is stated before it is used.

| Input | Reached this node? | How it is treated |
|---|---|---|
| Our docs, `../gecko-docs` on disk | yes | **measured first-party**, cited file:line throughout |
| Their site status facts (`llms.txt` 404, `llms-full.txt` 404, `sitemap.xml` 200, ~38 pages) | yes, as standing measured facts in the task brief | **MEASURED UPSTREAM** — used, labelled, not re-derived here |
| Their **page content** (quickstart shape, hooks, permissions, changelog voice, SDK reference generation, troubleshooting organisation) | **no** | **UNVERIFIED** — this node has no browser. Every claim about what their pages contain is marked UNVERIFIED and **carries no weight in the ranking** |
| An upstream truth table to carry verbatim | **no** | see the note below |

**The truth table did not arrive in transportable form.** The appendix was specified to
carry it verbatim because it is the load-bearing measurement. It reached this node as a
summary, not as a table, which is exactly the evidence-transport defect the consolidated
plan names as our most reproducible process failure
(`docs/specs/2026-08-12-consolidated-plan.md:96-97`). Rather than reconstruct someone
else's measurement from memory and present it as verbatim — which would manufacture the
defect — **Appendix A carries a truth table measured first-party in this node**, with a
provenance column, and marks every relayed row UNVERIFIED. Anyone with the original table
should diff it against Appendix A rather than trust either alone.

**Consequence for the ranking:** no adoption below is justified by a claim about their
content. Every "do now" item is justified by a defect or a gap **measured in our own repo**.
Their site's role in this document reduces to a prompt that made us look, plus two measured
facts about machine-readability. That is a smaller role than the brief anticipated, and
saying so is the honest answer the brief asked for.

---

## 1. Q1 — Information architecture

### 1.1 Their shape

Their nav, inferred from `sitemap.xml` only: `getting-started/`, `features/`,
`cloud-agents/`, `sdk/`, `troubleshooting/`, plus a changelog — roughly 38 pages
(MEASURED UPSTREAM). The section names are visible; the page order and grouping inside
them are **UNVERIFIED**.

That shape assumes a reader who has **already decided to adopt** and now needs to find a
capability by name. `features/` as a flat namespace is a lookup surface: it is excellent
once you know the noun you want, and it is useless before then, because a flat list of
capabilities cannot tell you which one you need first. `troubleshooting/` as its own
top-level section assumes the same reader — someone already running the thing, hitting an
error, searching by symptom.

### 1.2 Our shape — and the premise that turned out to be false

The brief states ours is task-shaped: *decide whether Gecko is for you → get your first
call working → point Gecko at your own API → make a call that spends money*.

**That is true of our agent-facing surface and false of our human-facing one.** Measured:

- `../gecko-docs/llms.txt:45,56,64,71` — the four section headings are exactly those four
  tasks, in that order.
- `../gecko-docs/docs.json:22-79` — the human nav groups are `Get Started`,
  `How it works`, `Use cases`, `On-chain`, `For AI agents`, `For API providers`,
  `Reference`.

`How it works`, `On-chain`, `Reference` are **topic** buckets, not tasks. `For AI agents`
and `For API providers` are **audience** buckets. So the human reader gets a three-way
hybrid — task, topic, audience — while the agent reader gets the clean task ladder. We
built the better IA and shipped it to the machine.

Two concrete costs of the hybrid, both first-party:

- `../gecko-docs/docs.json:28` puts **`roadmap` inside `Get Started`**, as the fifth thing
  a new reader is offered — before they have made a single call. Roadmap is an
  evaluation-stage page for a reader who already believes the thing works.
- `mcp-surface`, `access-and-auth` and `discoverability` are split across three different
  groups (`For AI agents` at `docs.json:60-63`, `How it works` at `docs.json:39`) even
  though all three belong to the single task *get your first call working*, which is how
  `llms.txt:56-62` groups them.

### 1.3 Where ours actually loses someone — the page and the sentence

**Page: `quickstart.mdx`. Sentence: line 45 —**

> `Reports your setup and the exact next command. It changes nothing and calls nothing.`

This is the first command a reader ever runs (`quickstart.mdx:40-46`) and the page never
shows what it prints. Grepped across all of `../gecko-docs/*.mdx`, the string `doctor`
appears at six sites (`introduction.mdx:151`, `quickstart.mdx:3,10,40,42,223`) and **not
one of them shows a line of output**. The same holds for `add`, `report` and `serve`:
`quickstart.mdx:48-83` gives four commands and zero expected results.

So the reader runs step 1, gets *something*, and has no way to tell whether it is the right
something. The page's own safety argument at `quickstart.mdx:219-231` — run in order and
you never take an unchecked step — depends on the reader being able to check, and the page
gives them nothing to check against.

We know how to fix this because we already did it one page over:
`gecko-101.mdx:56-58` prints `USDC   PEGGED   $0.9998   confidence: high` immediately after
the command, and the next sentence (`gecko-101.mdx:60`) tells you what that proves. That is
a success criterion. Quickstart has none.

### 1.4 Verdict

**A task ladder serves a first-time reader better than a feature list, and a feature list
serves a returning implementer better than a task ladder.** They optimised for the second
reader; we claim to optimise for the first and only actually do it in `llms.txt`.

The adoptable mechanism is **not** their `features/` list. It is the observation that they
have a distinct, findable home for the *already-running* reader (a symptom-indexed section)
and we have none — see Q2. Our fix at the top of the funnel is to make the human nav match
the ladder we already wrote for agents. Both are in §3.

---

## 2. Q2 — What they do that we do not

Mechanisms, with our side of each measured on disk. Every statement about **their**
implementation is UNVERIFIED; each row therefore stands or falls on our own gap.

### 2.1 A quickstart that proves it worked — GAP IS OURS, verified

Their quickstart's verification style: **UNVERIFIED**. Our gap does not depend on it:
`quickstart.mdx` ships four commands with zero expected output (§1.3). Cost to close: one
terminal session, four output blocks, one "you should see" line per step, and a note that
outputs are illustrative. Buys: the reader can self-serve the answer *did that work*,
which is the difference between an abandoned install and a first successful call.

### 2.2 Troubleshooting organised by symptom — GAP IS OURS, verified

They have a `troubleshooting/` section (MEASURED UPSTREAM, from the sitemap; its internal
organisation is UNVERIFIED). We have **nothing**. Grepped `../gecko-docs/*.mdx` for
`troubleshoot|If you see|error message|common error|symptom`, case-insensitive:
**no matches found**. A reader whose `add` fails has no page to land on.

Cost: real and recurring. A symptom index is only useful if the symptom strings are the
CLI's *actual* strings, so it has to be built from observed failures and re-checked when
error text changes. Buys: recovery instead of abandonment — but only after §2.1, because
a reader who never knew what success looks like cannot recognise a symptom.

### 2.3 A changelog — GAP IS OURS, verified

They publish one (MEASURED UPSTREAM, sitemap `/changelog`; its voice and cadence are
UNVERIFIED). We do not: `docs.json:16-83` lists no changelog page, no `changelog.mdx`
exists in `../gecko-docs`, and the only occurrence of the word anywhere in our docs is
`stay-correct.mdx:88`, where it refers to *someone else's* changelog. Cost: low per entry,
unbounded in aggregate — a changelog that stops is worse than none. Buys: evidence of
aliveness, which matters more than usual for a 0.x tool.

### 2.4 A "what this cannot do" page — WE ALREADY HAVE IT, and better

Whether they have one is UNVERIFIED. We do, in three layers, all verified:
`status.mdx:81-98` (`NOT built yet (honest)`, with `status.mdx:83` binding the rest of the
site to it), `llms.txt:98-108` (the same split, agent-readable), and per-page sections such
as `gecko-101.mdx:129-137` and `receipt.mdx`. `mainnet.mdx:135-139` goes further and
volunteers a live weakness — the rolling velocity counter is a file the same process can
write. **No adoption. This row exists so nobody re-proposes it.**

### 2.5 A generated SDK reference — GAP IS OURS, verified; adoption deferred

They have an `sdk/` section (MEASURED UPSTREAM); whether it is generated is UNVERIFIED.
We have no symbol-level reference at all — `architecture.mdx` is a module map, not an API
reference. Cost: high, and worse, it is the kind of page that rots invisibly. Deferred in
§3 with the reason.

### 2.6 Runnable examples kept true by machine — GAP IS OURS, partially

They have a workflows/examples area (MEASURED UPSTREAM); how they keep it true is
UNVERIFIED. We have exactly one worked end-to-end example page
(`txline-trading-agent.mdx`), and nothing in `../gecko-docs` re-runs any snippet. §3 says
when this becomes worth it.

### 2.7 Permissions / safety explained as a first-class page — PARTIAL, and honesty-capped

They have a permissions page (MEASURED UPSTREAM); its content is UNVERIFIED. We cover the
same ground across `access-and-auth.mdx`, `quickstart.mdx:112-127`, and
`status.mdx:73-76`. A consolidated "permissions" page is **honesty-capped** — see §3, row
I — because our spend controls are not all enforceable and `mainnet.mdx:135-139` says so.

---

## 3. Q3 — What we do that they do not, and whether we are cashing it

### 3.1 Machine-readability — we have it, we barely sell it

**Theirs:** `llms.txt` → 404, `llms-full.txt` → 404, SPA-rendered so `curl` returns a 404
shell for every path including the homepage (all MEASURED UPSTREAM). No machine-readable
surface.

**Ours, verified on disk:** `../gecko-docs/llms.txt` exists (119 lines) and is written
*for the agent as reader* — `llms.txt:10` literally addresses it (`agents, read this
section first`), `llms.txt:18-25` gives the exact parameter name and names the failure mode
of getting it wrong. `llms.txt:88-96` advertises `llms-full.txt`, `gecko.json`,
`/.well-known/gecko.json` and the append-`.md` convention; `gecko.json` and
`.well-known/gecko.json` are both on disk.

**Are we cashing it?** Barely. In the entire human-facing site the capability is sold in
**one `<Note>`**, `quickstart.mdx:31-35`. `introduction.mdx` — the page a human actually
lands on — never mentions it. We are the docs site an agent can read, and we tell that to
almost nobody. Cheap fix in §5, item D.

### 3.2 Publicly verifiable claims — we have it, and it is currently self-contradicting

`mainnet.mdx` is a page whose entire value is that the reader does not have to trust us:
sixteen signatures, each linked to Solscan, plus a `curl` at `mainnet.mdx:44-50` that asks
a public node directly. That is an asset almost no docs site has.

**And three of our pages disagree about how many there are.** Measured:

| page | line | says |
|---|---|---|
| `gecko-101.mdx` | 124, 127 | "**Eleven** real mainnet purchases across two days" / "All eleven are public" |
| `introduction.mdx` | 18-19 | "**fifteen** times out of fifteen" / "the last **four** signed inside a hardware enclave" |
| `mainnet.mdx` | 102, 129-130 | "**Sixteen** transactions. Sixteen exact predictions." / "the last **five** with no key on the planning machine" |
| `llms.txt` | 73 | "**sixteen** public mainnet signatures … the last **five**" |

`mainnet.mdx` is the source of truth and its own table (`mainnet.mdx:60-71`, `:95-100`)
supports sixteen and five. The other two pages are stale. A reader who does the exact thing
the page invites — check — finds our numbers disagreeing with each other before they even
reach the chain. This is the most damaging defect in the docs and the cheapest to fix.

### 3.3 Honest "not built" as a load-bearing feature

Covered in §2.4. Verified, ours, uncashed by nobody — it is already linked from
`introduction.mdx:165-167` and `llms.txt:50`. No work.

### 3.4 Provenance vocabulary published as doctrine

`status.mdx:25-27` publishes both provenance ladders — `EXTRACTED > DECLARED > INFERRED >
CLAIMED → VERIFIED / REFUTED` and `EXTRACTED / RECOVERED / FLAGGED`. Whether they have an
analogue is UNVERIFIED and irrelevant: this is ours and it is cashed. No work.

---

## 4. Q4 — The plan, ranked

Value = to a **first-time reader** (1-5). Cost = build **and keep true** (1-5). Ratio =
value ÷ cost. Honesty = whether the item can be built without a claim we cannot support.
**Where value is high and honesty is BLOCK, the item is rejected here, with the reason —
not quietly dropped.**

| # | Item | Value | Cost | Ratio | Honesty | Verdict + reason |
|---|---|---|---|---|---|---|
| A | Reconcile the mainnet count (11 / 15 / 16) to sixteen and single-source it | 5 | 1 | **5.0** | CLEAR | **DO NOW.** Two pages currently contradict the one page built for verification (§3.2). Repairs a false claim; costs one edit each plus a grep in CI. |
| B | Re-shape the human nav in `docs.json` to the task ladder already in `llms.txt` | 4 | 1 | **4.0** | CLEAR | **DO NOW.** One JSON file, no page slugs change, no claim changes. Fixes `roadmap` sitting fifth in `Get Started` (`docs.json:28`) and the three first-call pages split across groups. |
| D | Sell machine-readability on `introduction.mdx`, not only in a quickstart `<Note>` | 3 | 1 | **3.0** | CLEAR | **DO NOW.** The capability is already shipped and verified (§3.1); this is one paragraph describing something that exists. |
| C | Success criteria in `quickstart.mdx` — expected output + a "did it work" line per step | 5 | 2 | **2.5** | CLEAR, with a caveat | **DO NOW.** Closes the loss point named in §1.3. Caveat: outputs must be labelled illustrative, or they become a claim about exact CLI text that rots. |
| E | Troubleshooting indexed by symptom | 4 | 3 | **1.33** | CLEAR | **DO WHEN** C has landed and we have ≥6 symptoms with the CLI's real strings. Before C, a reader cannot tell a symptom from normal output, so E has nothing to attach to. A symptom page written from imagination is worse than none. |
| K | A gallery of runnable examples kept true by machine | 4 | 4 | **1.0** | CLEAR | **DO WHEN** we have ≥3 example programs *and* something that re-runs them. Today there is one (`txline-trading-agent.mdx`) and nothing re-runs it; a "gallery" of one is a page title pretending to be a section. |
| F | A changelog page | 2 | 3 | **0.67** | CLEAR | **DO WHEN** releases have a public cadence to report. Recurring cost, and a changelog that stops signals the opposite of what it is for. Not now. |
| J | A flat `features/` section mirroring theirs | 1 | 2 | **0.5** | CLEAR | **DO NOT.** It is the shape Q1 judged worse for our reader, and we would be adopting the weaker half of what we looked at. Rejected on the merits, not on cost. |
| G | A generated symbol-level SDK reference | 2 | 5 | **0.4** | CLEAR but risky | **DO NOT (now).** A generated reference implies a stable public API surface; we are 0.x and `architecture.mdx` deliberately documents seams rather than signatures. Cost is also the wrong shape — it rots silently, which is the failure mode our docs exist to argue against. Revisit at 1.0. |
| H | A hosted "click it and watch it verify" live-demo page | **5** | 3 | 1.67 | **BLOCK** | **REJECTED — honesty.** The highest-value idea we looked at, and we cannot build it. It would need hosted point-and-simulate, which `status.mdx:86-87` lists as **not built** and `llms.txt:104` states plainly: *simulation is local; the hosted surfaces give tools and plans; a receipt needs your own RPC.* A page that lets a stranger watch a verification run on our infrastructure would claim a capability we do not have, and `status.mdx:83` binds the whole site to the not-built list. **Not deferred — rejected until hosted simulate exists.** The honest substitute already ships: `mainnet.mdx` (check sixteen real signatures yourself) and `gecko-101.mdx:19-27` (one-take video). |
| I | A consolidated "permissions" page describing enforceable limits | 4 | 2 | 2.0 | **BLOCK as scoped** | **REJECTED as scoped — honesty.** A page headed "permissions" reads as *these limits hold*. `mainnet.mdx:135-139` records that three of four spending caps are real controls and the rolling velocity counter is **a file the same process can write** — a compromised agent can reset its own budget. Consolidating without leading on that would over-claim; leading on it makes the page an admission, which `mainnet.mdx` already carries in the right context. **Reduced form permitted:** a cross-link cluster from `access-and-auth.mdx` to the existing signing-gate and cap material, adding no new claim. That reduced form is folded into B's nav work, not given its own page. |
| L | Adopting their phrasing, headings, or page voice | 0 | 1 | 0 | **BLOCK** | **REJECTED — house rule.** Steal mechanisms, never sentences. Also unnecessary: nothing above needs their words. |

---

## 5. Do now — as PRs, with falsifiable definitions of done

All four are in `../gecko-docs`. **This spec does not touch that repo**; these are for a
separate branch there, opened and pushed by the founder. None changes a page slug, so no
redirects are needed. Each DoD is a check someone else can run and fail.

### PR-A · one mainnet count, single-sourced

**Files:** `gecko-101.mdx`, `introduction.mdx`, and a CI check.

1. `gecko-101.mdx:124,127` — "Eleven … across two days" → **sixteen**, across the span
   `mainnet.mdx` actually covers (`mainnet.mdx:129` says three days for the first fifteen;
   restate from the table at `mainnet.mdx:60-71` and `:95-100`, do not re-derive).
2. `introduction.mdx:18-19` — "fifteen times out of fifteen" → **sixteen out of sixteen**;
   "the last four signed inside a hardware enclave" → **the last five**.
3. `introduction.mdx:24-25` already lists exactly `36,508`, `36,399`, `22,527`. Leave it.
   No fourth number enters any page.
4. Add a docs CI step: grep every `*.mdx` and `llms.txt` for
   `eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen` in a sentence within two
   lines of `mainnet|signature|transaction|prediction`, and **fail if more than one
   distinct count appears across the repo**.

**DoD (falsifiable):**
- `grep -rniE '(eleven|fifteen)' ../gecko-docs --include=*.mdx --include=*.txt` returns
  **zero** lines that refer to the mainnet series. (Today it returns four:
  `gecko-101.mdx:124,127`, `introduction.mdx:18,19`.)
- The count and the enclave sub-count in `gecko-101.mdx`, `introduction.mdx`, `llms.txt`
  and `mainnet.mdx` are **identical** — sixteen and five.
- The CI check is proven by **mutation**: reverting `introduction.mdx:18` to "fifteen"
  turns the check **RED**. A green check on unmutated main proves nothing
  (`docs/specs/2026-08-12-consolidated-plan.md:167`).
- No compute-unit figure outside {36,508 · 36,399 · 22,527} appears on any mainnet page,
  and no fork figure appears without the word "fork" in the same block.

### PR-B · the human nav becomes the task ladder

**File:** `docs.json` only.

Regroup `navigation.tabs[0].groups` to the four task headings already proven in
`llms.txt:45,56,64,71`, plus one reference bucket:

- *Decide whether Gecko is for you* — `introduction`, `gecko-101`, `code-assistants`,
  `status`
- *Get your first call working* — `quickstart`, `pegana`, `mcp-surface`,
  `access-and-auth`, `discoverability`
- *Point Gecko at your own API* — `from-docs`, `comprehension`, `recorded-mode`,
  `stay-correct`, `for-providers`, `cloud`
- *Make a call that spends money* — `mainnet`, `receipt`, `program-surface`, `find-start`,
  `use-cases`
- *Reference* — `concepts`, `architecture`, `roadmap`, `txline-trading-agent`

**DoD (falsifiable):**
- The set of page slugs in `docs.json` before and after is **byte-identical as a set** —
  `jq` the slugs, sort, diff, expect empty. Zero pages added, removed or renamed.
- `roadmap` is **not** in the first group (fixes `docs.json:28`).
- `mcp-surface`, `access-and-auth` and `discoverability` are in **one** group.
- Group headings match `llms.txt`'s four section headings word-for-word, so the human and
  agent surfaces can no longer drift apart silently.
- Mintlify builds; every existing URL still resolves.

### PR-C · quickstart proves it worked

**File:** `quickstart.mdx`.

For each of the four steps (`quickstart.mdx:39-83`), add (i) a trimmed real output block
captured from an actual run, and (ii) one bolded line naming the success signal, in the
style already proven at `gecko-101.mdx:56-60`. Add one `<Note>` stating outputs are
illustrative and exact text may differ by version.

**DoD (falsifiable):**
- All four `<Step>` blocks contain an output block. Today: **zero** do — verified by
  grepping `doctor` across `../gecko-docs/*.mdx`, which returns six sites and no output.
- Every output block was **captured from a real run**, not written by hand; the capturing
  command is recorded in the PR body so a reviewer can re-run it.
- No output block contains a token, key, path under `$HOME`, or hostname belonging to
  anyone.
- The "illustrative" note is present, so the blocks are not a claim about exact CLI text.
- A reader can answer *did step N work* from the page alone, without running step N+1.

### PR-D · say that our docs are agent-readable, on the page humans land on

**Files:** `introduction.mdx` (and, if trivially placed, `gecko-101.mdx`).

Add one short block near the top of `introduction.mdx` pointing at `/llms.txt`,
`/llms-full.txt`, and the append-`.md` convention — the same three surfaces
`llms.txt:88-96` already advertises to machines. Frame it as *hand this site to your
agent*. **No comparative claim of any kind** — no "unlike other docs", no reference to
another docs site, named or unnamed.

**DoD (falsifiable):**
- `introduction.mdx` mentions `llms.txt` at least once. Today it mentions it **zero**
  times; the only mention in the site is `quickstart.mdx:31-35`.
- Every URL in the new block resolves (`/llms.txt`, `/llms-full.txt`, and one page with
  `.md` appended).
- The block contains no comparison to any other product or docs site.

---

## 6. Appendix A — the truth table

Measured first-party in this node against `../gecko-docs` on disk, except where the
provenance column says otherwise. See §0 for why this is not the upstream table carried
verbatim.

| # | Claim | Verdict | Provenance | Evidence |
|---|---|---|---|---|
| 1 | Their site is SPA-rendered; `curl` returns a 404 shell for every path | TRUE | MEASURED UPSTREAM | task brief, standing fact; not re-derived |
| 2 | `https://docs.sylph.ai/llms.txt` → 404 | TRUE | MEASURED UPSTREAM | as above |
| 3 | `https://docs.sylph.ai/llms-full.txt` → 404 | TRUE | MEASURED UPSTREAM | as above |
| 4 | `https://docs.sylph.ai/sitemap.xml` → 200, ~38 pages under `getting-started/`, `features/`, `cloud-agents/`, `sdk/`, `troubleshooting/`, `/changelog` | TRUE | MEASURED UPSTREAM | as above |
| 5 | Anything about the **content** of their pages | **UNVERIFIED** | not measured anywhere reachable from this node | no browser in this node; no adoption below depends on it |
| 6 | Our docs have `llms.txt` | TRUE | first-party | `../gecko-docs/llms.txt`, 119 lines |
| 7 | Our `llms.txt` addresses the agent as reader and names the failure mode of a wrong parameter | TRUE | first-party | `llms.txt:10`, `:18-25` |
| 8 | Our `llms.txt` is organised as four tasks | TRUE | first-party | `llms.txt:45,56,64,71` |
| 9 | Our **human** nav is not task-shaped; it is task+topic+audience hybrid | TRUE | first-party | `docs.json:22-79` — `How it works`, `On-chain`, `Reference` are topics; `For AI agents`, `For API providers` are audiences |
| 10 | `roadmap` is offered as the 5th page of `Get Started` | TRUE | first-party | `docs.json:28` |
| 11 | The three first-call pages are split across groups | TRUE | first-party | `docs.json:39` vs `:60-63` |
| 12 | `quickstart.mdx` shows **no** expected output for any of its four steps | TRUE | first-party | `quickstart.mdx:39-83`; `doctor` appears at `introduction.mdx:151`, `quickstart.mdx:3,10,40,42,223` — no output block at any site |
| 13 | We do show a success criterion elsewhere, so the pattern exists in-house | TRUE | first-party | `gecko-101.mdx:56-60` |
| 14 | We have **no** troubleshooting page and no symptom-indexed content | TRUE | first-party | grep of `../gecko-docs/*.mdx` for `troubleshoot\|If you see\|error message\|common error\|symptom` (case-insensitive): **no matches found** |
| 15 | We have **no** changelog page | TRUE | first-party | no `changelog.mdx` on disk; `docs.json:16-83` lists none; the only hit for `changelog` in the repo is `stay-correct.mdx:88`, describing someone else's |
| 16 | The brief's page list included a changelog page for us | **REFUTED** | first-party | see row 15 |
| 17 | We have an honest "not built" surface, in three layers | TRUE | first-party | `status.mdx:81-98`, binding sentence at `status.mdx:83`; `llms.txt:98-108`; `gecko-101.mdx:129-137` |
| 18 | We volunteer a live weakness rather than hide it | TRUE | first-party | `mainnet.mdx:135-139` — the rolling velocity counter is a file the same process can write |
| 19 | Hosted point-and-simulate is **not built** | TRUE | first-party | `status.mdx:86-87`; `llms.txt:104` |
| 20 | Signature count of record = **sixteen**; enclave-signed sub-count = **five** | TRUE | first-party | `mainnet.mdx:102`, `:129-130`, table rows `:95-100` |
| 21 | `gecko-101.mdx` says **eleven** | TRUE (and stale) | first-party | `gecko-101.mdx:124`, `:127` |
| 22 | `introduction.mdx` says **fifteen** and "last four" | TRUE (and stale) | first-party | `introduction.mdx:18-19` |
| 23 | `llms.txt` agrees with `mainnet.mdx` (sixteen / last five) | TRUE | first-party | `llms.txt:73` |
| 24 | Publishable mainnet CU set {36,508 · 36,399 · 22,527} is used correctly on the mainnet pages | TRUE | first-party | `mainnet.mdx:39`, `:57`, `:98`; `introduction.mdx:24-25` |
| 25 | Fork figures are labelled as fork, not mainnet | TRUE | first-party | `introduction.mdx:101-107` ("a mainnet-backed state snapshot — **not mainnet**"); `status.mdx:49-56` |
| 26 | We have exactly one worked end-to-end example page | TRUE | first-party | `txline-trading-agent.mdx`; no second example page in `docs.json` |
| 27 | We have no symbol-level SDK reference | TRUE | first-party | `docs.json:74-79` — `Reference` contains `architecture` and `status` only |
| 28 | Machine-readability is sold to humans in exactly one place | TRUE | first-party | `quickstart.mdx:31-35`; `introduction.mdx` contains no mention |

---

## 7. The one change

**If only one change were possible: reconcile the mainnet count to sixteen, single-source
it, and put a mutation-proven check in docs CI so it cannot drift again — PR-A.**

Why this one and not the nav:

`mainnet.mdx` is the only page on our site whose value does not depend on trusting us. It
hands the reader sixteen public signatures and a `curl` against a public node
(`mainnet.mdx:44-50`) and says, in effect, go and check. It is the strongest asset in the
docs.

And right now, a reader who accepts that invitation finds our own pages disagreeing before
they reach the chain: eleven on `gecko-101.mdx:124`, fifteen on `introduction.mdx:18`,
sixteen on `mainnet.mdx:102`. The defect is not that a number is stale. It is that the
inconsistency lands specifically on the claim we ask to be audited — and the whole product
argument is *we tell you what will happen and it happens*. A docs site making that argument
cannot be the thing that fails to keep its own numbers straight.

It also costs less than any other item on the list: two sentence edits and a grep. Value 5,
cost 1.

**Runner-up, deliberately not chosen: PR-B**, the nav. B is the larger structural
improvement and it fixes the premise this whole review started from — we built the task
ladder and shipped it only to machines. But B makes true claims easier to find, while A
stops two pages from making a false one. Repair before rearrange.

---

## 8. The short answer, for the record

The founder asked whether there is something worth stealing. **Honestly: not much.**

Their machine-readability is strictly behind ours — no `llms.txt`, no `llms-full.txt`, SPA
content a `curl` cannot reach (rows 1-4). Their feature-shaped IA is worse for our reader
than the ladder we already wrote (§1.4). Their "what this cannot do" story, whatever it is,
is not going to beat `status.mdx` plus a page that volunteers its own weakest control
(§2.4).

Three things we genuinely lack showed up: **success criteria in the quickstart**, a
**symptom-indexed troubleshooting** surface, and a **changelog**. Only the first is worth
doing now, and it is worth doing because of what §1.3 measured in our own repo, not because
they have one.

And the review's most valuable output was not adopted from them at all: looking hard at our
own pages surfaced a three-way contradiction in the mainnet count. The comparison earned
its keep by making us read our own docs like a stranger.
