# jurassic_fi: six of eight instructions cannot derive their own root account

**Status:** measured 2026-08-17 against mainnet, through Orquestra's MCP and our own graph.
Every address below is reproducible. Nothing was signed and nothing was sent.

**Program:** `raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm` (`jurassic_fi_token_sale`)

---

## What this is a showcase of

Not "here is a bug in a hyped program." The program is fine — it validates everything it
should. This is a showcase of **the gap between what a surface declares and what a caller can
do with it**, on a live launch holding real money, found in about ten minutes.

## The finding

Every instruction that touches a launch needs the `launch` PDA. Seven of the eight declare its
seeds like this:

```json
{"kind": "const",   "value": [108,97,117,110,99,104]},        // "launch"
{"kind": "account", "path": "launch.admin",     "account": "Launch"},
{"kind": "account", "path": "launch.launch_id", "account": "Launch"}
```

**The seeds are fields stored inside the account being derived.** To compute `launch` you need
`launch.admin` and `launch.launch_id`, which live in `launch`.

That is correct for the *program* — it holds the account, reads the fields, and checks the
address matches. It is a dead end for a *caller*, who has none of it. Our graph marks the
account unresolvable and reports the cycle rather than guessing, which is the honest answer and
also an unhelpful one.

And because three more accounts seed on `launch`:

```
user_position  = ["user_position", launch, contributor]
payment_vault  = [launch, token_program, payment_mint]
token_vault    = [launch, token_program, token_mint]
```

**the whole account graph is unreachable from the IDL.** Measured across the program:

| instruction | accounts | PDAs | unresolvable |
|---|---|---|---|
| `claim` | 8 | 4 | 3 |
| `contribute` | 8 | 3 | 3 |
| `refund` | 9 | 4 | 4 |
| `settle_success` | 12 | 3 | 3 |
| `fail_pending_settlement` | 5 | 2 | 2 |
| `initialize_launch` | 15 | 6 | 3 |
| `initialize_payment_mint_allowlist` | 4 | 1 | **0** |
| `remove_payment_mint_allowlist` | 3 | 1 | **0** |

The two instructions a builder does not care about are the two that are fully derivable.

## The recovery, and it was hiding in plain sight

`initialize_launch` declares the *same* PDA differently:

```
initialize_launch:  "launch" · `admin` (account) · `params.launch_id` (arg)   ← derivable
every other:        "launch" · `launch.admin`    · `launch.launch_id`         ← self-referential
```

The creation instruction has to state a derivable recipe, because at creation there is no
account to read from. **So the recipe exists, in exactly one place, and nothing carries it
across.**

Applied to a live launch — `admin` and `launch_id` read from the account itself, then re-derived
independently:

```
seeds = ["launch", admin_pubkey_bytes, launch_id as u64 little-endian]

  launch_id as u8   -> 33DDaYBpsCuKAKnbQGZxRBnwrLDi2fTNwKqBkfspTTeh
  launch_id as u16  -> EEVxnNuqRyfKkRjXZnpEp4m8pA4c51DyJqWRk2W9Lu3s
  launch_id as u32  -> D7NR5ext4hJEYnhvjW11vdvMY9ex1D8Qtqfs6odAiJPq
  launch_id as u64  -> 9nFKKFBEVW4njBtyJkcvngEkmT3qVXm4fgGGRBLbqH65   <-- the live account
```

That last address is a real launch on mainnet: `DEATON` / `TRCH1`, **323,816 USDC raised**
against a 660,000 cap, state `Open`, decoded through Orquestra's own `get_program_data`.

**Note the other three lines.** `u8`, `u16` and `u32` each produce a perfectly valid address
that belongs to nothing. This is the correlation study's sharpest finding appearing live: a
numeric seed is encoded at its declared width, `launch_id` is one name, and three of four
plausible readings derive successfully and wrongly.

## What this changes in our own code

A concrete, shippable comprehension improvement, and the first thing to build after the
showcase:

> **When a PDA name is declared resolvably in one instruction and self-referentially in
> others, the resolvable recipe is the truth and should propagate to the others.**

`gecko/program_graph.py` builds each instruction's account list independently, so `launch` is
correctly unresolvable in `claim` and correctly resolvable in `initialize_launch`, and the two
never meet. Carrying the recipe across turns **six uncallable instructions into callable
ones** on this program alone — and this pattern is not rare, because every Anchor program that
stores its own seed values has it.

It must carry provenance, not silently upgrade: the recipe is `recovered`, from a sibling
instruction, and the account it derives should be pinned by a chain read before anyone trusts
it. That is what we did by hand above and it is what the code should do.

## What a build-kit would give a developer here

Everything below is derived, none of it authored:

* **the recipe** — `["launch", admin, launch_id: u64 LE]`, with the width stated, because
  three widths derive wrong addresses successfully;
* **the order** — `launch` first, then `user_position`, `payment_vault`, `token_vault`, all of
  which need it;
* **the state machine** — `terminal_state` is `Open` here, and the errors name the rest:
  `LaunchNotActive`, `LaunchNotOpen`, `LaunchNotSettlementReady`, `LaunchNotSuccessful`,
  `LaunchNotRefundable`. Five of the 32 declared errors are state guards, so which call is
  legal depends entirely on where a launch sits;
* **the two instructions the documented flows never mention** — `fail_pending_settlement` and
  `remove_payment_mint_allowlist`. The first is how a launch *enters* the state where refunds
  become possible, which makes the documented refund flow incomplete: it tells a contributor
  how to be refunded and not how a launch becomes refundable;
* **the partial fill** — `contribute` takes `requested_amount` AND `min_accepted_amount`, so a
  contribution can be partly filled. An agent that ignores the second argument is not
  expressing an intent it certainly has.

## What this does not establish

* **Not a defect in Orquestra.** Their surface reports the seeds exactly as the IDL declares
  them, including the self-referential path, and their `list_pda_accounts` shows both variants
  side by side — which is how the recovery was found. Reporting a self-referential seed
  faithfully is correct behaviour.
* **Not a defect in jurassic_fi.** The program validates what it should. A seed that reads the
  account's own fields is a normal Anchor pattern and a runtime check, not a mistake.
* **The 32 error codes are declared, not verified.** We have not checked which are actually
  raised — that needs source, which we do not have for this program. `StoreNotEmpty` on another
  program was declared and never raised, so the check matters.
* **Nothing was simulated.** This is derivation and decoding only. Whether these instructions
  build and simulate is the next question and the score's actual job.
