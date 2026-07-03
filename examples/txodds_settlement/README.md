# TxODDS on-chain settlement — the flagship demo

**The bounty:** TxODDS "Prediction Markets and Settlement" (18k USDT, submissions close
July 19 2026). **One demo proves both of Gecko's theses.**

**What it is:** an agent uses **Gecko-comprehended TxLINE** (18 first-call-correct tools
over a painful, auth-gated, on-chain-anchored API) to watch a fixture and fetch the
**3-stage Merkle proof**, then **settles a prediction escrow trustlessly** by mapping that
proof onto `gecko-settlement::settle` → CPI `txoracle::validate_stat` — and **every TxLINE
call is risk-scored** by `gecko.risk` (the comprehension-native security gateway), with a
kill-feed of allow/step-up/block decisions.

| | |
|---|---|
| TxLINE comprehension | 18 ops → 18 first-call-correct tools (auth-gated; `recorded`/$0) |
| Proof → on-chain args | `getApiScoresStat-validation` mapped onto `validate_stat` (ts, summary, sub/main-tree proofs, `StatTerm`) |
| Security gateway | every call risk-scored; a poisoned/malformed call is **blocked** |
| On-chain program | `gecko-settlement` (gecko-programs, mollusk-tested) — settle reverts on a tampered proof |

```bash
uv run python examples/txodds_settlement/demo.py    # the demo, offline $0
uv run pytest examples/txodds_settlement/ -q         # pin the claims
```

**Scope:** this is the off-chain half (comprehend → watch → prove → risk-score → build the
settle args), fully testable offline (matches end after the deadline → `recorded` mode).
Founder-gated: the devnet deploy of `gecko-settlement` + submitting the `SettlePlan` on-chain,
and the demo video. The real-Merkle serialization needs one live-devnet `validate_stat` probe
(the program's mollusk suite verifies the CPI byte-shape via a program double).
