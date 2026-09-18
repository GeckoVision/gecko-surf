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

## 2b. Or on ECS, next to the hosted MCP (the way we run it)

The relay is a second Fargate service on the `surfcall-mcp-ecs` stack, behind the SAME load
balancer and certificate as the MCP, on its own HTTPS port. The certificate names one host
(`mcp.geckovision.tech`) and this account owns no DNS zone, so a second hostname would cost a
new certificate and a DNS change for nothing: the relay answers at
`https://mcp.geckovision.tech:8443/`. Its `/liveness` is the health check (Kora leaves it
unauthenticated on purpose); every RPC method needs the API key.

Three parameters under `/gecko-relay/` in SSM, put once, by you, from a machine that is not
the relay. The execution role reads only that prefix; the MCP's role reads only
`/gecko-mcp/*`.

```bash
# 1. the hot key, generated OFF the box (§1 above), pasted once, never written to a file here
aws ssm put-parameter --name /gecko-relay/KORA_RELAY_KEY --type SecureString --value "$(cat relay.b58)" --region us-east-2
# 2. the API key Gecko presents in x-api-key
aws ssm put-parameter --name /gecko-relay/KORA_API_KEY --type SecureString \
  --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" --region us-east-2
# 3. the mainnet RPC the relay simulates and reads through (a Helius URL carries its key: SecureString)
aws ssm put-parameter --name /gecko-relay/RPC_URL --type SecureString --value "$RPC_URL" --region us-east-2
```

Then one command builds Kora from `../kora` at the pinned tag, adds `kora.gecko.toml` and the
memory signer file on top (`examples/kora_demo/Dockerfile.relay`), pushes to ECR
`gecko-relay`, and deploys. The script refuses to deploy while any of the three parameters
is missing, because the task would fail at boot otherwise.

```bash
./infra/deploy-relay.sh                      # --port 8443 --kora-ref kora-cli-v2.2.0-beta.8 are the defaults
aws logs tail /ecs/gecko-relay --follow --region us-east-2   # "RPC server started on 0.0.0.0:8080"
```

Fund the signer (§1) only after the six probes below pass against the public URL:

```bash
KORA_RPC_URL=https://mcp.geckovision.tech:8443/ KORA_API_KEY=... ./infra/probe-relay.sh
```

What the ECS shape changes from compose: nothing in the config. `kora.gecko.toml` is byte
for byte the same file, the caps are the same, and `signAndSend` is still off. What it
adds: the key lives in SSM and reaches the container as an environment variable at boot,
the container has no shell access from outside, and `aws ecs update-service
--desired-count 0` is the off switch.

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

**Measured 2026-09-18, the first one.** Founder-authorized in chat, step by step: the store
wallet funded the relay (0.01 SOL) and the PayBox wallet (0.20 USDC); the dry run passed
(receipt PASS, 48,552 CU, exact binding, two signers); then one broadcast.

```
LANDED   4fjGFCpRS2GR86DiL5LbhndPT8gJ637HM8yLm5FndgYf8tzwDVFsnZvWnq6Xx8Ujtu32x4Xcd1egaSnAy54fzeWW
CU       predicted 49230  charged 49230        (slot 448234184)
signers  2  payer 6Q5Ki322…  authority GpaLFMwQ…
buyer    SOL 22,865,000 -> 22,865,000   USDC 0.20031 -> 0.10031
relay    SOL 10,000,000 -> 9,990,000    (fee 10,000 lamports: two signatures)
store    USDC 4.580028 -> 4.680028
```

Read back from the chain, not from the runner: the transaction carries two programs, the
shop's and Lighthouse's assertion the relay appended, and the buyer's SOL did not move.
Trace and graph: `docs/assets/gasless-mainnet-first-run.trace.jsonl` and
`docs/assets/gasless-mainnet-first-run.html` (steps: sponsor 755 ms, resimulate 755 ms,
verify, sign 2,108 ms by PayBox, merge, send 271 ms, confirm 4,394 ms). The runner's own
"relay SOL after" line read the balance before the fee was visible at the default commitment;
fixed in the same change by reading at `confirmed`.

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
