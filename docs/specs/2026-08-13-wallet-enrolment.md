# Wallet enrolment — how an account gets a wallet, and why it may not just say so

**Date:** 2026-08-13 · **Status:** design ruling, not yet built · **Blocks:** hosted-signer
mode (B) reaching a real user

## Where this sits

`gecko.wallet_binding` (#420) answers `account_id -> wallet_id -> pubkey` so a buyer is
LOOKED UP rather than taken at the caller's word. The threading PR carries the gate's
verified account down to the tool call, so the lookup can finally fire. Both halves exist
and **no wallet can be bound**, because `WalletDirectory` is a READ protocol on purpose:
a call path that could also write could rebind an account mid-request, which is the same
defect as trusting a caller-supplied address.

So enrolment is the last missing piece, and it is the piece with the sharp edge.

## The attack that decides the design

The obvious endpoint is the wrong one:

```
POST /registry/wallet   { "wallet_id": "<custodian handle>" }   ← DO NOT BUILD THIS
```

**A `wallet_id` is not a secret.** It is an opaque handle, it travels in support threads,
logs, dashboards and screenshots, and nothing about it proves possession. Meanwhile the
thing that makes it *spendable* — the Privy app secret and the enclave that holds the key —
lives with **us**, not with the caller.

Put those together: anyone who learns a victim's `wallet_id` binds it to their **own**
account and the hosted signer then signs, from the victim's wallet, whatever that attacker
asks for. Not a downgrade, not an impersonation nuisance — a **custody bypass**, and one we
would have built ourselves, on top of an enclave whose whole purpose is that the key cannot
be extracted. The key would not have to be: we would be operating it on request.

Note what does NOT save us. The signer's three-way check
(`sol_delta_account == fee_payer == backend.pubkey`) confirms the transaction pays from the
wallet we are signing for. It is perfectly satisfied here — the attacker's transaction
really is paid by the victim's wallet. The check answers "are these bytes about this
wallet"; it was never asked "is this wallet this caller's".

## The ruling: bind on CREATE, never on ASSERT

The enrolment endpoint does not accept a wallet id. It **makes** one.

```
POST /registry/wallet   {}          Authorization: Bearer <gecko key>
  → account_id  comes from the GATE (never the body)
  → we call the custodian's create-wallet
  → it answers (wallet_id, address)
  → we write { account_id, wallet_id, pubkey } and return the ADDRESS only
```

The caller never names a wallet, so there is nothing to forge. Possession is not proven
because it is not needed: the wallet did not exist until this authenticated request made
it, and it was ours to bind from the first instant.

The pubkey is FETCHED from the custodian's own answer, never accepted from a body — the
same rule `PrivyBackend.pubkey` already follows, and for the same reason: a supplied
address lets a typo produce signatures for an account nobody controls.

### What follows from it

| | |
|---|---|
| **One wallet per account** | A second create is refused (409) and returns the existing address. Silent rebinding is exactly the substitution the record exists to prevent. |
| **Rebinding is an operator act** | Not an API. A lost wallet is a support case with a human in it, not a POST. |
| **Bring-your-own-wallet stays mode A** | You sign for yourself and supply the buyer. We never bind an address we cannot sign for — a binding we cannot act on is a claim, not a record. |
| **Absence stays absence** | No directory, or no binding, means mode A. It must never mean "carry on without checking" ([[wallets.py]] fails closed and loudly). |

## What it needs

1. **A custodian client in the package.** `scripts/privy_backend.py` already does the
   fetch half (`_fetch_address`); create is a new call, and both belong in `gecko/` under
   the repo rule that scripts stay thin. The transport is injectable, so the whole path
   falsifies offline (Pattern B).
2. **The write half of the directory** — a separate protocol from `WalletDirectory`, held
   by the enrolment route ALONE. Not a method the call path can reach.
3. **One authenticated route**, gated by the same `keyauth` decision as the mounts.

## What needs the founder, and why

Creating wallets on the live Privy app is **outward-facing and hard to reverse**: each
successful call creates a real, fundable Solana account under our app. It is also the
first Privy call we would make that is not a signature.

So: the create path ships **disabled without explicit configuration**, and the first live
create is a founder-run act, exactly like a mainnet broadcast. Everything below it — the
route, the refusals, the binding, the offline simulation of the custodian — lands first and
proves itself with no network at all.

## Still open

- **Which custodian.** Privy is proven for signing (13 enclave-signed mainnet txs). The
  1Claw question — can a policy engine express a per-day lamport counter and a constraint
  tied to a simulation we performed, over a RAW Solana message — is unanswered, and it is
  what decides whether the spend ledger closes at the key holder or stays advisory.
- **The durable spend ledger.** Independent of enrolment, and the reason the rolling caps
  do not yet mean anything on a hosted plane.
