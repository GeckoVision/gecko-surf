# Implementation plan — every blocker between here and "users on mainnet"

> **SUPERSEDED 2026-08-12 by `2026-08-12-consolidated-plan.md`. Do not schedule from this
> file.** Three things in it are false and one is unsatisfiable: PR-1's premise about the RPC
> as a trust root is wrong (the SOL leg already is a `getAccountInfo` differential in the
> signing path); PR-1 itself is a drain bypass, blocked; PR-8 and PR-9 were already done when
> this was written; and PR-7's DoD cannot be met with the levers that exist. Kept for the
> record because the corrections are only legible against it.

**Written 2026-08-12, after mainnet tx #12** — the first fully autonomous purchase whose
signing key never existed on our machine (Privy enclave, 36,399 CU predicted = 36,399
consumed, signature `3kcngvKSkfRze7UPc6v1MeeZvxZAR6VG8evoyoA2VGF1eG9MKGzg41838BCGRHqqQBDwQ4FpC8R6qSa18FRTW5wR`).

The purpose of this document is the founder's stated one: *run autonomous and mainnet
tests so we understand **when our simulation actually fails**, and what is missing, before
we let users on mainnet.* Everything below is either something a real run proved, or
something a real run cannot yet reach.

---

## What is PROVEN as of today (do not re-litigate these)

| Claim | Evidence |
|---|---|
| The comprehension → plan → simulate → bind → gate → sign → settle loop runs end to end, autonomously, on mainnet | tx #12, one call, no human in the loop |
| The CU prediction is exact | 12 of 12 mainnet transactions |
| Gecko can hold **no key at all** | `scripts/privy_backend.py`; the key is in Privy's enclave |
| The program-id allowlist exists on BOTH sides of the seam | our `allowed_instructions` + Privy policy rule `jee7y79fr8e5rm520b1xe8bt` |
| The hosted surface serves 4 tools over both transports | `mcp.geckovision.tech/orquestra/mcp` and `/orquestra/sse` |

---

## The blockers, grouped by what they threaten

### A. Simulation lies, or refuses when it should not — the "when does simulation fail" question

**A1 — `balances-null` makes the entire $0 fork lane unsignable. (Confirmed by execution.)**
`gecko/simulate.py` refuses when a token-balance key is present but `null`, and its comment
justifies the cost as: *"null does not occur on the standard mainnet RPC ... so this refusal
costs nothing operationally."* Measured today, same transaction, same code path:

| node | `preTokenBalances` | `token_delta.status` | outcome |
|---|---|---|---|
| `api.mainnet-beta.solana.com` | `[]` | `measured` | signable |
| surfpool 1.1.1 (fork) | `null` | `unmeasurable` | spend gate refuses `amount-unresolvable` |

The refusal DIRECTION is right and must not be relaxed: `null` is the node declining, and
an SPL spend reading as zero outflow defeats the cap. What is wrong is the cost claim — the
fork is the lane Pattern B requires every claim to be falsified in, and nothing can be
signed there. **This also answers STATE.md's open S2 question (`preTokenBalances: null` —
MALFORMED or ABSENT?) with data instead of a decree: it is neither. It is a node that does
not compute token balances during simulation.**

**A2 — the current slot must be read at `processed`, or the signer refuses correctly.**
`getSlot` at the default (finalized) commitment sits ~31 slots BEHIND the slot
`simulateTransaction` ran at, so `TransactionSigner` refuses `receipt-slot-implausible` —
correctly, since a receipt ahead of "now" means the two observations are not of the same
chain. Fixed in `scripts/privy_fork_proof.py`; there is no shared helper, so every future
caller can rediscover it.

**A3 — `exact` binding strength is derived from the caller's own `replace_blockhash` flag
and never proved against the chain** (`simulate.py:388`). Pre-existing, unchanged.

**A4 — the single-RPC trust root.** Every verification flows through ONE unauthenticated
node that chooses the state, the `err`, and the `unitsConsumed`. Pre-existing, printed on
every demo run, unsolved.

### B. The signer seam

**B1 — a backend fault reports only its exception TYPE, which cost real debugging time.**
Privy refused with `400 {"error":"RPC request denied due to policy violation",
"code":"policy_violation"}`. Our backend redacted it to `HTTP 400`, and the seam redacted
that to `PrivyBackendError`. Diagnosing it needed three throwaway probes. A vendor's own
machine-readable error **code** is not a payload and not a secret.

**B2 — `SignerHandoff` closes OMISSION, not FABRICATION.** A caller can hand-build one with
`approved=True` and any bytes. Recorded as a strict xfail
(`tests/test_signer.py::test_a_hand_built_handoff_is_refused`). Needs a construction token
minted inside `verify_handoff`.

