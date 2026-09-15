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

## 8. Two signatures, in this order

The bytes now need both, and **order matters for Lighthouse**.

1. **Kora signs as fee payer** via `signTransaction` (never `signAndSend` — it is disabled
   here, and Lighthouse does not apply to it).
2. **The buyer signs as token authority.**
3. **We submit.** Kora never broadcasts.

Expect `gecko/signer.py` to refuse these bytes if you hand them to a buyer-only signer:
code `fee-payer-not-controlled`. **That is the gate working, not a bug** — the tool schema
says so out loud, and a caller who does not know it reads it as our failure.

---

## 9. Judge by what moved, not by what returned

```
  buyer SOL before : 0
  buyer SOL after  : 0          <- the claim. If this moved, it was not gasless.
  relay SOL before : 50000000
  relay SOL after  : 50000000 - (base fee + priority)
  buyer USDG       : down by the espresso price + the swap
  buyer USDC       : the swap output, less what the store took
```

**The assertion that matters is `buyer SOL after == buyer SOL before == 0`.** Everything
else is the purchase working; that line is the gasless part working.

---

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
