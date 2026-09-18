# The mainnet relay: fund it, probe it, land one purchase, watch the balance

Everything here is the mainnet repeat of `gasless-fork-rehearsal.md`. The fork run landed
(`2BPVWkXP…`, buyer SOL `None -> None`, simulated == charged 55,325) and PayBox signed the
relay-extended bytes with the relay's signature intact (section 8b). Mainnet changes the RPC
URL, the key, and the money. Nothing else may change.

## 0. What the relay is, and what caps it

Kora signs as fee payer so the buyer needs no SOL. Its only real spending cap is its
balance: `max_allowed_lamports` is per transaction, `[kora.usage_limit]` is per wallet, and
there is no global budget. Fund it with what you can afford to lose. The config in
`examples/kora_demo/kora.gecko.toml` keeps every `fee_payer_policy` flag off, Lighthouse on,
`signAndSend` and `transfer` disabled, an API key on every call, and `let_me_buy` plus
Whirlpool as the only custom programs (Kora has no fee-payer parser for either; Lighthouse's
balance assertion is the backstop, and `gecko/relay.py` refuses an answer without it).

## 1. The key, generated off the box

```bash
uv run python - <<'PY'
from solders.keypair import Keypair
kp = Keypair()
print("pubkey     ", kp.pubkey())
print("KORA_RELAY_KEY (base58, 88 chars) printed to the terminal only:")
print(str(kp))
PY
```

Put the base58 string in the host's `examples/kora_demo/.env` as `KORA_RELAY_KEY`, by hand,
over a shell you trust. Never in a chat, a ticket, or a commit. Fund the pubkey with the
first budget (0.05 SOL is 5,000 espressos at the measured 10,000-lamport fee).

## 2. Start it

On the host (the ECS account that runs `surfcall-mcp-ecs`, or any VM with Docker and a
checkout of `../kora` beside this repo at `kora-cli-v2.2.0-beta.8`, or a later tag once
upstream PR #675 ships):

```bash
cd examples/kora_demo
cp .env.example .env                       # RPC_URL (Helius), KORA_API_KEY (long random), KORA_RELAY_KEY
cp signers.memory.toml.example signers.toml
docker compose up -d --build
docker compose logs -f kora                # "RPC server started on 0.0.0.0:8080"
```

`KORA_API_KEY` is the credential Gecko's settle path presents in `x-api-key`. Generate it
with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`. Port 8080 is loopback
only; Gecko runs on the same host, or over a private network you add to the compose file.

## 3. Probe the wire before the first coin

A config is a claim; 401 and 405 are evidence. Same six probes as the fork runbook, against
the live node:

```bash
export KORA_API_KEY=...            # the same value as in .env
K=http://127.0.0.1:8080/
for M in getPayerSigner getSupportedTokens; do
  curl -s -o /dev/null -w "$M with key -> %{http_code}\n" -X POST $K -H 'Content-Type: application/json' \
    -H "x-api-key:$KORA_API_KEY" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$M\",\"params\":{}}"
done
for M in signAndSendTransaction transferTransaction; do
  curl -s -o /dev/null -w "$M with key -> %{http_code} (must be 405)\n" -X POST $K -H 'Content-Type: application/json' \
    -H "x-api-key:$KORA_API_KEY" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$M\",\"params\":{}}"
done
curl -s -o /dev/null -w "getPayerSigner no key -> %{http_code} (must be 401)\n" -X POST $K \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"getPayerSigner","params":{}}'
```

Expected: 200, 200, 405, 405, 401. Then confirm the signer is the key you funded:

```bash
curl -s -X POST $K -H 'Content-Type: application/json' -H "x-api-key:$KORA_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"getPayerSigner","params":{}}'
```

## 4. The first purchase

The buyer is a PayBox wallet on `autonomous` mode holding the product's mint (USDC for
Espresso). The runner prepares with the relay as payer, asks Kora to sign, re-verifies the
extended bytes, asks PayBox to sign as authority, merges, sends, and judges by the ledger.
Nothing here is new; it is the fork run with a different URL.

```bash
set -a; source .env; set +a                          # HELIUS_API_KEY, PAYBOX_TOKEN, PAYBOX_SIGNIN_KEY
export KORA_RPC_URL=http://127.0.0.1:8080 KORA_API_KEY=...
uv run python scripts/gasless_purchase.py --network mainnet \
  --rpc-url "https://mainnet.helius-rpc.com/?api-key=$HELIUS_API_KEY" \
  --signer paybox --product Espresso --max-spend-usdc 0.15 \
  --trace private/runs/first-gasless.jsonl --graph private/runs/first-gasless.html   # dry run first
# add --broadcast to land it
```

Per `CLAUDE.md` the broadcast is founder-typed. The ledger row it writes carries `payer` =
the relay; the graph it draws is the evidence for the runbook.

## 5. Watch the balance

Kora exports Prometheus at `/metrics` on the same port (`[metrics]` in the config), with the
fee payer's balance every 30 s (`[metrics.fee_payer_balance]`):

```bash
curl -s -H "x-api-key:$KORA_API_KEY" http://127.0.0.1:8080/metrics | grep signer_balance
```

Alert when it drops under the budget you set aside for one day of purchases; refill or
stop the container. A relay that runs dry fails every purchase with a clear refusal, which
is the right failure. A relay that runs full is the one to watch.

## 6. Rotate

When the demo is done: `docker compose down`, sweep the remaining SOL back, delete
`.env`. The key was only ever in that file and in the container's environment.

## What must not change from the fork

Every `fee_payer_policy` flag stays `false`, Lighthouse stays enabled,
`sign_and_send_transaction` and `transfer_transaction` stay off, the API key stays on, and
the six probes are re-run after every config change. The residual is the same as on the
fork: no fee-payer parser for the custom programs, so Lighthouse is the backstop, and
`gecko/relay.py` refuses any relay answer that does not carry it.