**B3 — a retried transaction reserves the velocity budget twice.** The only key identifying
"the same transaction" is its binding, and a binding may not enter the spend ledger
(invariant #1). **Both Privy and 1claw solved this the same way: a caller-supplied
idempotency key with a 24h TTL.**

**B4 — the spend ledger is ADVISORY**, writable by the process it bounds, so a compromised
agent resets its own budget. Now partially answerable: Privy policy rules are enforced in
the enclave and cannot be reset by us.

**B5 — `_FakeBackend` in `tests/test_signer.py` echoes its input UNSIGNED**, and the whole
suite treats that as "signed". Safe today only because `PrivyBackend` makes its own
signature-slot check (PR #395).

### C. Correctness / routing — gates the "let users on mainnet" claim

**C1 — the out-of-scope floor is 8/12 false accepts, 8/8 on the new rows.** A scripted demo
is safe; **an autonomous agent typing free English at this router is not.** This gates the
autonomous-store claim directly, not just retrieval work.

**C2 — S2's two defects**: cross-account decimals under-report by 1000×, and `.isdecimal()`
accepts Unicode digits.

**C3 — S4's plan refusal has no caller** — an unwired guard is a comment.

**C4 — nothing from the S2/S3/S4/R2 workstream is security-gated.** Per STATE.md the gate
approved the *plan* with 13 conditions; no post-implementation gate ran on delivered bytes.

### D. Provenance and reference surface (carried forward)

**D1** — read `let_me_buy`'s own on-chain IDL (`2nvFnEJA1HKueLpPaje8vR4tsU8rgRktx2dZhyjAfQZG`)
via `gecko.rpc` → `pda_extract.py`, lifting three `manual` origins to `extracted`. The
mainnet run today derived every account offline at `manual` provenance.
**D2** — `tests/fixtures/petstore_openapi.json` is invalid JSON (trailing comma, line 6).
**D3** — `to_arazzo` refuses silently; `graph_data` has no contract.
**D4** — the two-tier `x-gecko-arity` extension + the upstream Arazzo PR (verified: no
for-each in 1.0 or 1.1.0; the spec's own answer is index-0 binding, which we refuse; 5 of 7
Pegana chains refuse).

---

## The PRs

Ordered so nothing depends on something later. Each DoD is falsifiable.

### PR-1 — `fix(simulate): a node that does not compute token balances is not a node that declined`
**Blocker:** A1. **Owner:** `defi-security-engineer` decides, `software-engineer` implements.
**This changes a fail-closed control and MUST be gated before merge.**

Proposal: keep the `balances-null` refusal, and add ONE way out that the node cannot fake —
**structural proof from the decoded message**. If no account in the message is owned by a
token program and no token program appears in the instruction list, then no SPL balance can
change; that is a fact derived from the bytes, not from the node's report. `token_delta`
becomes `measured` with an explicit `basis: "structural"`.

**DoD**
- [ ] A recorded test with `preTokenBalances: null` AND a token-program instruction still
      refuses `balances-null`. (The dangerous direction stays closed.)
- [ ] A recorded test with `preTokenBalances: null` AND no token program present reports
      `measured` with `basis="structural"`, and the spend gate authorizes.
- [ ] `TokenDeltaReport` carries the basis; `structural` is never reported when any token
      program is present, asserted by an AST/enumeration test.
- [ ] `scripts/privy_fork_proof.py --network fork` signs end to end against surfpool.
- [ ] The security gate has ruled on the bytes, with 40-char SHA in the record.

### PR-2 — `fix(rpc): one helper for "the slot these two observations share"`
**Blocker:** A2. **Owner:** `software-engineer`.

**DoD**
- [ ] A single exported helper reads the current slot at `processed` and is used by
      `privy_fork_proof.py`, `autonomous_purchase.py`, and `prepare_purchase.py`.
- [ ] A test pins that the DEFAULT commitment is NOT used (grep/AST), with the reason.
- [ ] `receipt-slot-implausible` remains reachable — a test forces it with a stale receipt.

### PR-3 — `fix(signer): a vendor's machine-readable error code survives redaction`
**Blocker:** B1. **Owner:** `software-engineer` + `defi-security-engineer` review.

**DoD**
- [ ] `PrivyBackendError` carries the vendor's `code` field (e.g. `policy_violation`) when
      the body is JSON with a short scalar `code`; never the body, never headers.
- [ ] `_ask_backend` surfaces `type: code` rather than type alone.
- [ ] A test proves an app secret, a bearer token and a transaction never appear in the
      raised message, using a body that contains all three.
- [ ] Length-capped and charset-restricted so a hostile vendor cannot inject prose.

### PR-4 — `feat(spend): an idempotency key, so a retry does not reserve the budget twice`
**Blocker:** B3. **Owner:** `data-engineer` (ledger schema) + `web3-engineer`.

**DoD**
- [ ] `SpendPolicyGate` accepts a caller-supplied idempotency key; a repeat within the TTL
      returns the SAME verdict without reserving again.
- [ ] The ledger row stores the key and never the binding, the address, or a payload —
      invariant #1 asserted by a schema test.
- [ ] The key is forwarded to Privy's `Idempotency-Key` header.
- [ ] A test drives three retries of one transaction and asserts the daily counter moved
      exactly once.

### PR-5 — `feat(privy): the spend policy gets an enclave twin`
**Blocker:** B4, and the honest limit in `privy_backend.py`'s header.
**Owner:** `web3-engineer`.

Today's policy rules were authored by hand. Generate them from our own `SpendPolicy` so the
two cannot drift, and make the drift detectable.

**DoD**
- [ ] A function projects a `SpendPolicy` into Privy policy rules (`signTransaction` only;
      `signAndSendTransaction` NEVER emitted).
- [ ] A read-back check compares the live Privy policy to the projection and reports drift.
- [ ] Refuses to emit a rule set that is WIDER than the local policy.
- [ ] Documented: which caps Privy can enforce (program id, lamports) and which it cannot
      (per-mint token caps, rolling windows) — the second list stays local and says so.

### PR-6 — `fix(signer): a handoff carries proof of where it came from`
**Blocker:** B2. **Owner:** `staff-engineer` designs, `software-engineer` implements.

**DoD**
- [ ] A construction token minted inside `verify_handoff` and verified in `sign`.
- [ ] `tests/test_signer.py::test_a_hand_built_handoff_is_refused` flips from xfail to pass.
- [ ] `_FakeBackend` is changed to produce a real signature slot (B5), and the existing
      suite still passes — or every test that relied on the echo is named and fixed.

### PR-7 — `fix(find_start): the out-of-scope floor stops accepting out-of-scope rows`
**Blocker:** C1. **Owner:** `ai-ml-engineer` + `graph-engineer`.
**Gates the autonomous-store claim. Nothing user-facing on mainnet ships before it.**

**DoD**
- [ ] `false_accepts` reported split by which floor branch admitted each row, with
      denominators, cold cache (`PYTHONDONTWRITEBYTECODE=1`, `__pycache__` cleared).
- [ ] recall@1 ≥ 0.7576 and recall@3 ≥ 0.9091 on the INTEGRATED tree.
- [ ] The 8 runnable false accepts are ≤ 2, or the autonomous-store claim is withdrawn in
      writing.

### PR-8 — `fix(spend): S2's decimals and digit defects` · **PR-9 — `fix(plan): S4's refusal gets a caller`**
**Blockers:** C2, C3. Carried from STATE.md unchanged; both are prerequisites of C4's gate.

### PR-10 — `feat(provenance): let_me_buy's own IDL lifts three origins to extracted`
**Blocker:** D1. **Owner:** `graph-engineer`. Pure Python, $0, no new dependency.

**DoD**
- [ ] The three `manual` origins in today's account plan read `extracted`.
- [ ] `prepare_purchase` output shows the new tier on the hosted surface.

### PR-11 — the reference-page fixes
**Blockers:** D2, D3, D4. Split as before: fixture, `to_arazzo` refusal + `graph_data`
contract, then the two-tier `x-gecko-arity` and the upstream Arazzo PR.

---

## Sequencing for the dynamic workflow

1. **First dispatch, alone:** the post-implementation security gate (C4) on S2/S3/S4/R2 —
   40-char SHAs and worktree paths pasted in. A gate dispatched alongside its input fails
   open.
2. **Then, in parallel:** PR-2, PR-3, PR-10 (independent, no shared files).
3. **Then:** PR-1 (needs its own gate), PR-8, PR-9.
4. **Then:** PR-4, PR-5 (PR-5 reads PR-4's key).
5. **Then:** PR-6, PR-7.
6. **PR-11** any time — it touches no signing path.

**Standing constraints, unchanged:** `X402_MODE` stays `stub`. A fork is not mainnet and
the output must say so. Only 36,508, 36,399 and now tx #12's 36,399 are publishable mainnet
CU. Mainnet signing is founder-authorized per run, never standing.
