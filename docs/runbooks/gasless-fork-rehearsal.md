# Fork rehearsal: a gasless purchase, before any of it costs money

Every command and every output below was run on 2026-09-15 against a real surfpool fork of
mainnet with a real Kora relay. Nothing here is illustrative. Where a number appears, it is
what the machine answered.

The point of this rehearsal is that **mainnet should be the boring repeat**, not the first
attempt. A failed Kora transaction is pure loss with no reimbursement, and the relay's only
real spending cap is its balance.

---

## 0. The thing to understand before you start

**A fork reports mainnet's genesis hash.** Measured:

```
$ curl -s -X POST http://127.0.0.1:8899 -d '{"jsonrpc":"2.0","id":1,"method":"getGenesisHash"}'
  5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d      <- this is mainnet-beta's genesis
```

So you **cannot tell a fork from mainnet by asking the chain**, and neither can Kora: its
`cross_cluster_check` passes on both. The only thing that distinguishes them is the URL you
typed. This is the same lesson `docs/disclosure-policy.md` records from the signing design
that was broken by swapping one string: *internal consistency is not authenticity.*

Practical consequence: **keep two terminals and two shells with different env**, and never
reuse a shell that once held a mainnet RPC URL for a fork run, or the reverse.

---

## 1. Boot the fork

```bash
set -a; source .env; set +a
export RPC="https://mainnet.helius-rpc.com/?api-key=${HELIUS_API_KEY}"

surfpool start --no-tui --no-deploy --rpc-url "$RPC" --port 8899
```

Wait for it rather than sleeping a fixed amount:

```bash
until curl -s --max-time 2 -X POST http://127.0.0.1:8899 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getSlot"}' | grep -q result; do sleep 1; done
```

Observed: up in a few seconds, at slot `447143946` against upstream `447143950`.

---

## 2. A throwaway relay key, fork only

Never the mainnet relay key. Never committed. Mode 600.

```bash
S=<scratchpad>
uv run python - <<'PY'
import json
from solders.keypair import Keypair
kp = Keypair()
open("relay-fork-throwaway.json", "w").write(json.dumps(list(bytes(kp))))
print(kp.pubkey())
PY
chmod 600 relay-fork-throwaway.json
```

`signers.fork.toml` — note `strategy` must be one of `round_robin | random | weighted`.
`single` is not a value and Kora refuses it:

```toml
[signer_pool]
strategy = "round_robin"

[[signers]]
name = "gecko-fork-relay"
type = "memory"
private_key_env = "KORA_FORK_RELAY_KEY"
weight = 1
```

Kora's `memory` signer wants **base58 of the 64-byte keypair**, which is what
`str(Keypair)` gives you (88 chars):

```bash
export KORA_FORK_RELAY_KEY=$(uv run python -c "
import json
from pathlib import Path
from solders.keypair import Keypair
print(str(Keypair.from_bytes(bytes(json.loads(Path('relay-fork-throwaway.json').read_text())))))")
export KORA_API_KEY=fork-rehearsal-not-a-secret
```

---

## 3. Fund the relay and confirm the buyer has nothing

The cheatcode funds on the fork only. `4jccRjip…` is `usdg-nosol-buyer` — it holds USDG and
**zero SOL**, which is the whole point of the exercise.

```bash
RELAY=<the pubkey from step 2>
BUYER=4jccRjipEL8CWje6a9PhEjKAnHgfSS1xTMqsf14KvAT7

curl -s -X POST http://127.0.0.1:8899 -H 'Content-Type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"surfnet_setAccount\",
       \"params\":[\"$RELAY\",{\"lamports\":50000000}]}"
```

Observed:

```
  relay  CfR1NAWH…  50000000 lamports     (0.05 SOL)
  buyer  4jccRjip…         0 lamports     <- cannot pay a fee, by construction
```

---

## 4. Validate the config against the live fork, with the signer

Flag order matters: `--config` and `--rpc-url` are **global**, `--signers-config` belongs to
the subcommand.

```bash
kora --config examples/kora_demo/kora.gecko.toml --rpc-url http://127.0.0.1:8899 \
     config validate-with-rpc --signers-config signers.fork.toml
```

Observed: `✓ Configuration validation successful!` with four informational warnings
(Mock price source, in-memory usage store, fallback disabled, weight ignored under
round_robin). Compare to the shipped default, same binary, which gives **1 hard error and
35 SECURITY warnings** — see `examples/kora_demo/kora.gecko.toml` for the delta table.

---

## 5. Start the relay

