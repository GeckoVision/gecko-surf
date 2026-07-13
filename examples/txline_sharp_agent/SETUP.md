# TxLINE access — the quick setup

**Short version:** the whole demo runs **$0 in recorded mode with no key at all**. You only
need the steps below for **live** data.

## There's no "API key page"

TxLINE access is **an on-chain subscription**, not a website signup. There is no dashboard to
copy a key from. Access is a wallet action + a short handshake:

1. `subscribe(service_level, weeks)` — an **on-chain Anchor instruction paid in USDC**, signed
   by *your* wallet.
2. `POST /auth/guest/start` → a guest JWT.
3. your wallet **signs** a message.
4. `POST /api/token/activate` → the **two tokens** (`Authorization` + `X-Api-Token`).

## What we automate vs. what only you can do

| Step | Who | Automated? |
|---|---|---|
| Build the subscribe tx, guest-start, activate, seal the two tokens | **Gecko / this repo** | ✅ `scripts/subscribe.py` |
| **Fund a wallet with USDC and sign/broadcast** the subscribe tx | **You** | ❌ your money, your signature |

Gecko never signs or holds funds — the on-chain subscribe is your own signed action. Everything
around it (the API calls, token activation, sealing the tokens in your OS keychain) we handle.

## Steps (live only)

```bash
# 0. recorded mode needs NONE of this — skip unless you want live data.

# 1. Fund a Solana wallet with USDC. Point the subscriber keypair at it:
#    ~/.gecko/wallets/txodds-subscriber.json

# 2. SIMULATE the subscribe on mainnet first (no funds moved, no signature broadcast):
uv run --with solders --with httpx python scripts/subscribe.py            # simulate → "PASS"

# 3. When PASS, YOU broadcast the real subscription (this spends USDC and is your signed tx):
uv run --with solders --with httpx python scripts/subscribe.py --broadcast

#    …the script then runs guest-start → sign → activate and prints the two tokens.

# 4. Seal them so the agent uses them invisibly (never in mcp.json):
gecko auth set txline
```

Then wire TxLINE **live**:

```bash
gecko add examples/txline_demo/spec/txline_openapi.yaml \
  --base-url https://txline.txodds.com --auth-keychain txline --mode live
```

See `scripts/SUBSCRIBE.md` for the full on-chain detail.
