# "All the calls working" — what that means, measured, and everything it needs

Measured 2026-09-11 with `gecko/endpoint_score.py` (#527) and the packaged configs. Every
number below is reproducible offline; none is quoted from a note.

## Where we are

**21 wired cards: 10 ok, 9 weak, 2 blocked.**

Those 11 non-ok cards reduce to **8 distinct defects**, because one bad seed blocks every
card that declares it. This is the whole scale-1 roadmap:

| kind | account | blocks | origin | what it needs |
|---|---|---|---|---|
| ASSUMED le2 | `whirlpool.fee_tier` | 1 | extracted | derive both ways against a live account |
| ASSUMED le2 | `whirlpool.adaptive_fee_tier` | 1 | extracted | same |
| ASSUMED le8 | `meteora.bin_array` | 2 | extracted | same |
| ASSUMED le8 | `jurassic_fi.launch` | 3 | extracted | same |
| UNBINDABLE | `pumpfun.creator_vault` | 3 | **manual** | a resolver at plan time |
| UNBINDABLE | `ore.round` | 1 | **manual** | a resolver at plan time |
| UNBINDABLE | `whirlpool.bundled_position` | 1 | extracted | nobody has looked yet |
| UNBINDABLE | `whirlpool.reward_token_badge` | 1 | extracted | nobody has looked yet |

**The `manual` vs `extracted` split is the useful line, and it separates three kinds of
work.** `manual` means a human already read the source and the seed genuinely needs runtime
data — those are honest gaps, not oversights, and no recipe will fix them. `extracted` means
the IDL was believed and nobody checked.

## The three kinds of work

**A. Four assumed byte orders — cheap, mechanical, and already tooled.**
`check_seed_endianness` (#526) names them. The fix per account: derive both little- and
big-endian against a live account and keep the one that exists. Raydium's `amm_config`
proved the method — big-endian matched 8 of 8 on chain, little-endian 1 of 8, and that one
was index 0 where both encodings are the same two zero bytes. Result is recorded as
`recovered` or `manual` with a `why`, which is what moves a card from weak to ok.

Structural confirmation from the callable-path subgraph: `provider_config -> pda` is the
strongest edge at 45. **A seed fix is a config/overlay edit plus a verification, not a code
change.**

**B. Two honest gaps that need a resolver, not a recipe.** `pumpfun.creator_vault` and
`ore.round` are already `manual` — a human looked and the seed depends on runtime data. The
work is a plan-time resolver that reads the account, not a better static recipe. This is the
only item here that touches `gecko/` meaningfully.

**C. Two nobody has looked at.** `whirlpool.bundled_position`, `reward_token_badge`. Both
surface-only — no swap needs them, which is why `whirlpool.swap_v2` is `ok` and lands real
mainnet transactions. Cheapest honest outcome may be to declare them out of scope rather
than recover them.

## What we cannot say yet, and it is the bigger half

**Scale 1 is 21 cards. The catalog is 4,534 programs.** What fraction of THOSE are callable
is unmeasured. The "335 of 1,888 instructions blocked" figure in our notes is **unsourced** —
grep finds it nowhere, and there is no script that produces it.

What we do have, reproducible since #525: `correlate_catalog.py --live --limit 60` prints
**56 of 60 programs build a graph**. That is comprehension coverage, not callability. The
callable rate at catalog scale has never been measured.

**That measurement is a roadmap item, not a footnote.** Without it "all the calls working"
has no denominator, and we would be fixing 8 defects against an unknown.

## Everything we need, in order

1. **Measure the callable rate at catalog scale.** Extend `endpoint_score` to run over
   comprehended-but-unwired programs and report the distribution. Bounded like #525's
   `--limit`, because each program costs a partner IDL fetch. Until this lands the roadmap
   has no size.
2. **0a — Orquestra through the existing retrieval arms, unlabelled** (#531). The harness
   that decides retriever adoption contains zero occurrences of `orquestra`, `program`,
   `solana` or `find_start`.
3. **The four assumed byte orders (A).** Mechanical, tooled, config-only. Moves 7 of the 11
   non-ok cards.
4. **The reachability gate** (#531). Five instances this week of a capability that works and
   cannot be found.
5. **The two resolvers (B).** `creator_vault`, `round`. Real `gecko/` work.
6. **Raydium CLMM**, which now walks into a repo that can tell it what is missing.
7. **0b — archetype labels**, separately and reviewed at #529's trust class.
8. **Decide C**: recover the two whirlpool surface accounts, or declare them out of scope.

## The honest headline

If all eight defects were fixed tomorrow, the claim would be *"every call our 8 wired
programs expose is derivable"* — **not** *"all the calls work"*. The second claim needs item
1, and item 1 has never been run.

Say the first. It is true, it is checkable by anyone with `endpoint_score`, and it is the
one a provider can act on.
