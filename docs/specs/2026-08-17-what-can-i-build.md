# "What can I build on this?" — the provider's showcase, and the three ways it goes wrong

**Status:** design response, 2026-08-17. Grounded in the correlation measurement
(`2026-08-17-what-correlates.md`) and in `sendaifun/solana-new`, cloned and read.

---

## The proposal, and why it is right

A tool a provider ships that answers, for their surface: **what can I build on this, what does
it connect to, and does it actually work?** Not a tool list — a starting point, with runnable
examples, a live simulation, and a CI that keeps it true.

It is right for a reason that has nothing to do with the demo: **it is the first artifact that
uses the graph for what a graph is for.** Everything we have shipped so far answers "is this
one call correct." A developer does not have that question. They have "what can I make with
this, and what else does it need." That question is a graph traversal and nothing else can
answer it.

It also lands exactly on the provider-buyer reframe: the provider ships it, under their name,
to developers they want building on them. Same shape as the surface page and the plugin.

**So: build it.** With three sharpenings, because the appealing version of this idea is the
one that quietly abandons every rule we have.

## Sharpening 1 — a buildable, not an idea

`solana.new` offers *"500+ ideas sourced from YC, Alliance, Superteam, and SendAI."* Those are
human-sourced and inspirational. **We must not do that**, because an idea is not refutable and
every artifact we ship has been.

The honest version is stronger and only we can make it:

> **A buildable is shown only if the calls it needs score green.**

Not "here is something you could build" but "here is something that builds, simulates, and
reports honestly, as of this morning — and here is the run that proves it." An idea list is
inspiration. **A verified-buildable list is a claim with a receipt**, and it is the difference
between our showcase and a landing page.

It also fails usefully: a buildable that stops scoring green disappears from the page and the
provider is told which call broke it. That is the drift watch earning its keep.

## Sharpening 2 — the 4,800-node map is the seductive part and the weakest

A graph across the whole catalogue is the image that sells this, and our own measurement says
what it would be made of:

* **277 account names recur across programs; 15% of them mean different things** — a PDA in one
  program and a plain account in another. `event_authority` is a PDA 271 times and a plain
  account 10 times.
* An exact-name join **misses about a third of its true matches** on `snake_case` versus
  `camelCase` alone.
* `index` is a `u32` in 21 places, a `u8` in 11 and a `u16` in 11 — and it is used as a seed.

**A 4,800-node map built on names would be wrong in exactly the way we publish findings about.**
That is not an argument against the map; it is the specification for it. Build it from what
actually correlated:

| edge kind | basis | density |
|---|---|---|
| declared dependency | a **pinned program address** in an account slot | sparse, explicit, already true |
| family resemblance | **shared constant seed vocabulary** (`tick_array`, `amm_config`, `pool_vault`) | 32 seeds across families |
| type-compatible input | **value-domain signature**, never the argument name | the thing we already compute |

The resulting map is smaller and less impressive than a name graph. It is also *right*, and
each edge can say where it came from. **A beautiful map that is 15% fabricated is worse than a
sparse map that is not.**

## Sharpening 3 — per-provider, or we become the marketplace

A cross-provider "here is everything you could build across the ecosystem" map ranks and
compares providers, implicitly and then explicitly. That is the line this repo has held all
year: **we render one owner's surface and never rank owners against each other.**

So the product is **per-provider by default**: their surface, their buildables, their name.
Cross-program edges appear when the provider's own surface genuinely depends on another
program — a pinned address is a fact about their program, not a comparison. What we do not
ship is a leaderboard of ecosystems wearing a graph's clothing.

## What it actually produces, for one surface

Taking `let_me_buy`, where everything below is already measured:

```
WHAT YOU CAN BUILD                        VERIFIED           WHAT IT NEEDS
a storefront checkout                     6/6 green          SPL Token, ATA
a delivery queue                          green, WITH A CAP  the queue depth is 20
a two-agent waiter + kitchen service      rehearsed on fork  two signers
```

and beside each, the thing no scaffolder can give you:

* the **runnable** call, projected into whatever format the client wants — FDL today, an
  Agent Plugin, an A2A card;
* a **rehearsal**, so the developer's first attempt happens where being wrong is free;
* the **traps**, because a starting point that omits them is a trap: the two ATAs that differ
  only by owner, the missing authority guard, the receipts cap that loses orders silently;
* a **CI** that re-runs it and says which of the five answers moved.

`solana.new` gets you to a scaffold. **This gets you to a call that works against a program
that actually exists**, which is the step where the scaffold stops helping.

## Distribution: contribute a skill, do not build a rival installer

`sendaifun/solana-new` is a skill router with four phases — Learn, Idea, Build, Launch — and
its `CONTRIBUTING.md` explicitly invites outside skills: *"Pick the phase where your skill
contributes most."* Adding to the catalogue is documented as a five-minute contribution.

Its Build phase already has `scaffold-project`, `build-defi-protocol`, `launch-token`. **What
it has no skill for is whether the call you are about to make is correct against a specific
deployed program.** That is a skill-shaped hole in someone else's router, and the same logic
that applied to surfpool and to Agent Plugins applies here: **use the distribution, do not
rebuild it.** A rival `curl | bash` competes with a channel we would rather be inside.

## What this does not establish

* **Nobody has asked for it.** Same status as the surface page and the plugin — a coherent
  artifact, an unvalidated demand.
* **"Verified buildable" needs a definition before it needs a UI.** How many green calls make a
  buildable, and does one red call remove it? Undecided, and it should be decided by looking at
  a real one rather than in the abstract.
* **The map is measured on 56 programs, not 4,400.** The tail is older and messier, and the
  legacy-IDL crash we fixed this week is evidence of what it holds.
* **We have not shown that a correlation makes an agent more correct.** The measurement says
  what could be joined. Proving the join helps needs the before/after harness pointed at a
  multi-program task, and that golden set does not exist.