```bash
kora --config examples/kora_demo/kora.gecko.toml --rpc-url http://127.0.0.1:8899 \
     rpc start --signers-config signers.fork.toml
```

Observed: `RPC server started on 0.0.0.0:8080`, usage limiting with 1 rule, multi-signer
balance tracking on a 30s interval, `/metrics` enabled.

---

## 6. Prove the hardening on the wire, not in the config file

This is the step people skip. A config that *says* it is locked down and a relay that *is*
are different claims, and only one of them is evidence.

| probe | expect | observed |
|---|---|---|
| `getPayerSigner`, no api key | refuse | **401** |
| `estimateTransactionFee`, no api key | refuse | **401** |
| `getPayerSigner`, with key | serve | **200** |
| `getSupportedTokens`, with key | serve | **200** |
| `signAndSendTransaction`, with key | **refuse** | **405** |
| `transferTransaction`, with key | **refuse** | **405** |

```bash
curl -s -X POST http://127.0.0.1:8080/ -H 'Content-Type: application/json' \
  -H "x-api-key:$KORA_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"getPayerSigner","params":{}}'
```

Two wire facts that are in no Kora document and will waste an afternoon each
(`gecko/kora_surface.py:26-32` records them):

- **Parameters are NAMED.** Positional is refused outright with
  `invalid type: map, expected a string`, the opposite of the JSON-RPC convention.
- **No-arg methods take `{}`**, not omission.

Confirm the signer is the relay you funded, and that Kora is willing to be the payment
address too:

```
{"signer_address":"CfR1NAWH…","payment_address":"CfR1NAWH…"}
```

And that the allowlist is the one you wrote, not the default:

```
{"tokens":["EPjFWdd5…USDC","2u1tszSe…USDG"]}
```

---

## 7. Prepare the purchase with the relay as fee payer

```python
from gecko.prepare_purchase import prepare_purchase_result

out = prepare_purchase_result({
    "store": "geckocoffee",
    "product": "Espresso",
    "buyer": "4jccRjipEL8CWje6a9PhEjKAnHgfSS1xTMqsf14KvAT7",
    "fee_payer": RELAY,                 # the relay pays; the buyer still authorises
    "network": "fork",
    "rpc_url": "http://127.0.0.1:8899",
})
```

Three things must be true of the result, and each has a test behind it:

1. `out["fee_payer"] == RELAY`, and the **bytes agree** — `decode_message(...).fee_payer`
   is the relay, because `account_keys[0]` is the fee payer.
2. `out["gasless"]` exists and says `buyer_pays_network_fee: false`, with **both**
   signatures listed. Absent entirely when the buyer pays their own fee.
3. `out["accounts"]["signer"]` is the **buyer**. The relay appears in no account slot.
   Paying a fee grants no authority over funds.

**The property that makes this safe**: change the fee payer and the binding hash changes,
because the payer sits inside the hashed message body. Measured on identical bytes:

```
  self-paid   ff4a4ba6…
  relay-paid  f69e1541…
```

So a relay that rewrites the payer **invalidates the receipt** instead of quietly landing
different bytes than the ones we attested. `tests/test_prepare_purchase_tool.py` pins this;
if those two hashes ever match, every gasless receipt is worthless.

---

## 8. Two signatures, in this order, and why the order is forced

The bytes need both. **Kora signs first**, and not as a courtesy: with Lighthouse enabled
Kora **appends** a balance assertion to the message before it signs
(`crates/lib/src/lighthouse/assertion.rs`, `add_fee_payer_assertion`, skipped only when
Kora itself sends). So the transaction that comes back is not the one you sent. It is your
instruction plus one Lighthouse `AssertAccountInfo` on the relay's own balance, and every
byte-identical check in this repo refuses it. Correctly.

That is why the sequence is what it is, and why it lives in one function,
`gecko.autonomous_purchase.settle_sponsored`:

1. **Relay signs** (`signTransaction`, never `signAndSend`). `gecko.relay.accept_relay_signature`
   then checks the answer by name: same payer, same blockhash, same signers, every
   instruction we authored byte-identical, and the addition is Lighthouse, read-only,
   introducing no writable account, with a signature in slot 0 that verifies.
2. **The answer is a NEW subject.** Re-simulated with the buyer tracked, re-verified at
   `exact`. The receipt over the original attests nothing about a message with one more
   instruction in it.
