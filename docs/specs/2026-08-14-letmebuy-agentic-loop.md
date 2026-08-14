# Making the LetMeBuy store work, end to end, from a chat client

**Date:** 2026-08-14 · **Status:** approved, task #1 starting · **Supersedes nothing;
narrows** `2026-08-13-roadmap.md` goal 1 into shippable pieces

## The one sentence everything below is derived from

> A person opens Claude web or Grok, adds our hosted surface, says *"order a water from
> Jonas Bar at table 11"*, approves once, and the order lands on mainnet — the receipt's
> predicted CU matching what the chain charged, and no key ever on our machine or theirs.

That is the acceptance test. A task exists only if it makes that sentence true. Nothing
here is done until a real client does it once.

## Why this, and why now — the positioning half

The founder's question was *"we help API providers do what?"* and it has no answer **in
this case**, which is the finding rather than the problem. Orquestra is a **partner**
(open-source, likely absorbed into the Solana Foundation), and LetMeBuy is a program built
by Solana Foundation developers. There is no provider across a table from us here. The
sentence felt confusing because it was being asked to name a payer, and the payer changes.

The formulation that survives both worlds names the job, not the payer:

> **We make a published surface executable by an agent.**

In this case the person who needs that is Berkay: he has published a surface agents cannot
act on. In the paid future it is a provider paying us to do it for their users. Same job.

**Scope ruling (founder, 2026-08-14):** the destination is *"make any API usable from
chat"*; the claim we make **now** is the narrow one — *a published surface an agent cannot
act on → one correct, checked call*. The broad claim becomes true by shipping the narrow
one. This is the existing wedge boundary, unchanged: *"plain-action → correct call (we
own); full-commerce multi-hop = agent reasoning = NORTH STAR, don't over-promise."*

### The claim ladder

Each task earns a sentence. Do not say a line before its task is green.

| After | We may say |
|---|---|
| today | "An agent can execute a purchase correctly on a real Solana program — 19 of 20 mainnet transactions, every one predicting its exact CU." |
| #1–#3 | "…and it can do that from a hosted surface behind a key you mint yourself." |
| #4–#5 | "…without ever holding a key, with the wallet bound to your account rather than named by the caller." |
| #6 | "Make any API usable from chat." ← the destination, now with a client that did it |

## Two corrections this work must not inherit

1. **"Gecko broadcasts nothing" is FALSE and is already published.** `mainnet.mdx:151`
   says it; `mainnet.mdx:119-121` on the same page disproves it — Gecko asks for a
   signature and submits the bytes itself, deliberately, so the signed message can be
   re-checked against the receipt before it reaches the chain. The true and stronger claim
   is **holds no key, never signs**. Any hero copy asserting "no broadcast" would make the
   site contradict the engine.
2. **Lead every demo with a recovered gap, never the catalog listing.** The standing rule:
   *"listing /api/projects + passing /instructions IS a one-week wrapper the coding agent
   absorbs → every demo must LEAD with a recovered gap."* A hero whose first beat is
   "Surface found: Let Me Buy · Orquestra" opens on the wrapper-shaped part. Open on the
   part that cannot be wrapped — the seed the IDL drops, the buyer/merchant token accounts
   a naive derivation confuses, the revert that a build-and-hope path earns.

## Where we actually are (verified 2026-08-13/14)

Working: `prepare_purchase` derives every account offline, refuses wrong plans before
building, simulates the exact bytes, returns UNSIGNED bytes with a receipt. Mode B (the
buyer looked up from a bound wallet rather than named by the caller) is proven end to end
offline over the served transport. The SSE transport is gated on both doors and its stream
door works. The `store` mount exists and is unbuildable unless it will be gated.

Missing: nothing can WRITE a wallet binding; nothing on the hosted plane can sign; the
spend ledger does not survive a task replacement; no real client has ever driven it.

## The six tasks

Ordered by uncertainty killed per unit of work, not by dependency depth.

### 1. Prove hosted login mints a key

**No new code.** Run the real OTP against the deployed host and see whether a
`gecko_sk_…` comes back. `PRIVY_APP_ID` and `MONGODB_URI` are both filled, so
`build_login_service_from_env` should return a live service and `/auth/login/*` should
answer rather than 503.

*Why first:* everything gated sits on it, and it is the cheapest possible falsification.
`HttpPrivyServerClient` asserts a passwordless wire shape verified empirically once; if
Privy's behaviour differs today we learn it for free, before anything is built on top.

