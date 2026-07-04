# TxODDS bounty — submission overview (endpoints + API experience)

*Draft for the required submission fields. Entry: an agent-driven, trustless on-chain
settlement engine on TxLINE.*

## The idea (core + highlight)
An AI agent uses **Gecko-comprehended TxLINE** to watch a World Cup fixture and pull the
**3-stage Merkle proof**, then settles a prediction escrow **trustlessly** by CPI-ing into
`txoracle::validate_stat` — the program never decides the outcome, the on-chain oracle
proof does. Every TxLINE call is **risk-scored** by a comprehension-native security layer
(poisoned/malformed calls blocked). One demo, two primitives: trustless settlement + an
agent security gateway.

**Business/technical highlight:** the settle instruction cannot be spoofed — a *tampered*
proof makes it revert (proven by the program's negative test). The escrow releases only on
a real, on-chain-verified TxLINE outcome.

## TxLINE endpoints used
- `GET /api/scores/snapshot/{fixtureId}` — live score state for the fixture.
- **`GET /api/scores/stat-validation`** — the 3-stage Merkle proof (`statToProve`,
  `eventStatRoot`, `summary`, `statProof`, `subTreeProof`, `mainTreeProof`), mapped onto
  the on-chain `validate_stat(ts, fixture_summary, fixture_proof, main_tree_proof,
  predicate, stat_a: StatTerm, stat_b?, op?)`. **This is the settlement primitive.**
- (Comprehended the full surface: odds/scores/fixtures snapshot + updates + SSE stream +
  the fixtures/odds validation endpoints — 18 operations → 18 first-call-correct tools.)

## Our experience using the TxLINE API — what we liked, where we hit friction
**Liked (genuinely novel):** the data is cryptographically signed and Merkle-anchored on
Solana with an on-chain `validate_stat` — so you can settle **without trusting the oracle
service**, which most "sports data feed" APIs can't offer. The single normalized JSON
schema across competitions made comprehension clean.

**Friction (and how we handled it — this is where a comprehension layer earns its keep):**
1. **Auth is a multi-step on-chain handshake** (guest JWT → on-chain `subscribe` → sign →
   `activate` → two-token `Authorization` + `X-Api-Token`). An agent handed the raw
   OpenAPI would stall here — it's not a single first call. We drove it as one adapter and
   injected the tokens invisibly (the agent never handled a credential).
2. **The proof→on-chain mapping is non-obvious.** The `stat-validation` response fields
   don't 1:1 the `validate_stat` args — `subTreeProof`→`fixture_proof`, the stat + its
   `eventStatRoot` + `statProof` fold into a `StatTerm`, and the two-stat path uses
   `statKey2`/`statProof2`. Getting this byte-exact is the whole game for the CPI; we
   pinned it in code so the agent produced the correct settle payload first try.
3. **The three-level Merkle hierarchy** (stat → score-update sub-tree → fixture summary →
   batch root) is powerful but under-documented for the on-chain side; the reference
   `github.com/txodds/tx-on-chain` examples were essential.

**Net:** TxLINE's on-chain trust model is excellent and differentiated; the cost is a real
first-call-correctness burden (multi-step auth + a precise proof→CPI mapping). That burden
is exactly what our tooling removes — which is why the agent settled correctly the first
time, with every call security-scored.

*Repo + deployed devnet program + demo video: [to add].*