3. **The buyer signs, in the `authority` role**, over the extended bytes. That role refuses
   `authority-is-the-fee-payer`, `signer-not-a-required-signer` and, the one that makes
   "gasless" a measured word, `authority-lamports-moved` when the buyer's own delta is not
   zero. The spend gate runs inside and keys the token caps on the buyer, because the
   receipt tracks the buyer. The policy must allowlist the Lighthouse instruction
   (`default_spend_policy(sponsored=True)`), otherwise the gate refuses the relay's
   addition, which is the gate doing its job.
4. **Merge** through `gecko.cosign.merge_signatures` over the relay's message; both
   signatures verify against it or nothing is sent.
5. **We submit.** Kora never broadcasts.

The runner does all of it:

```bash
export KORA_RPC_URL=http://127.0.0.1:8080
export KORA_API_KEY=fork-rehearsal-not-a-secret

# dry run: prepares and verifies, never asks the relay
uv run python scripts/gasless_purchase.py --network fork --rpc-url http://127.0.0.1:8899 \
    --buyer-keypair ~/.gecko/wallets/usdg-nosol-buyer.json --product Espresso

# the real thing, on the fork
uv run python scripts/gasless_purchase.py --network fork --rpc-url http://127.0.0.1:8899 \
    --buyer-keypair ~/.gecko/wallets/usdg-nosol-buyer.json --product Espresso --broadcast
```

A buyer-only signer in the default `fee-payer` role still refuses these bytes with
`fee-payer-not-controlled`. **That is the gate working, not a bug.** The role is authored
out loud on the profile (`signing_as="authority"`), never inferred from the bytes.

## 8b. The buyer's key in PayBox, measured

**2026-09-17, fork, kora-cli built from PR #675, PayBox `sol-default` wallet on `autonomous`,
`scripts/paybox_backend.py` over the SDK CLI. Nothing submitted: the bytes carried a fork
blockhash, which mainnet rejects.**

```
1. prepared: 54,647 CU, binding a1cd0206…, slots [(relay, EMPTY), (GpaLFMwQ, EMPTY)]
2. relay signed: appended ['L2TExMFK'] (Lighthouse), unfilled now ['GpaLFMwQ']
3. PayBox signed in 2537 ms
   - message byte-identical to the relay's: True
   - unfilled slots after PayBox: ()
   - relay signature preserved in slot 0: True
   - both signatures verify over the message: True
```

The one thing this answers that no document does: **PayBox's MPC signs its own slot and
leaves a co-signature already in slot 0 untouched.** So the order relay -> PayBox -> submit
holds with a real custodian, not only with the ephemeral key of section 8. 2.5 s for the
signature is inside the blockhash window with room; `open()` refuses a wallet on
`always_approve`, which is the mode that would not be.

## 9. Judge by what moved, not by what returned

**Measured 2026-09-16, surfpool 1.1.1 fork at slot 447438514, kora-cli 2.2.0-beta.8 with
`examples/kora_demo/kora.gecko.toml`, two runs of `scripts/gasless_purchase.py --network
fork --product Espresso --broadcast`:**

```
  relay (fee payer)  7UkWfQwjVRKWWxQgfGVa5bGDfGykh769asYQNMic3yTE   funded 0.05 SOL
  buyer (ephemeral)  3c71Qu73axHuksDMRcNhciwvQ5DSophM8phRf6gopLkT   funded with tokens only

  LANDED   2BPVWkXPxiUCdGRYkXoKRr3VRZfLJ3aLhyd7gfXHrMgZ7JkHRNLGeSS8egYibG77qoyVeV3efTSLHBsTAWU5JMCn
  CU       simulated 55325  charged 55325
  signers  2  fee payer 7UkWfQwj…
  buyer SOL  None -> None   (None = account never existed)
  relay SOL  moved -10000   (fee 10000)
  buyer tok  moved -100000   store tok moved 100000
  receipt    row #57 'Espresso' price 100000
  reset      4 accounts restored
GASLESS: the buyer's SOL did not move; the relay paid; the ledger balances.
```

The first run (signature `4TatoNV4…`) reported `simulated 54647 charged 55325`: the
production path had simulated the ORIGINAL bytes, and the relay's appended assertion
costs 678 CU. The lane now re-simulates the relay's bytes before the buyer signs, and the
second run agrees to the unit.

**The assertion that matters is the buyer's SOL line.** `None -> None` is stronger than
`0 -> 0`: the buyer's account never existed as a lamport holder, before or after.
Everything else is the purchase working; that line is the gasless part working.