**Done when:** one email round-trip yields a key, or yields a precise failure naming which
call broke. The founder runs it — the code lands in their inbox.

### 2. Bind the wallet at login

**This should need no new Privy call.** `authenticate` returns
`{user: {id, linked_accounts: [...]}}` and `_identity_from_payload` already walks that list
(today for `type == "email"`). If a Privy account created at signup carries its embedded
Solana wallet in the same list, enrolment reads the wallet id and address out of a response
we already receive and writes the `WalletBinding` at login.

**That "if" is the unverified part, and it is why task #1 comes first.** Two things are
assumed and neither is confirmed: that the Privy app is configured to create an embedded
wallet at signup, and that `authenticate` returns the wallet's **id** rather than only its
address. A binding needs the id — the address alone is a pubkey we could not ask anyone to
sign for. Task #1's live run shows us the actual payload; if the wallet id is absent there,
this task grows one authenticated `GET /v1/wallets` lookup for the user, which is a read we
already know how to make (`PrivyBackend._fetch_address` does the single-wallet form). It
does not grow a create call under any outcome.

That retires the design in `2026-08-13-wallet-enrolment.md` **in its outward-facing half**:
no create call, no real fundable account made by us, nothing for the founder to authorize.
What survives from it is the rule that mattered — **a `wallet_id` is never accepted from a
caller**; here it is never accepted at all, it is read from the identity provider's own
answer about the person who just proved their email.

The pubkey is still FETCHED, never asserted: the signer's three-way check re-derives it
from the custodian at signing time, so a stale binding refuses rather than signs.

**Done when:** an offline test drives a fake Privy `authenticate` payload through login and
finds the binding written; and a payload with no embedded wallet writes nothing and leaves
the account in mode A.

### 3. Wire the store mount

Config and deploy only; the code merged in #423. The mount is built only when a wallet
directory exists AND the gate stance is on AND the name is in the gate's scope — so this
is `GECKO_GATED_SURFACES` gaining `store`, and a redeploy.

**Done when:** `/store/mcp` answers 403 without a key and lists `prepare_purchase` with
the buyer-bound description with one.

### 4. The settle tool

The tool that produces a signature. It **must not** live in `gecko/prepare_purchase.py` —
that module's first paragraph says it holds no key and has no path to one, the public docs
repeat it, and it runs behind a PUBLIC mount. A new module, reachable only from the gated
mount, that takes a receipt-backed plan and asks the custodian to sign.

Everything the signer already enforces stays in force and is not re-implemented here: the
spend policy's five caps, the three-way `sol_delta_account == fee_payer == backend.pubkey`
check, the empty-signature-slot refusal, and the exact-binding requirement.

**Done when:** an offline test with a fake signing backend completes plan → receipt →
signature → refusal-on-mismatch, before any live run.

### 5. Durable spend ledger

Must precede the first hosted spend, not follow it. On Fargate a task replacement resets
`FileSpendLedger`'s windows, and with more than one task each has its own budget — the cap
multiplies by task count. EFS per the architecture pass; **never a document store**.

It stays **ADVISORY wherever it lives**, because the process it bounds can truncate it.
Making it a control means moving the counter to the key holder, which is the unanswered
1Claw question and is explicitly not in this spec.

**Done when:** two processes sharing one ledger path cannot exceed one daily cap between
them, proven offline.

### 6. One real client, end to end

The acceptance sentence. Claude web and Grok both speak the two-endpoint SSE shape we
serve.

**Report transport results separately from client-behaviour results.** A transport-level
client proves the handshake and the tools; it cannot tell us how a model selects a tool,
how the client reconnects at the 1800s stream cap, or how it renders a refusal. Those are
observations about a client, and either we observe them on the real one or we do not
report them.

**Done when:** the sentence at the top of this document is true, once, and the signature,
slot, and CU are recorded the way the other twenty were.

## Non-goals

- Making the advisory counter a control (needs the key holder — the 1Claw measurement).
- A public catalog, a payment rail, or custody. Unchanged invariants.
- The provider-surface work (roadmap goal 2). The loader trust mode that guards it already
  landed in #424; the rest waits.
- Any claim from a higher rung of the ladder than the task that is green.

## How we work through it

Pragmatic-programmer sizing, at the founder's direction: small tasks, each one shippable
and independently falsifiable, offline simulation first and live smoke last (Pattern B).
One PR per task. A task that cannot be falsified offline before it is run live is not
split finely enough yet.
