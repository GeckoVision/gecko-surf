# What we deliver to Orquestra, and why a patch cannot carry it

**Status:** proposal, 2026-08-18. Every number below was measured this week against the
live catalogue, the live MCP and mainnet. Nothing here has been asked for.

Narrower than `2026-08-16-ingestion-graph-surface.md`, which designs the whole ingestion
surface. This answers one question: **what, concretely, do we hand a catalogue owner, and
what is the line between something we send as a patch and something we send as data.**

---

## 1. The question, and why it is now answerable

We shipped a fix upstream (PR #11: a seed's declared type, resolved through `idl.types`).
It is the right kind of contribution — their vocabulary, their exports, their tests. But it
prompted an obvious follow-up we could not answer honestly until we measured it: **once
that merges, what is still missing?**

Measured on jurassic_fi, six instructions need the `launch` account:

| | before PR #11 | after PR #11 |
|---|---|---|
| refuse (`launch.admin` needs the account being derived) | 5 | **5** |
| answer, wrongly (arg width unresolved) | 1 | 0 |

The width fix removes the wrong answer. **It does not move the five refusals**, and it
cannot: a seed that reads a field of the account being addressed is unresolvable from the
IDL *by construction*. No patch to their code changes that, because the missing fact is not
in the file their code is reading.

That is the line, and it is not a matter of effort or ownership:

> **Derivable from the IDL alone → their code. We send a patch and walk away.
> Needs more than the IDL → an artifact. We send data, they store it.**

The line is our own provenance ladder, restated: `extracted` is theirs, `recovered` and
`manual` are ours.

## 2. What sits on our side of the line, counted

Across the catalogue's most-used programs, on the `(instruction, account)` pairs their
listing publishes:

| | typed seeds | derivable accounts |
|---|---|---|
| their surface | 49/734 — 6.7% | 7/784 — **0.9%** |
| with our graph | 706/734 — 96.2% | 386/784 — **49.2%** |

Plus **591 PDA slots we resolve that their listing does not mention at all** — recipes
carried from the instruction that declares one to the instructions that do not. Counted
separately and never folded into the rates above, because their surface was not asked
about them.

Both arms read the same IDL from their own API. Neither is allowed a private input.

## 3. The artifact

One document per program, generated at ingest, stored beside the IDL. It carries exactly
the four things a caller cannot get from the IDL:

1. **The seed recipe, with widths.** `launch = ["launch", admin: pubkey, launch_id: u64 LE]`.
   The width is the load-bearing part: `launch_id` at u8, u16 and u32 each derives a
   different, perfectly valid address, and only u64 is the account that exists.
2. **The recovered recipe, for accounts that cannot state their own.** Carried from the
   sibling instruction that declares it derivably, with provenance saying so. Never
   presented as the program's own word.
3. **Provenance per account** — `extracted` (the IDL says it), `recovered` (a sibling said
   it), `flagged` (nobody can say it). A flagged account is still listed: a developer who
   knows a capability exists and is currently unreachable is better informed than one who
   never knew.
4. **Derivation order.** `launch` before `user_position`, because the second seeds on the
   first. An order is not decoration; it is the difference between a plan and a list.

It carries no response payloads, no user data and no secrets — the same control-plane
promise that lets us ingest a surface unilaterally in the first place.

## 4. Where it plugs in

Their catalogue already has the hook. jurassic_fi's own page says *"No registered
developer — auto-imported from Solana on-chain data"*: there is an ingest step, it runs
without the program's author, and it is exactly where comprehension belongs.

**Not at call time.** A program's IDL is immutable per deployment and a PDA recipe changes
only on redeploy, so computing this per request would recompute an answer settled months
ago — and would couple their uptime to ours for no gain. The right shape is the one the
industry uses for immutable inputs: source maps, package registries, SBOMs. Computed once,
stored, versioned, regenerated when the program changes.

Which also makes it **fail-safe**: if we disappear, their catalogue keeps the last
artifact. Nothing degrades.

## 5. A2A falls out of it, rather than being bolted on

An A2A capability card needs capabilities with honest confidence. Provenance *is* that
field — there is nothing else to invent. `prepare_instruction` already emits the runtime
half of the same idea per account: `pinned` (the program's own word), `derived` (computed),
`supplied` (the caller's claim, and the only one nobody verified).

A card that says "this program can do X" without saying how well we know it is the
guessing this whole layer exists to remove.

## 6. What we do not do, and will not

* **We do not list.** The artifact renders one owner's surface, under their name. The
  moment we publish a catalogue of our own we become the marketplace this repo exists not
  to be.
* **We do not rank.** Aggregate findings can be public; specifics stay private until
  fixed; a ranking of providers never ships (`docs/disclosure-policy.md`).
* **We do not replace their builder.** Measured: our derived accounts handed to *their*
  `build_instruction` produced a jurassic_fi `contribute` that simulates on mainnet at
  21,368 CU, bit-identical to one we built ourselves. Their construction was never wrong.
  We supply one step and hand the rest straight back.

## 7. One integration detail nobody would guess

Rehearsing on a local fork, their builder fetches a blockhash for *its own* network — a
mainnet one, which a fork has never seen (`Blockhash not found`). The obvious fix, handing
their builder the fork's `rpcUrl`, is **correctly refused by their SSRF allowlist**, and we
would not want that relaxed: a localhost URL is precisely what the allowlist exists to
block.

So the blockhash travels the other way — read from the fork, passed as `recentBlockhash`,
which their schema already accepts. Their builder never reaches our machine and the
allowlist stays strict. Worth stating in any integration doc, because the failure reads
like a bug in their builder and is not one.

## 8. What this does not establish

* **Nobody has asked for it.** A coherent artifact and an unvalidated demand, exactly like
  the surface page and the plugin before it.
* **The sample is 15 programs, not 4,400.** The tail is older and messier; the legacy-IDL
  crash we fixed this week is evidence of what it holds.
* **The 0.9% figure is a snapshot of a gap we are actively closing.** Our own upstream PR
  removes part of what it measures. The durable claim is the *class* — a surface that
  states a path without a width — not this instance of it.
* **Nothing of ours runs on their platform.** Everything measured here composes from our
  side only.
* **"Recovered" is a reconstruction, not the program's word.** It should be pinned by a
  chain read before anyone irreversible trusts it, which is what the rehearsal is for.

## 9. The order of the conversation, which is part of the argument

1. **PR #11 merges first.** A clean upstream fix, in their vocabulary, costing them
   nothing.
2. **Then the measurement**, showing what the merge did *not* reach — five refusals that no
   patch can move.
3. **Then the artifact**, as the answer to a gap they can now see in their own code.

Reversed, it reads as a vendor proposing a dependency. In this order it reads as a
contributor reporting what is left. The sequence is not politeness; it is the difference
between a claim they can check and one they have to take on trust.
