# TxLINE Sharp Movement Detector — start a new Solana project with Gecko

A working example of the whole Gecko thesis on one *painful, paywalled* API: point Gecko at
the **TxLINE** World Cup odds feed, and your agent gets first-call-correct odds tools with
**zero integration code** — no client to hand-write, no reading TxLINE's docs, no wrestling
its two-token on-chain auth. The agent then monitors the feed and flags **sharp moves**
(implied-probability shifts) — the signal a trading tool or an on-chain prediction market
acts on.

Runs **$0 in recorded mode** (synthetic feed, no keys, no network). Goes **live** with a
TxLINE subscription. Built for the Superteam **Trading Tools & Agents** and **Prediction
Markets & Settlement** tracks.

**See the whole use case in one run** — wallet setup → comprehend → subscribe → flag → settle:

```bash
uv run python -m examples.txline_sharp_agent.story
```

The user does 3 acts (fund · set up · authorize a policy); the agent does everything else,
`$0`, offline. The rest of this page breaks that story into its parts.

---

## The 3 steps

**1 · Connect** — comprehend the API (one line, no Python):

```bash
npx @geckovision/gecko add \
  examples/txline_demo/spec/txline_openapi.yaml \
  --base-url https://txline.txodds.com --mode recorded
```

Gecko turns 18 TxLINE operations into first-call-correct tools. Auth-gated odds tools stay
hidden until a session can satisfy them, so the agent can't mis-call what it can't see.

**2 · Secure** — your key is sealed in the OS keychain, never in `mcp.json`. Recorded mode
needs no key at all; for live data, seal your TxLINE session (see [SETUP.md](SETUP.md)):

```bash
gecko auth set txline    # only for live — recorded is $0 and keyless
```

**3 · Execute** — the agent reads the feed and flags sharp moves:

```bash
uv run python -m examples.txline_sharp_agent.demo
```

```text
t2  Home 45.600%  Draw 27.000%  Away 27.400%
t3  Home 54.800%  Draw 27.000%  Away 18.200%   ⚡ SHARP
      └─ [1x2] fixture 42 · Pinnacle · Home: 45.600% → 54.800% (+9.200 pp, up)
      └─ [1x2] fixture 42 · Pinnacle · Away: 27.400% → 18.200% (-9.200 pp, down)
```

---

## What's inside

| File | Role |
|---|---|
| `detector.py` | Pure logic: `OddsPayload` snapshots → `SharpMove` signals (threshold on implied-prob deltas). No network. |
| `synthetic.py` | A deterministic, schema-valid **synthetic TxLINE feed** — drifts, then one sharp move. The local-simulation half. |
| `surfcall_tools.py` | The Gecko⇄LLM seam: allow-listed TxLINE odds reads only, never-raises, output-capped. |
| `agent.py` | A Claude tool-use loop (injectable LLM) that reasons over a flagged move using the odds tools. Offline-testable. |
| `story.py` | **The whole use case, end to end, one `$0` run** — wallet setup → comprehend → subscribe → flag → settle. |
| `demo.py` | The `$0` recorded showcase — comprehend → first-call-correct call → replay feed → flag the move. |
| `settlement_sim.py` | Day 15 → Day 18 bridge: a flagged move → the risk-scored on-chain `validate_stat` settlement plan, `$0`. |
| `wallet_sim.py` | Agentic wallet in the middle: the 3-step user surface (fund · set up · authorize a policy); the wallet signs only within the policy. `$0`, `WalletSeam`-pluggable. |
| `.claude/agents/` | Curated Solana agents (`defi-engineer`, `solana-architect`, `solana-qa-engineer`) for the settlement build. See `NOTICE.md`. |
| `.mcp.json` | Surfpool (local mainnet-fork) + solana-dev MCP servers. |
| `tests/` | Detector logic, first-call-correctness, and the agent loop — all offline. |

## Try the reasoning agent (optional, needs an LLM key)

`demo.py` is deterministic and keyless. For the agent that *reasons* over a move
(`agent.py`), give it an Anthropic client and call `analyze(moves, llm=..., tools=..., model=...)`.
The loop is bounded and testable offline with a fake LLM (see `tests/test_agent.py`).

## Next: settle it on-chain (Prediction Markets track)

A sharp move is a *signal*; the payout is *settlement*. Run the bridge:

```bash
uv run python -m examples.txline_sharp_agent.settlement_sim
```

It chains a flagged move into [`../txodds_settlement`](../txodds_settlement), where an agent
(every call **risk-scored** by the security gateway) pulls TxLINE's **3-stage Merkle proof** and
maps it onto the on-chain `validate_stat` settlement instruction — the program never decides the
outcome, the proof does. Runs `$0` recorded.

**Local mainnet-fork simulation:** that instruction is profiled on **Surfpool** by
[`gecko-programs`](https://github.com/GeckoVision/gecko-programs) — `FakeSurfpool` (offline,
`$0`, the CI path) or `RpcSurfpool` (founder-run). Both **profile** the transaction and **never
sign or broadcast** — a real mainnet settle is always the user's own signed action. The bundled
`.claude/agents/solana-qa-engineer` + the Surfpool MCP drive that fork.

## Agentic wallet in the middle — 3 user steps

```bash
uv run python -m examples.txline_sharp_agent.wallet_sim
```

The user's *entire* on-chain surface collapses to three acts — **fund** a wallet, **set it up**
once, **authorize one policy** ("spend ≤ $X for {subscription, settlement}"). The agent (Gecko
the brain) comprehends TxLINE, builds the correct subscribe + settle transactions, and hands
each to the wallet, which **signs only within the policy** — an over-cap request is refused.
Gecko never holds keys or funds.

`WalletSeam` is the injected boundary (like `gecko.access.Session`). `SandboxWallet` models a
`$0` ephemeral wallet (`pay --sandbox`) for the offline demo. **Recommended mainnet hands:
Privy** (Solana instruction-level policy enforced in-enclave; OKX OnchainOS is a valid
alternate), with **pay.sh as the x402 rail** on top — the wallet signs, the rail settles.
MagicBlock is deferred (session keys bind only if the counterparty program integrates them).
Same tx-building code path, only the signer edge changes.

## Demo-day mapping

- **Day 15 — Trading Tools & Agents:** this detector + agent (a deployable standalone tool).
- **Day 18 — Prediction Markets & Settlement:** `../txodds_settlement` + the Surfpool sim above.

Fork this: `gecko add <your-api>` and your agent is calling it correctly, first try.
