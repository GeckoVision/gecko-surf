# What correlates across a catalogue of 4,400 programs

**Status:** measurement, 2026-08-17. 56 program graphs built from a live public catalogue.
Reproduce with the script in this session's scratchpad; every figure below is counted, none
estimated.

---

## The question, and why it is the real one

A single first-call-correct answer is worth something. It is not the product. The product is
the thing that knows **what connects to what** — which call must precede which, which output
feeds which input, which two surfaces are talking about the same object. So the question to
answer with data rather than conviction is:

> **Across thousands of programs, what actually correlates?**

This repo has had an answer written down for months — *a join basis is a value domain, never
a name* — and until now it was a design decision. Here it is a measurement.

## 1. Names do not correlate. They collide.

The most-shared account names across 56 programs:

| account name | programs | as a PDA | as a plain account |
|---|---|---|---|
| `payer` | 35 | 0 | 227 |
| `system_program` | 31 | 0 | 351 |
| `token_program` | 29 | 0 | 239 |
| `owner` | 25 | 0 | 212 |
| `mint` | 23 | 0 | 73 |
| **`authority`** | **22** | **24** | **207** |
| **`user`** | **15** | **14** | **81** |
| **`event_authority`** | **12** | **271** | **10** |

**277 account names are used by more than one program. 44 of them — 15% — are a derived
address in one program and a plain account in another.** `event_authority` is the sharpest:
a PDA 271 times and a plain account 10 times. `authority`, the second most common name in the
whole catalogue, is ambiguous in exactly this way.

A join on the name fuses those. It does not fuse them *usually* — it fuses them **silently**,
and the result is a well-formed address belonging to someone else, which is the one failure
mode nothing downstream catches.

### And the string join fails before the semantics do

`system_program` appears in 31 programs. `systemProgram` appears in 18. `token_program` in 29,
`tokenProgram` in 15. **An exact-name join over this catalogue misses roughly a third of its
own matches on casing convention alone** — before anyone asks whether the two things mean the
same thing.

## 2. Addresses correlate. They are the real edges.

What programs genuinely share is pinned program IDs — a hardcoded address in an account slot
is a declared dependency rather than an inferred one:

```
11111111111111111111111111111111              306   system        ← the floor
TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA   176   spl-token     ← the floor
ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL  110   associated    ← the floor
whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc    66   ← a real shared dependency
MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr    34   ← a real shared dependency
6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P    18   ← a real shared dependency
```

The first three are the runtime, and counting them as correlation is counting the floor.
**What is left after removing the floor is the interesting graph** — and it is small, explicit,
and machine-readable. Nobody has to infer it.

## 3. Seed vocabulary correlates, within families

Constant PDA seeds shared by more than one program:

```
'__event_authority'  11 programs      'pool'         6     'position'    6
'pool_vault'          5 programs      'observation'  5     'tick_array'  4
'amm_config'          4 programs      'bonding-curve' 3
```

32 distinct constant seeds appear in more than one program. The clustering is real and it is
**protocol-family shaped**: `tick_array`, `observation`, `amm_config` and `pool_vault` recur
across concentrated-liquidity AMMs because those programs are variations on one design. This
is the correlation that a catalogue can genuinely exploit — *this program is shaped like that
family, so the calls that work there probably work here.*

## 4. The finding that matters most: arguments disagree about their own type

Argument names carrying more than one declared type across the catalogue:

| argument | types seen |
|---|---|
| `index` | `u32`×21, `u8`×11, `u16`×11 |
| `nonce` | `u64`×19, `array`×9, `u8`×1 |
| `reward_index` | `u8`×20, `u64`×7 |
| `token` | `pubkey`×13, `u16`×3, `u64`×1 |
| `amount` | `u64`×76, `array`×1, `option`×1 |

**`index` is a `u32` in one program, a `u8` in another and a `u16` in a third.** These
arguments are used as PDA seeds. A numeric seed is encoded little-endian **at its declared
width**, so the same name at the wrong width derives a completely different address — and
derives it successfully.

That is the whole argument for value domains in one row of a table. A name tells you nothing
about how many bytes to write.

## What this means for the product

* **A catalogue that joins on names is wrong 15% of the time by construction**, and misses a
  third of its true matches on casing before that. Nobody notices, because every wrong answer
  is a valid address.
* **The genuine cross-program graph is smaller and better than the name graph**, and it is
  already declared: pinned addresses, shared seed vocabulary, and the value domain of every
  argument. It needs recovering, not inventing.
* **This is what an ingestion tool is for.** One program comprehended is a call. A catalogue
  comprehended is a graph in which "what else works like this" is answerable — and that
  question is the one an agent builder actually has.
* **It generalises past Solana.** The same three findings restate as: field names collide,
  declared references are the real edges, and a name carries no type. Any catalogue of APIs
  has that shape.

## What this does not establish

* **56 programs, not 4,400.** The sample is the top of a live catalogue by usage; the long
  tail is unmeasured and will be older and messier, not cleaner.
* **Shared seed vocabulary is not proof of a shared interface.** `pool` meaning the same thing
  in two AMMs is an inference this measurement supports and does not demonstrate; two programs
  using the word is evidence of a family, not of a compatible call.
* **No correlation was acted on.** This measures what could be joined. It does not show that
  joining it makes an agent more correct — that would need the before/after harness pointed at
  a multi-program task, and no such golden set exists.
