# Autonomous local signing — design

**Status:** DESIGN ONLY. No implementation this run, no code file touched by this document.
**Date:** 2026-08-10 · **Lane:** staff-engineer · **Gate:** `touchesSecurityControl: true` — the `defi-security-engineer` gate reviews this BEFORE any code is written, and the gate's word is final.
**Precondition:** `feat/signer-handoff-rebased` (`gecko/handoff.py`, PR #358) merged. Everything below builds on `SignerHandoff`.

## What this is

Today a human is in the loop for every signature: Gecko emits an unsigned transaction and a receipt, and the founder runs the signing command. The target is an **end user's agent** signing without that human — **local signer first**, hosted/web as a later horizon. This document designs the trust chain, the custody choice, the policy, the refusal set, the never-autonomous list, the rollout, and the hosted horizon. It does not authorise anything.

## Hard constraints (stated, not implied)

1. **x402 is NOT the mechanism here and is out of scope.** Our x402 is inbound-only (Gecko charges) and is a payments concern. This design is about signing **Solana transactions that Gecko has simulated**. `X402_MODE` is not referenced, not read, and not changed by anything in this design; it stays `stub`. `private/operating/STATE.md:491-495` still records the direction as "sign x402 payments autonomously" — the founder's scope correction supersedes that line, and this document diverges from it deliberately rather than silently. **Where the two genuinely intersect:** an agent that pays for an API with an on-chain x402 settlement would need exactly this signing path underneath it. That is a *consumer* of this design, not a part of it — and it would arrive as one more entry in the program/instruction allowlist of §3, never as a mechanism. Do not wire them. If someone proposes to, they are proposing that Gecko become a payment rail, which `CLAUDE.md` already forbids.
2. **Claude never signs or broadcasts a mainnet transaction.** This is a **product target for the end user's agent**, not permission for any agent operating in this repository's sessions. `CLAUDE.md:135-137` currently reads as governing both; §9 PROPOSES replacement wording. It is a proposal — this document does not apply it.
3. **No key material, no treasury address, no facilitator URL** appears in this file or in any file this design produces. Keys are referenced by *handle*, never by value — the pattern already shipped in `gecko/credentials.py:51-65` (`CredentialRef` = which credential, never the credential).
4. **Control-plane invariant #1 holds.** No response payload, no transaction payload, no key, no log line is persisted. `simulate.py:15-17` and `handoff.py:28-29` already state this; nothing here relaxes it. The one new piece of state this design introduces — the cumulative-spend counter of §3 — stores counters, never payloads, and §8 names its own weakness.
5. **Pattern B.** The first implementation deliverable is a free offline simulation that can falsify the whole refusal matrix with no network, no chain, and no key. Live smoke is the final check, never the debugger.

## Inputs consumed

**A1 — the advisory-gate list.** A1's finding is that our safety controls are things a caller MAY consult rather than states that change what a caller CAN do. Independently reproduced here:

- `gecko/signing_gate.py:39-65` — `evaluate` is a pure decision function. Grep finds no caller in `gecko/`; the only importer is `tests/test_signing_gate.py:11`.
- `gecko/handoff.py:7-11` — `txbind.evaluate_tx` shipped "with no callers outside its own module". Confirmed: inside the package only `handoff.py:122` calls it; every other caller is a script or demo a human runs (`scripts/sign_and_send.py:102`, `scripts/prepare_purchase.py:267`, `scripts/compose_e2e.py:86`, `demo/kit/mainnet_receipt_screenplay.py:141`).
- `gecko/handoff.py:84-85` — the defaults `require="structural"`, `replace_blockhash=True` mean a caller that takes the defaults gets a binding that does not cover the blockhash (`txbind.py:22-28`, `:233`).
- `gecko/simulate.py:286-298` — the binding is best-effort; a bare `except` yields `binding=None`. Downstream this fails closed (`txbind.py:197-201`), which is correct, but the *reason* is lost.
- `gecko/policy.py:26-30` — "an unset field is a no-op". Correct for the API surface. Fatal for a signer.
- `gecko/enforce.py:26-28` — an established fail-OPEN precedent ("if the SCORER ITSELF raises, the caller logs and ALLOWS").

**A2 — the "same bytes" invariant.** The bytes that were simulated, the bytes that were bound, and the bytes handed to the signer are **one captured object, never re-derived** — `handoff.py:97-110`: "CAPTURE, never rebuild … `/build` embeds a fresh `recentBlockhash` each time, so the second transaction is not the one that was simulated." This is the strongest control we have and the only genuinely structural one, because it is enforced by the shape of the data rather than by a caller's diligence. §4 restates the re-simulate-before-signing requirement in these terms so the property is **enforced, not hoped for**.

---

## 1. The trust chain

`intent → plan → simulate → Receipt → txbind binding → SignerHandoff → signer → broadcast`, with the build step named explicitly because it is a third party.

| Hop | VERIFIED (a mechanism checks it) | ASSERTED (we take someone's word) | ATTACKER-CONTROLLABLE |
|---|---|---|---|
| **user → intent** | nothing | that the natural-language request is what the user meant | the user's own prompt context; any tool description or doc text the agent read (comprehension-time quarantine sits upstream: `signing_gate.py:1-13`) |
| **intent → plan** | **nothing.** No mechanism checks that the plan expresses the intent | the whole correspondence | poisoned surface text steering account choice or amount; this is the largest asserted link in the chain |
| **plan → built tx** | that a transaction came back and decodes (`simulate.py:217-224`) | that the builder built *this* plan | the builder is a remote HTTP service (`simulate.py:183-195`); a compromised builder returns different accounts |
| **built tx → simulate** | that the RPC accepted and executed the bytes | that the RPC node is honest | the RPC URL: `rpc.py:76-79` allows loopback and any public host by design |
| **simulate → Receipt** | land/no-land, CU, categorical revert class (`simulate.py:300-332`) | that the snapshot resembles the state at signing time; `network_label` carries this caveat | whoever controls the node controls the snapshot |
| **Receipt → binding** | sha256 over the message; version+strength folded in so structural can never be read as exact (`txbind.py:163-175`) | under `structural`, the blockhash — normalised out (`txbind.py:130-144`) | nothing, once the digest exists; absence of a digest is the risk (`simulate.py:286-298`) |
| **binding → SignerHandoff** | **structurally verified:** `transaction_base64` is populated iff `approved` (`handoff.py:49-57`, `:124-134`); the verified bytes and simulated bytes are the same object (`handoff.py:97-110`) | that the caller branches on `approved` — and it does not matter, because a refusal carries no bytes | a caller could bypass `prepare_handoff` entirely and call the builder itself |
| **SignerHandoff → signer** | today: the founder's eyes, plus `scripts/sign_and_send.py`'s own re-checks | today: nothing else | a caller that hands the signer a raw string instead of a handoff |
| **signer → broadcast** | fee payer == keypair pubkey; a fresh live re-simulation; `require="exact"` (`scripts/sign_and_send.py:79-106`) | that the human read the plan | RPC for `sendTransaction`; the ~150-slot window |

### Where today's human adds safety that structure must replace

Every property between "we have a receipt" and "a signature exists" currently lives in `scripts/`, not in `gecko/`. Four things go away the moment the human does:

1. **Fee-payer identity.** `sign_and_send.py:79-85` stops if the transaction pays from an account the keypair does not control. Nothing in `gecko/` performs this check. → must become a refusal path (§4).
2. **Freshness.** `sign_and_send.py:87-96` re-takes the receipt against live state at the moment of signing, with `replace_blockhash=False`, and `:102` demands `exact`. The docstring is explicit: "A receipt is true for the state it was taken against." → must become a structural requirement (§4).
3. **The pause.** `--send` defaults off (`:108-110`); `prepare_purchase.py:276-284` prints "APPROVED TO SIGN — by you, not by this script." The pause is where a human notices a wrong destination. → the pause cannot be replaced by more verification; it is replaced by **authorization** (§3). This is the whole reason §3 exists.
4. **Intent judgement.** The human is the only thing checking that the plan is what was asked for. **Nothing in this design replaces that.** The policy of §3 bounds the damage; it does not prove intent. Say this plainly in any outward copy.

**Reversibility:** the trust chain's shape is **one-way** — it is the security story, and every downstream artefact (receipt, handoff, policy) encodes it. Module names and file layout are two-way.

---

## 2. Key custody — one recommendation

**RECOMMENDATION: (D) an external signer holds the key and consults our verdict — as the destination. Local-first is reached via (B) OS keychain, with (A) a keypair file allowed only behind an explicitly named developer profile. (C) a TEE-leased short-lived key is the hosted horizon, injected at the same seam. All four are the same injected `Signer` protocol; Gecko never reads key material in any of them.**

The four options, judged on one question — *does Gecko end up holding, or being able to hold, key material?*

| Option | Custody posture | Verdict |
|---|---|---|
| **A. Local keypair file** | The key is on disk in the user's own process, read by the user's own signer. Gecko holds a callable, not bytes — **only if** we never read the file ourselves. | Allowed for stage 0–1 behind a named profile. Not the default. |
| **B. OS keychain** | Same as A, minus a plaintext file, plus OS-level access control and an unlock prompt. | **The local-first default.** |
| **C. TEE-leased short-lived key (1claw pattern), injected via the `access.py`-shaped seam** | Key material never exists in our process; the lease expires. Strongest of the local family, most infrastructure. | The hosted horizon (§7), same seam, not built now. Note the practical caveat already on record: `scripts/SUBSCRIBE.md:21` — an OKX TEE key could not do the local message-sign the activation needed. Enclave wallets are not drop-in for every signing shape. |
| **D. External signer holds the key and consults our verdict (Privy-style, signer BELOW our verdict)** | **Gecko cannot hold a key even by mistake.** The key belongs to a party whose business is custody; we supply a verdict and bytes. | **The recommendation and the destination.** |

**Which option keeps us out of being a custody provider, and why.** D, unambiguously. A custody provider is defined by *possession or control of key material on behalf of another party* — a legal and regulatory posture, not an architectural one. Under D we never possess and never control: we produce a `SignerHandoff` and someone else decides. B and A stay non-custodial for a narrower reason: the process holding the key is **the user's own machine, under the user's own account**, and Gecko holds a *callable*, never the bytes. That property is fragile in exactly one way — the first time we add "let Gecko read `~/.config/solana/id.json` for convenience", we are a custody provider on that machine. **The rule that protects it: no function in `gecko/` ever takes a key path, a mnemonic, a secret key, or returns one.** `sign_and_send.py` reads a keypair today and is in `scripts/` for exactly this reason (`sign_and_send.py:1-5`: "nothing in `gecko/` signs, holds a key, or broadcasts, and that stays true"). Autonomous signing must not be the change that makes that sentence false.

**What "local first" looks like SAFELY** (the founder's sequencing):

1. **The seam is a protocol, not a key.** `Signer.sign(handoff: SignerHandoff) -> SignedTransaction`. It takes the **handoff object**, never a base64 string — so a caller cannot hand the signer bytes that never passed the gate. The type is the enforcement.
2. **Resolution by reference, reusing what shipped.** Which key is a `CredentialRef`-shaped handle resolved through the existing backend chain — keyring → command → env (`credentials.py:12-24`) — and the typed error must never contain the value (`credentials.py:43-49`). A TEE/1claw lease is the same shape as its `CommandBackend`: an argv list whose child returns a handle, never a shell string.
3. **A dedicated key, funded to the cap.** The local signer's key is not the user's main wallet. It holds only what the policy authorises it to lose. This single decision bounds every unknown-unknown in §8.
4. **The signer refuses to sign for an account it does not control** — fee payer == its own pubkey, checked inside the signer (today `sign_and_send.py:79-85`).
5. **The key never crosses a process boundary we own**, never appears in a log, never appears in an exception, and is never a function argument in `gecko/`.
6. **Explicit, named opt-in.** No default configuration signs anything. Absence of configuration is a refusal, not a permission (contrast `policy.py:26-30`, which is opt-in-as-no-op — correct there, inverted here).

**Reversibility:** the *seam* (a `Signer` protocol taking a handoff) is **two-way** — cheap to reshape. The *custody posture* (we never hold key material) is **one-way**: it is the same class of promise as invariant #1, and unwinding it changes what Gecko legally is.

---

## 3. The spend policy — verification and authorization, separate and both present

**The OKX `singleTxLimit` lesson, stated as a rule:** a per-transaction limit is **authorization** — it says "you may spend up to X". It says nothing about whether *this* transaction does what was asked. And a receipt is **verification** — it says "these bytes will land and burn this much CU". It says nothing about whether the user ever wanted them to. Each is worthless as a substitute for the other:

- Verification without authorization signs a perfectly-simulated transfer of everything to an attacker's address.
- Authorization without verification blesses a transaction that reverts, or that does something the limit does not measure — and a per-tx cap of X is defeated by N transactions of X.

**Both predicates must pass. Neither may be inferred from the other. A denial from either is a refusal that yields no bytes.**

Four caps. The natural home is `gecko/policy.py`'s `AgentPolicy` (`policy.py:21-45`), extended — but with **its default inverted for signing**:

1. **Per-transaction cap.** Maximum value a single transaction may move. Extends the existing `spend_cap` (`policy.py:32`).
2. **Cumulative / velocity cap.** Maximum over a rolling window (per hour and per day), and a maximum transaction count. This is the cap the `singleTxLimit` lesson actually demands. `examples/txline_sharp_agent/wallet_sim.py:87-93` already prototypes the cumulative form (`_spent + intent.amount > cap → PolicyViolation`) with no keys and no chain — lift it. §8 names where the counter is weak.
3. **Allowlisted programs + instructions.** Not "which program" but "which instruction of which program". A program allowlist that permits every instruction of a DEX permits `setAuthority`-shaped instructions in the same breath. An instruction that cannot be decoded cannot be allowlisted, and therefore is refused (§5).
4. **Allowlisted destinations.** The set of accounts that may receive value. Extends `recipient_allowlist` (`policy.py:33`).

**Three properties that make this a policy rather than a suggestion:**

- **The policy is evaluated over the DECODED MESSAGE, never over the agent's stated intent.** Program ids, instruction discriminators, writable accounts, and the receipt's own `sol_delta` (`simulate.py:310-316`) are facts about the bytes. The agent's intent string is attacker-influenceable (§1, hop 1). We already decode locally (`txbind.py:112-127`) — the policy reads that same message.
- **Absent policy = refuse.** `policy.py:26-30` documents "an unset field is a no-op". That is right for a governed API session and **wrong for a signer**: an autonomous session with no policy authored must sign nothing. This inversion is a deliberate, called-out divergence from the existing record's semantics, not an oversight — implement it as a distinct type or an explicit `authorized: bool` rather than by quietly changing `AgentPolicy`'s meaning under its current consumers.
- **The policy is authored by the human, out of band, and the agent cannot widen it** (§5, item 2).

**Reversibility:** the *presence* of both predicates is **one-way** (it is the security invariant). The *numbers*, the record's field names, and where the record is stored are **two-way**.

---

## 4. Fail-closed behaviour

**The invariant: every path below yields NO bytes and NO signature.** Structurally, not by convention — `SignerHandoff.transaction_base64` is `None` on every refusal (`handoff.py:49-57`), populated only inside `if verdict.approved` (`handoff.py:124-134`), and the signer's parameter type is `SignerHandoff`, never `str`. A caller that forgets to check the flag still cannot reach signable bytes.

| # | Refusal condition | Detected at | Result |
|---|---|---|---|
| 1 | Builder returns no transaction / build transport fails | `simulate.py:206-224` | refusal — **see the gap below** |
| 2 | Simulation reverts (`status != "pass"`) | `txbind.py:212-217` | `approved=False`, no bytes |
| 3 | Binding absent (tx undecodable, unknown version) | `simulate.py:286-298` → `txbind.py:197-201` | refusal |
| 4 | Binding strength below what the caller required | `txbind.py:206-211` | refusal — never a silent downgrade |
| 5 | Presented binding ≠ attested binding | `txbind.py:224-231` | refusal — the swapped-account case |
| 6 | Blockhash expired between simulate and sign | fresh `exact` re-simulation (§below) | refusal; re-plan, it is free |
| 7 | Policy denies — per-tx cap | policy predicate over the decoded message | refusal |
| 8 | Policy denies — cumulative / velocity | policy predicate + counter | refusal |
| 9 | Policy denies — program/instruction not allowlisted, or undecodable | policy predicate | refusal |
| 10 | Policy denies — destination not allowlisted | policy predicate | refusal |
| 11 | No policy authored | policy resolution | refusal (§3) |
| 12 | Key unavailable / keychain locked / lease expired | signer | refusal — never fall back to a weaker key source |
| 13 | RPC unreachable, or not the configured endpoint | `rpc.py` / transport | refusal — "we could not check" is never "fine" (`txbind.py:33-35`) |
| 14 | Receipt older than N slots | freshness check (§below) | refusal |
| 15 | Fee payer ≠ the signer's own pubkey | signer, today `sign_and_send.py:79-85` | refusal |
| 16 | Handoff already consumed | single-use marker (§8) | refusal |

**GAP TO FIX BEFORE IMPLEMENTATION (row 1).** `prepare_handoff`'s docstring says "Never raises for a bad build or an undecodable transaction" (`handoff.py:94-96`), but `handoff.py:111-118` calls `simulate` unwrapped and `_default_build_call` raises `SimulateError` on an HTTP or URL failure (`simulate.py:206-213`). **An exception is not a refusal object.** The zero-effort fix a caller reaches for is `try/except` around the handoff — which is precisely the bypass shape `handoff.py:9-11` warns about. Row 1 must return a refusal `SignerHandoff`. This is a review comment on PR #358, not an edit made here.

**The re-simulate-immediately-before-signing requirement, in A2's terms.**

> The bytes that are simulated, the bytes that are policy-checked, the bytes that are bound, and the bytes that are signed are **one object, captured once, never re-derived**.

Enforcement, not hope — three mechanisms:

1. **Capture, never rebuild.** Already structural (`handoff.py:97-110`). Any implementation that re-POSTs the builder to "get the bytes for signing" is rejected at review: `/build` embeds a fresh blockhash, so the second transaction is not the one that was simulated, and a `structural` binding would hide exactly that.
2. **The signer accepts only a handoff.** Because `transaction_base64` is `None` on refusal, "the bytes" and "the approval" cannot be separated by a caller. A signer whose parameter is `str` re-opens the whole window; the type signature is the control.
3. **Freshness is a precondition of signing, not a courtesy.** Immediately before signing, the signer re-runs `simulate(..., replace_blockhash=False)` against live state and requires `require="exact"` — the `exact` binding covers the blockhash (`txbind.py:26-28`), so an expired or substituted blockhash cannot match. Additionally the handoff records the slot at simulation time and the signer refuses if `current_slot - receipt_slot > MAX_RECEIPT_AGE_SLOTS`. Two independent reasons to refuse a stale receipt, because `exact` alone silently degrades to "whatever the node accepted".

This is exactly what the human does today (`sign_and_send.py:87-106`) and what `prepare_purchase.py:276-284` tells the human to do ("Sign THESE BYTES; do not rebuild"). The structure now says it instead of a printed sentence.

**Fail-open is excluded here.** `enforce.py:26-28` allows fail-open when the *scorer* crashes, on the reasoning that a scoring bug must not break the product. That reasoning does not transfer: on the signing path a crash in the checker must refuse. The correct precedent from the same file is `FAIL_CLOSED_SIGNAL = "gate.unscored_write"` (`enforce.py:53-56`) — a state-changing operation that could not be checked is refused, and the refusal names why.

**Reversibility:** **one-way.** The refusal set is the product's safety claim.

---

## 5. What must NEVER be autonomous — closed list

Closed means: nothing is added to this list by implication, and anything not on it is still subject to §3.

1. **Signing for a fee payer or authority the agent's own key does not control.** Produces a useless signature at best; at worst it is an attempt to act as someone else.
2. **Changing the policy — raising a cap, adding to any allowlist, or extending a window.** The policy *is* the authorization. An agent that can widen its own bound has no bound. Policy authorship is human, out of band.
3. **Key export, derivation, backup, or migration to another host.** These convert a bounded local capability into an unbounded portable one, and none of them is verifiable by simulation.
4. **Any instruction that transfers future signing power** — `setAuthority`, upgrade-authority changes, token `approve`/delegate (especially unlimited), multisig membership changes. One approval becomes unbounded future approvals, and the receipt for such a transaction looks harmless.
5. **Account closure or any irreversible sweep to an arbitrary destination** (`closeAccount` with a destination outside the allowlist). Irreversibility removes the only remedy we have.
6. **A transaction containing an instruction we cannot decode, or a program id not on the allowlist.** An undecodable instruction cannot be policy-checked, so approving it would be approving something we did not read (`txbind.py:33-35`).
7. **The first transfer to a never-before-seen destination above a dust threshold.** The most common real-world loss is a correct-looking transaction to the wrong address; a destination allowlist only helps if adding to it is a human act (item 2).
8. **Signing an off-chain / arbitrary message.** A raw ed25519 message signature is a blank cheque no simulation can evaluate. Note that `access.py:35-36` already defines `Signer = Callable[[bytes], str]` for the auth handshake — **that seam must never be reused for autonomous transaction signing**, and the two must not share a type name.
9. **Anything on mainnet before stage 3, and anything at all outside an explicitly named profile.** Absence of configuration is a refusal (§2).
10. **Any signing or broadcast by an agent operating inside this repository's sessions, on any network.** This is the `CLAUDE.md` rule and it is unchanged by anything above; §9 only clarifies that it governs *us*, not the product.

---

## 6. Staged rollout

Each stage has its own gate and its own **measured** promotion criteria. "Measured" means the number is the return value of a named function, asserted in a test or printed by a named command — the standard already recorded in `STATE.md`'s FEEDBACK section ("A quotable number must be the return value of a named function"). An assertion in a PR description is not a measurement.

**Stage 0 — offline falsifier (Pattern B). No network, no chain, no key.**
- Gate: this document approved by `defi-security-engineer`.
- Build: injected fake build/rpc/signer, plus the `wallet_sim.py:49-98`-shaped policy fake.
- MEASURE: (a) for **every** row of §4's table, `transaction_base64 is None` **and** a spy signer's call count is exactly `0` — one test per row, no exceptions; (b) a property test that mutating any byte of the message flips approval; (c) the same-bytes invariant — the object handed to the signer `is` the captured object; (d) the row-1 gap returns a refusal rather than raising.
- Promote when: the refusal matrix is complete and every row is red before the fix and green after (harness before fix).

**Stage 1 — surfpool mainnet fork.**
- Gate: stage-0 matrix green; security-gate sign-off on the diff, not just the design.
- MEASURE: (a) the §4 matrix reproduced against a real node, same counts; (b) receipt-predicted CU vs fork-executed CU across ≥N distinct plans; (c) **zero** signatures produced on any refusal path, observed at the signer boundary, not inferred; (d) the distribution of simulate→sign latency and the resulting blockhash-expiry rate — this number decides whether `exact` is operationally viable at all; (e) policy denial counts by rule, proving each rule fires at least once.
- Promote when: matrix reproduced live, expiry rate known, no unexplained approval.

**Stage 2 — devnet.**
- Gate: stage-1 measurements written down with the producing function named.
- MEASURE: (a) end-to-end landed rate; (b) receipt-predicted CU vs on-chain charged CU (the claim we already publish for 11 mainnet receipts — hold the new path to the same bar); (c) **count of transactions signed against a stale receipt = 0**, measured by comparing the receipt slot recorded in the handoff to the slot at signature; (d) cumulative-cap accounting reconciles against on-chain history to the lamport; (e) count of transactions signed that were not in the intended plan = 0.
- Promote when: (c) and (e) are zero over a stated, non-trivial number of transactions, and (d) reconciles.

**Stage 3 — mainnet with a hard cap.**
- Gate: founder go-ahead **and** a second security-gate review of the live configuration, not just the code.
- Preconditions: an explicitly named profile; a dedicated key funded only to the cap; per-tx and daily caps set at a stated dust level; a kill switch that revokes the policy without a deploy; the allowlists enumerated by hand.
- MEASURE before any cap increase: everything from stage 2 at mainnet scale, plus zero false approvals in stage 2, plus the *actual* spend against the *authorised* spend.
- Note: cap increases are a human act (§5, item 2). There is no automatic promotion past stage 3.

**Reversibility:** the staging itself is **two-way** (order and thresholds can change); "no mainnet before an explicit gate" is **one-way**.

---

## 7. The web / hosted horizon — designed for, not designed now

Hosted signing is where this becomes a custody question rather than an engineering one. What follows exists so hosted is **not a rewrite**; none of it is built now.

**Key location.** The key never enters a Gecko process and never enters a Gecko datastore — hosted or not. Hosted Gecko holds a *policy* and produces a *verdict*; the key stays with the user's KMS, TEE, or external signer (option D of §2). Any design in which a hosted Gecko can sign on a user's behalf makes us a custody provider and requires a legal/compliance answer that is a `business-manager` call, not an engineering one. Do not cross that line inside a technical PR.

**Attestation.** A hosted verdict must not be trusted merely because it arrives from us. The trust-minimising design is the one already implicit in §4: **the signer re-simulates and re-checks the binding itself**, so our verdict is a convenience, not a credential. If a signer nonetheless wants to rely on our verdict, the verdict must be signed and must carry the receipt slot and the binding — but relying on it is strictly weaker, and the docs must say so.

**Multi-tenant blast radius.** Per-tenant policy isolation and per-tenant counters, with tenancy **derived at the boundary and re-validated on read** — the pattern PR #356 established for the corpus, which is the right precedent to copy rather than re-derive. No shared key material by construction (there is none). No shared RPC credential. A policy or counter that cannot be attributed to exactly one tenant is a refusal, not a default.

**What keeps this from being a rewrite:** everything in §2–§4 is (a) a `Signer` protocol taking a `SignerHandoff`, (b) a policy record, (c) a refusal set. "Hosted" changes only which `Signer` implementation is injected and where the policy is read from. If a hosted design requires changing the *handoff* or the *refusal set*, the local design was wrong — that is the tell to watch for.

**Reversibility:** the seam is **two-way**; "the key never enters our process" is **one-way** and is the same class of promise as invariant #1.

---

## 8. Adversarial — what could go wrong, and which parts of THIS design are advisory rather than structural

Per A1: a control is **structural** when a caller *cannot* reach the unsafe state, and **advisory** when a caller *may choose* to consult it. Naming which is which is the point of this section.

### Structural today
- **A refusal carries no bytes.** `handoff.py:49-57`, `:124-134`. Populated only inside the approved branch.
- **Capture, never rebuild.** `handoff.py:97-110`. A2's invariant; enforced by the shape of the data.
- **Strength cannot be silently downgraded.** `txbind.py:206-211`, and strength is folded into the digest (`txbind.py:169-175`), so structural can never be mistaken for exact.
- **A missing binding is a refusal.** `txbind.py:197-201`.

### Advisory today — each is an attack surface until it is made structural
1. **`signing_gate.evaluate` is a pure function nobody calls.** `signing_gate.py:39-65`; zero callers in `gecko/`, only `tests/test_signing_gate.py:11`. The poison verdict therefore gates nothing at signing time. → fold the tool-identity/quarantine check into the handoff, or drop the claim.
2. **`evaluate_tx` is enforced only where a human already is.** Inside the package only `handoff.py:122`; everywhere else it is a script the founder runs. Any *new* caller can still go straight to the builder and skip the gate entirely. → the builder call must not be reachable from the autonomous path except through `prepare_handoff`.
3. **The defaults are the weak setting.** `handoff.py:84-85` gives `structural` + `replace_blockhash=True`. A caller that takes the defaults gets a binding that does not cover the blockhash. → for autonomous signing the signer must *demand* `exact`; a default is not a control.
4. **The binding is best-effort.** `simulate.py:286-298` swallows every exception to `None`. Downstream refusal saves us, but the reason is lost, so a systematic decode failure looks like a policy denial. → record the reason.
5. **`policy.py`'s unset-is-a-no-op default.** `policy.py:26-30`. Under signing, this is "no policy = unlimited". → §3's inversion, or the policy is decorative.
6. **The fee-payer check lives in a script.** `sign_and_send.py:79-85` only. → §4 row 15.
7. **A build failure raises instead of refusing.** `handoff.py:94-96` vs `:111-118` + `simulate.py:206-213`. → §4 row 1. An exception invites the `try/except` that becomes the bypass.
8. **Fail-open precedent next door.** `enforce.py:26-28`. Nothing structurally prevents someone copying that stance onto this path. → say so in the module docstring when it is written.
9. **The cumulative counter is only as trustworthy as its storage.** If the counter lives in a file the agent's own process can write, an agent (or anything that has compromised it) resets its own budget and the velocity cap becomes advisory. **This is the weakest part of §3.** Either the counter lives where the signer — not the agent — controls it (ideally inside the external signer of option D, which is another argument for D), or the cap must be described as advisory. Do not publish it as a control until this is settled.
10. **A handoff has no single-use marker.** Nothing today prevents the same approved handoff being presented twice. Within the blockhash window a replay is a real second transaction. → §4 row 16; the marker must be held by the signer.
11. **The RPC is an unauthenticated trust root.** `rpc.py:76-79` permits loopback and any public host by design (surfpool + mainnet). A hostile or compromised node returns a clean simulation for a transaction that will not behave as attested — and *every* verification in this design flows through it. Mitigation options: simulate against two independent endpoints and require agreement before a mainnet signature, or accept it and state the limit in the docs. **Do not leave it unstated.**
12. **Intent → plan is entirely asserted.** No mechanism checks that the plan is what the user asked for. §3 bounds the loss; it does not prove intent. This is the largest advisory link in the whole chain and it does not become structural in this design. Outward copy must not imply otherwise.

### Attack scenarios worth naming
- **Poisoned surface text steers the plan.** Comprehension-time quarantine is upstream (`signing_gate.py:1-13`) and — per item 1 — is not consulted at signing. The policy is the only thing standing between a steered plan and a signature.
- **Compromised builder returns different accounts.** Caught **only** because we simulate and bind *the bytes the builder returned* and hand *those same bytes* onward (A2). Break that invariant and this attack succeeds silently.
- **Swap between simulate and sign.** Closed by the same-bytes invariant plus §4's freshness rule; re-opened by any signer that accepts a `str`.
- **Blockhash-window replay.** Item 10.
- **Death by a thousand small transactions.** The `singleTxLimit` lesson; answered only by the velocity cap, whose storage is item 9.
- **TOCTOU on the policy.** The policy must be read once and carried with the handoff, not re-read at signing time where it could have changed underneath.
- **Leakage through logs.** `SignerHandoff` carries `logs_tail` (`handoff.py:67`). It is returned, never stored (`handoff.py:28-29`) — any telemetry or corpus write that touches a handoff breaks invariant #1. Add a test that asserts it, since the invariant is currently a comment.

---

## 9. PROPOSED replacement wording for CLAUDE.md — not applied

`CLAUDE.md:135-137` currently reads:

> - **Sign or broadcast a mainnet transaction.** The on-chain subscribe is **founder-run only**. Claude simulates and hands over the command; the founder broadcasts. The OKX agentic wallet is a funder, not a signer here.

It reads as governing both the agents working in this repository and the product we are building. Proposed split — **a proposal for the founder, applied by nobody in this run**:

Under **NEVER**:

> - **Sign or broadcast a transaction from inside this repository's sessions — any network, no exceptions.** This rule governs **agents working on Gecko**, not the product. Claude simulates and hands the command to the founder; the founder broadcasts. The on-chain subscribe stays founder-run. The OKX agentic wallet is a funder, not a signer here.

Under **ALWAYS** (or a new "Gated capabilities" note):

> - **Autonomous signing is a PRODUCT capability for the end user's agent, and it is gated.** Code that lets *someone else's* agent sign may exist in this repo only under `docs/specs/2026-08-10-autonomous-local-signing.md`: `defi-security-engineer` approval **before** implementation, the offline falsifier first, no mainnet default, no key material in `gecko/`, and never exercised by an agent in this repo.

Rationale: today's single rule makes the product target look like a violation of our own standards. Two rules make the distinction explicit — *we* never sign; *the product* may, under a gate.

---

## Decision summary

**RECOMMENDATION:** ship autonomous signing as an injected `Signer` protocol that accepts only a `SignerHandoff` — local-first via the OS keychain, destination an external signer that holds the key and consults our verdict — with verification (fresh `exact`-bound re-simulation) and authorization (per-tx, velocity, program+instruction, destination) as **two independent predicates that must both pass**, every failure yielding no bytes and no signature.

**WHY:**
- The only structural control we have today is "a refusal carries no bytes" plus capture-never-rebuild; everything else on the path is advisory (§8, items 1–8) and was enforced by the human we are about to remove.
- Keeping the key outside our process is what keeps Gecko a comprehension-and-verification layer rather than a custody provider — a one-way door.
- Verification and authorization answer different questions; the `singleTxLimit` lesson is that shipping one and calling it safety is how this fails.
- Pattern B makes the whole refusal matrix falsifiable for $0 before a single byte is signed.

**REVERSIBILITY:** one-way — trust chain, custody posture, refusal set, both-predicates rule, "no mainnet before an explicit gate". Two-way — module names, file layout, thresholds, staging order.

**MODULES AFFECTED:** `gecko/handoff.py` (row-1 gap; the handoff is the only path to bytes) · `gecko/txbind.py` (freshness/slot, unchanged semantics) · `gecko/simulate.py` (record the receipt slot; keep binding best-effort but record the reason) · `gecko/policy.py` (the four caps + inverted default, as a distinct signing record) · `gecko/access.py` (the seam shape only — `Signer` there signs arbitrary bytes and must not be reused) · `gecko/credentials.py` (key-handle resolution by reference) · a new signer module that holds no key · `scripts/sign_and_send.py` (unchanged; it is the human path and stays) · `CLAUDE.md:135-137` (proposed, not applied).

**DELEGATE TO:** `defi-security-engineer` gate first (blocking) → `software-engineer` for the stage-0 offline falsifier → `web3-engineer` for the signer seam and stage-1 fork measurements. No implementation before the gate returns.

**OPEN QUESTIONS:**
1. **Where does the cumulative-spend counter live?** If the agent's own process can write it, the velocity cap is advisory (§8, item 9) — which argues for option D holding it. Founder + security gate.
2. **Do we require two independent RPC simulations before a mainnet signature, or accept a single node as a trust root and document the limit?** (§8, item 11.) This is a cost/latency call as much as a security one.