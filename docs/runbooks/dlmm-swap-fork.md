# DLMM swap on a fork: land it, judge it by what moved

Measured 2026-09-18 against a surfpool 1.1.1 fork of mainnet on `:8899`.

## The command

```bash
uv run python scripts/dlmm_swap_fork.py \
  --rpc-url http://127.0.0.1:8899 \
  --input EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v \
  --output So11111111111111111111111111111111111111112 \
  --bin-step 5 --base-factor 4000 --amount 1000000
```

The pool is derived from the two mints, the bin step and the base factor. The signer
is an ephemeral key that exists only because the endpoint proved it is a fork. The
lane funds it by cheatcode, assembles the same bundle the landing orchestrator
simulates, sizes the compute budget, signs, lands, reads the ledger, and resets the
three accounts it touched.

## The run that landed

```
pool          DNkZFw1VF36K15yoDvgY2QmrDiwrLP5y1DcMEHEPPa1Q  bin arrays [-42, -41, -40]
quote         expected 4267965  floor 4054566
LANDED        41WJsvF49AAJMxPRLcF5XVdYFhQ75QmX8eP5UswmyiNDSaE8VM6yyZfZRR1YN4HerhXfv4hYCjJB9dNcsppXeLML
CU            simulated 73931  limit 88717  charged 73931
token out     None -> None  (received 4265562 by native-sol)
signer SOL    moved 4260562  (fee 5000)
reset         3 accounts restored
```

1 USDC in, 0.004265562 SOL out, above the floor, simulated equals charged.

## Three things the run taught

**A SOL output has no token account to read.** The bundle's postlude closes the wSOL
account, so the output arrives as native lamports. The judge now reads the signer's
balance and adds the fee back (`received_witness = "native-sol"`). Before the fix the
same landed run reported `received None` and a false discrepancy.

**The first two attempts failed for reasons the quote cannot see.** Selling SOL into
this pool answered `6036 BitmapExtensionAccountIsNotProvided`, then `6037
CannotFindNonZeroLiquidityBinArrayId`. The first was ours: the plan now passes the
bitmap extension account when it exists. The second was the pool: it holds 0.94 SOL and
zero USDC across three arrays, all at or above the active bin, so selling SOL has
nothing to take. The state-quoted price is spot price times amount and never reads
the bins, so it cannot refuse a one-sided pool. A pre-check that reads the output-side
amounts in the selected arrays would turn 6037 into a named refusal. Not built yet.

**"Deep" is not a property of the parameters.** Bin step 5 and base factor 4000
resolve to this thin one-sided pool, not the main SOL/USDC market. Pick the pool by
reading its bins, not by guessing its fee tier.

## What surfpool cannot witness

Token balance arrays and the account snapshot are null on surfpool 1.1.1, so a token
output is read from the ATA before and after, and a SOL output from the signer's
lamports. The fork's pool state is not reset between runs; the second run on the same
pool quotes less because the first one moved the active bin.