**Why this lane and not the spend gate.** surfpool nulls `preTokenBalances`,
`postTokenBalances` AND the `accounts` snapshot on every simulation (measured 2026-08-30,
`gecko/simulate.py`), so the spend gate refuses `amount-unresolvable` here for a reason
about the node, not the bytes. The fork's one measurement is `gecko.sandbox.rehearse`:
land, then read the ledger. That is what `--network fork` runs. `--network mainnet` runs
`gecko.autonomous_purchase.settle_sponsored`, where the arrays exist and the gate decides.

**Three things the rehearsal found that the offline tests could not** (all fixed on the
way, see the commit log): the Orquestra builder ships a two-signer header over a one-slot
signature array whenever the payer is a relay (`SanitizeFailure`, repaired by
`cosign.normalize_signature_slots`); Kora refuses `signTransaction` without a `user_id`
under free pricing with usage tracking (the client sends the buyer); and the failure
diagnosis blamed the zero-SOL buyer for a fee it was never going to pay.

## 9b. The whole USDG story, gasless, measured

**2026-09-18, surfpool 1.1.1 fork, kora-cli built from PR #675, ephemeral buyer holding
0.11 USDG and nothing else, `scripts/gasless_purchase.py --network fork --product Espresso
--convert-from USDG --broadcast`:**

```
  convert    110000 USDG -> USDC on 9RqDTfwC…  min out 108893
  leg 1      LANDED 4kMU5pCH…  CU 50873   relay SOL -10000
  leg 2      LANDED 2Q8EwH8X…  CU simulated 55325 charged 55325   relay SOL -10000
  buyer SOL  None -> None   (None = account never existed)
  relay SOL  moved -20000 across both legs
GASLESS ROUTE: converted and bought; the buyer's SOL never moved.
```

Both legs relay-paid: Kora signed first each time, `gecko.relay` accepted its Lighthouse
addition, the buyer co-signed its own slot, `cosign` merged, the ledger judged. The trace
graph of the run has nine steps.

**Three things this run found, all fixed on the way:**

1. The swap builder tags its transaction with a Memo instruction; the relay refused the
   whole swap for it. Memo moves nothing; it is now in `allowed_programs`.
2. **The relay's own Token-2022 policy refuses USDG.** `transfer_hook_policy = "deny_all"`
   and `transfer_hook` / `permanent_delegate` in `blocked_mint_extensions` reject a mint
   whose hook authority is mutable, and USDG's is (Paxos). `signTransaction` counts as
   delayed signing, so Kora's default would refuse it too. Only `allow_all` lets the
   convert leg through. The committed config stays closed; the run above used a relaxed
   copy, and the note in `kora.gecko.toml` says what you accept if you relax it on
   mainnet: Paxos's authorities over the two mints you already chose to hold.
3. The transaction id is slot 0, the relay's signature. The co-sign step reported the
   buyer's signature and the confirmation poll asked the node about a transaction that
   does not exist, for 30 s, while the tokens had already moved. Fixed in `_cosign`;
   `_confirm` also reads the landed transaction itself when the status query says nothing.

## 10. Tear down

```bash
pkill -f 'kora .*rpc start'
pkill -f 'surfpool start'
rm -f relay-fork-throwaway.json signers.fork.toml
```

The throwaway key has no value and never touched mainnet, but leaving keypairs on disk is
how one eventually does.

---

## What changes for mainnet, and what must not

**Changes:** the RPC URL, a real relay key held properly rather than a throwaway, a real
`KORA_API_KEY`, and a funded balance you are willing to lose.

**Must not change:** every `fee_payer_policy` flag stays `false`, Lighthouse stays enabled,
`sign_and_send_transaction` and `transfer_transaction` stay off, and the wire probes in
step 6 are re-run **against the live relay** before a single real transaction. A config
file is a claim; 401 and 405 on the wire are evidence.

**The residual risk, named rather than closed.** Kora's validator says `whirlpool` and
`let_me_buy` have *"no dedicated fee-payer instruction parser"* — for a custom program it
can only validate inner instructions to standard ones. So for the two programs this flow
actually calls, `fee_payer_policy` is **not** the backstop. Lighthouse's balance assertion
is, and that is why it is mandatory and why `sign_and_send` must stay off.

**And the cap that actually binds is the balance.** There is no global SOL budget in Kora:
`max_allowed_lamports` is per transaction, `[kora.usage_limit]` is per wallet. Fund the
mainnet relay with what you can afford to lose, and alert on
`[metrics.fee_payer_balance]` — it is the budget, so it is the number to watch.

Per `CLAUDE.md`, the mainnet broadcast is founder-run. Gecko prepares, simulates, and hands
over the command.
