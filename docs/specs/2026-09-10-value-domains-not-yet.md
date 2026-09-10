# Phase 3, refused on measurement: a value domain cannot be inferred, and inferring it
# would teach our own eval

Phase 3 of the catalog plan was to wire value-domain recognition so `usdg`, `convert` and
`stablecoin` stop reaching zero cards, repaying what `tests/test_retrieval_eval.py:439`
calls the card debt. I measured it before building it. **It should not be built as
designed**, and both the positive and the negative form fail for different reasons.

## What the gap actually is today

Measured 2026-09-10 with `gecko.vocab_gap` on the founder's own phrasing from this
session — not on the eval fixture:

| token | cards reached |
|---|---|
| `stablecoin` | **0** |
| `hyusd` | **0** |
| `usdg` | 1 |
| `usdc` | 4 |
| `sol` | 4 |
| `swap` | 6 |

Better than the record suggests (`usdg` was 0 when the gap was first written), and
`stablecoin` and `hyusd` still reach nothing.

## Why the positive form is disqualified

The plan was: a card that deals in `solana-token-mint` gains its members as terms, so
"swap usdg" reaches every card that accepts a caller-named token.

**13 of the 52 golden intents contain a token symbol, and 2 of those are out-of-scope
floor rows** — including `"send ten usdc to my friend's wallet"`, the row whose false
accept was fixed on 2026-09-08. So adding `usdc` / `sol` / `usdg` as card terms would:

1. **inflate recall on 11 in-scope rows by teaching the eval its answers** — the exact
   failure `test_retrieval_eval.py:439` names: *"what must NOT repay it is pasting this
   fixture's vocabulary into the config, which would make the eval score itself"*; and
2. **push the floor back down on 2 out-of-scope rows**, undoing Phase 0.

A change that moves our own scorecard in both directions at once cannot be evaluated by
that scorecard. That alone disqualifies it.

## Why the negative form does not rescue it

Used as a FILTER — exclude cards that cannot take a named mint — the contamination
problem disappears: removing wrong answers teaches no right ones, and fewer candidates can
only help the floor. But the filter needs to know which cards accept a caller-named token,
and **nothing declares that**. Both available proxies are inference, and both are wrong:

* **By account name** (any slot matching `mint` / `token_a` / `token_b`): **15 of 21
  cards**, including `pumpfun.mint_authority` — an account the program DERIVES, not one a
  caller chooses. Matching `usdg` against it is a false positive of exactly the shape
  Phase 0 removed.
* **By declared input**: **8 of 21 cards**, and it wrongly excludes `jurassic_fi.contribute`
  on two in-scope rows (*"contribute usdc to the jurassic finance token sale"*, *"back the
  triceratops skull sale with usdc"*). That card genuinely accepts USDC and declares it as
  an ACCOUNT rather than an input.

Too broad breaks the floor; too narrow drops real answers. There is no third proxy.

## What would work, and what it costs

The blocker is that **a card's accepted value domain is not declared anywhere**, so every
route to it is a guess about the program's intent from the shape of its names.

`StartSpec.surface_named` is the precedent and the model. Its own comment says why it
exists: `extracted` must be EARNED, and falling through to a confident claim for any
unknown string shipped a typo as *"the surface stated this"*. A declared
`value_domains: Mapping[str, str]` — account or input name → domain token, in the
`graph._norm` vocabulary `canonical.py` and `provider_matrix.py` already share — is the
same trust class: an affirmative, reviewed, per-program claim.

Cost: eight programs, reviewed once each, asserted against the landing orchestrators the
way `tests/test_derive_plan_provenance.py` already asserts provenance.

Then, and only then, the domain is used as a **filter** and never as a term — so it can
remove a wrong venue and can never teach the eval an answer.

## What this does not fix, and should be said plainly

`stablecoin` is a CLASS, not an asset. No mint registry contains it, and deciding that USDC
is a stablecoin is a judgement rather than a fact the chain states. It stays a named gap.

## Reproduce

```bash
uv run python -c "
from gecko.vocab_gap import gap_report, render
print(render(gap_report(['swap some USDG to USDC and then buy an espresso',
                         'convert my stablecoin into what the shop takes'])))"
```
