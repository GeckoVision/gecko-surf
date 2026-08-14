# Making the LetMeBuy store work, end to end, from a chat client

**Date:** 2026-08-14 · **Status:** approved, task #1 starting · **Supersedes nothing;
narrows** `2026-08-13-roadmap.md` goal 1 into shippable pieces

## The one sentence everything below is derived from

> A person opens Claude web or Grok, adds our hosted surface, says *"order a water from
> Jonas Bar at table 11"*, approves once, and the order lands on mainnet — the receipt's
> predicted CU matching what the chain charged, and no key ever on our machine or theirs.

That is the acceptance test. A task exists only if it makes that sentence true. Nothing
here is done until a real client does it once.

**"Approves" WHERE — the one unresolved word.** The store ruling (§3) says the caller signs
and pays; a chat client has no wallet, so the signature cannot happen inside Claude web or
Grok. Something outside the chat closes the loop, and which thing it is decides whether
this spec is finished at #3 or runs to #6:

| Who signs | What the chat client shows | What we must build |
|---|---|---|
| **The human, in their own wallet** | the check, then a link/QR; they sign in Phantom/Privy and the order lands | nothing more — the check already exists today |
| **The agent, via its own signer** (Privy, 1claw, paybox-style) | the whole loop, unattended | nothing of ours; we compose their signer |
| **Us, for a user who signed up with us** | the whole loop, unattended | #2 + #3b + #4 + #5 — a different product |

Rows 1 and 2 keep *holds no key, never signs* intact on the public surface. Row 3 is the
hosted-signer product and is explicitly NOT this loop. Until this is chosen, read
"approves once" as row 1.

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

### 1. Prove hosted login mints a key — ✅ DONE 2026-08-14

**Result: green.** `noreply@geckovision.tech` → Privy OTP → verified identity → a real
`gecko_sk_…` from the hosted registry, HTTP 200. No new code was needed. The endpoint was
live all along: `POST /auth/login/start` answers `400 "enter a valid email address"` on an
empty body, not the `503 "login_disabled"` that a missing `PRIVY_APP_ID` or registry
produces — which also corrects an earlier claim in this session that hosted key issuance
was disabled in production. It was not. `GECKO_OTP_FROM` was never the blocker for this
path; that variable belongs to a **different, unbuilt** SES path (`boto3` is not even a
declared dependency).

Two things the task surfaced that reading the code would not have:

* **The handle IS the email.** `HttpPrivyServerClient.start` returns the address, because
  Privy's `authenticate` takes `{email, code}` and issues no server-side login id. A
  verify against a *different* address than the code was issued for returns
  `401 "invalid or expired code"` — the same answer as a wrong code, an expired code, or
  any other non-2xx, because the client collapses them all deliberately so the response
  cannot distinguish "wrong code" from "unknown email". Good security, poor diagnosis:
  **budget one wasted round-trip per address confusion.**
* **A login-minted key is `enabled=False`.** Self-service login yields an IDENTITY and
  zero access; `GeckoKeyResolver` resolves a key only if its record exists AND is enabled,
  so a fresh key does not even resolve (`403 invalid_token`, not `not_enabled`). See the
  store section below — this is what the founder's ruling replaces for our own storefront.

**Handling note, recorded because it will happen again:** the key was printed in full while
being read back. The guard matched a field named `key`; the endpoint returns `api_key`.
**Redact by PATTERN (`gecko_sk_`), never by field name** — a name can be different, a
prefix cannot. The exposed key is inert (disabled, proven against `/birdeye/mcp`), but it
must never be the record that gets enabled; mint a fresh one for that account first.


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

### 3. The store surface is OPEN — the check is the gate

**Founder ruling, 2026-08-14.** The store works like an x402 channel: **open**, no key
wall. The agent receives a *"payment check"* — the pre-signed transaction, i.e. the
unsigned bytes plus the receipt that attests they will land — and **to conclude the
transaction it must sign and pay. Otherwise the loop does not close.** How we eventually
charge is unresolved and deliberately not decided here.

This is the better design, and it is worth saying why rather than just recording it: **the
economic gate replaces the access gate.** An access gate on our own storefront would mean
approving each buyer by hand (`gecko keys enable` + `gecko keys grant`), which is right for
a paid third-party surface whose quota WE pay for — `birdeye` — and wrong for a storefront
where the buyer spends their own money. Nobody needs permission to be handed a check. They
need a wallet to settle it. A caller who plans a purchase and never signs has consumed a
simulation; a caller who wants the coffee pays for it themselves.

**Why `birdeye` being walled is not a counter-example** (founder, same ruling). It is
walled for two reasons that have nothing to do with access-control philosophy: the key we
serve it with is a **trial** key, so serving it commercially is not ours to do; and
**our intent is to be an engine, not a marketplace** — reselling somebody else's paid API
to strangers is precisely the drift the thesis forbids. It also exists mainly for internal
use (a co-founder, and tests). So the two surfaces differ because the money differs: on
`birdeye` the quota is OURS, on the storefront the money is the BUYER'S. Gate what you pay
for; hand a check for what they pay for.

It also collapses a whole class of attack surface. On an open, caller-signs surface the
buyer is supplied by the caller — mode A — and that is *correct*, not a weakness: naming
somebody else's address only yields bytes you cannot sign. The impersonation the wallet
binding exists to prevent is only reachable where WE hold the signing authority.

**Consequences, stated plainly:**

* Task **#2** (bind the wallet at login) is **not on this loop's critical path.** A binding
  matters only when we are the signer. It stays built (#420, #422) and idle.
* Task **#4** (a settle tool) is **not built for this loop.** The caller signs. Building a
  hosted signer here would contradict the boundary the whole product rests on — *holds no
  key, never signs* — on the one surface that is public.
* The `store` mount from #423, which is *unbuildable unless gated*, is therefore **not the
  shape this ruling asks for.** The open storefront is what `orquestra` already serves
  today: `find_start`, `list_stores`, `prepare_purchase` in mode A, keyless, public.

**Done when:** an open surface hands back a check — plan, accounts, receipt PASS, unsigned
bytes — to an unauthenticated caller, and refuses to be anything more than that.

### 3b. Wire the gated store mount (deferred, not deleted)

Config and deploy only; the code merged in #423. The mount is built only when a wallet
directory exists AND the gate stance is on AND the name is in the gate's scope.

**Deferred by the ruling above.** It remains the right shape for the *other* product — a
user who signed up with us and wants their agent to complete a purchase without a human
touching a wallet. That product needs the binding, the settle tool, and the durable
ledger. It is not this loop, and mixing the two is how the public surface would quietly
acquire a signing path.

**Done when:** (deferred) `/store/mcp` answers 403 without a key and lists
`prepare_purchase` with the buyer-bound description with one.

### 4. The signer is somebody else's — see `2026-08-14-who-signs.md`

**Replaced by research, 2026-08-14.** Four specialists converged: the signer is **PayBox**
(MoonPay), reached as an MCP connector the user adds in their own chat client. Its
`request_wallet_sign` / `op: "solanaTransaction"` takes `transactionBase64` and returns
`signedTransactionBase64` — no rebuild, no re-stamped blockhash, no injected instruction —
so our attestation, taken over the message, survives the signature byte-for-byte.

We build **nothing that signs.** The only proposed build is
`verify_signed_transaction(transaction, binding, network)`: `verify_handoff` at
`require="exact"`, already written and not exposed. Keyless, stateless. It proves what was
signed is what was checked, before broadcast.

Also settled there: Actions/Blinks is **ruled out by specification** (an unsigned
transaction's fee payer and blockhash MUST be overwritten by the client, which is the
mutation the binding is taken against), and no custody provider in the market — Turnkey
included — can express "only sign if this hash matches a simulation someone else
performed." That is the whole-category answer to the 1claw question.

### 4b. The hosted settle tool (deferred with §3b)

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
