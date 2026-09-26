# ADR — On a surface with no signing window, the user's own wallet asks us for the transaction

**Status:** Proposed · 2026-09-26. It needs two founder answers (see Open questions) before it is Accepted.
**Deciders:** founder, on a `staff-engineer` ruling
**Supersedes:** for surfaces with no signing window only, the "Not doing: `prepare_payment_link`"
line in `docs/specs/2026-08-15-next-moves.md`, and the Solana Pay half of "Ruled out: Actions
and Blinks, by specification" in `docs/specs/2026-08-14-who-signs.md`. Both still hold for any
host that draws a signing window in time. **Superseded by:** nothing.

## Context

`@gecko_check_bot` (`POST /telegram/webhook`, `gecko/telegram_webhook.py`, live 2026-09-26)
lists menus and prepares purchases for a person in a chat. It cannot get them signed. The goal
is that the purchase completes from a chat (Telegram, WhatsApp, Grok) and Gecko never holds a
key and never signs.

Measured, and documented by the vendor:

1. **Claude web works.** Mainnet `4pYeAX…` (2026-09-26 06:00:55 UTC): `prepare_purchase` →
   PayBox signs in its in-chat window → `submit_transaction` → landed, and the merchant was
   notified. The first attempt died on the ~60 s blockhash; a re-prepare landed.
2. **Grok fails.** PayBox draws the window only after the agent's reply finishes. It still
   failed after the key was connected and the two-turn discipline was followed. The blockhash
   dies in between.
3. **Claude Code never draws the window.** A request stayed `pending_signature` for 100 s with
   `approval_mode: autonomous` and `agent_signer_recorded: true`.
4. **PayBox signs only in the window, or with a `pbxk1.` agent key the SDK uses on the
   requesting machine.** It documents no server-side delegated signer. `autonomous` is a
   grant, not a signer. Requests are client-scoped. An arbitrary `solanaTransaction` is signed
   as given, with no blockhash refresh. For a hosted bot, PayBox's only path is a per-user
   `pbxk1.` key on our server.

The earlier ruling against Solana Pay rested on one clause, found in both the Actions and the
transaction-request specs: if the returned transaction carries no signature, the wallet MUST
overwrite `feePayer` and `recentBlockhash`. That destroys an `exact` binding taken beforehand.
The ruling was right for the question it answered, "PayBox with an exact binding, or Solana
Pay?" On a surface with no window the question is different: "Solana Pay, custody, or nothing
lands?"

A second fact narrows the cost of that clause. A transaction request tells us the wallet's
`account` BEFORE we build. If `feePayer` is set to that account, the fee-payer overwrite is a
no-op. Only the 32 blockhash bytes change, and the `structural` binding ignores the blockhash
by design. A wallet that re-serializes anyway (the spec calls this behaviour "undefined") is
caught after the fact by the field-level receipt, not before.

## Decision

**One: Telegram and WhatsApp use a Solana Pay transaction request.** The bot sends an https
link, plus a QR code, that hands off to `solana:<our endpoint>`. The user's own wallet GETs the
label and icon, then POSTs its `account`. On POST, and only then, the engine builds the
transaction from the pinned request through `BuildCall`, with `feePayer` = buyer = the POSTed
account and a fresh blockhash. It rehearses the transaction, compares the effect with the pin
field by field, and either returns the transaction or refuses with the field named. The wallet
signs and sends. After it lands, we find the transaction by (account, structural binding), read
the ledger, and write the receipt. The comprehension logic lives in a new
`gecko/payment_request.py`. The route and the Telegram rendering stay thin transport. **No new
adapter seam:** the signer is the user's wallet, not something we inject.

**Two: Grok gets host-aware ordering now, as instructions only.** Do not prepare until the
signer is live. On hosts that draw the window after the reply, end the turn right after
`request_wallet_sign`. Re-prepare if the blockhash expired. When that is not enough, the agent
offers the same transaction-request link as in One.

**Three: durable nonces are a Grok/PayBox-only follow-up, gated on measurement.** The nonce
authority is the user, never Gecko. The nonce account is created once, from any host that
works. A nonce transaction is re-simulated before it is submitted, and checked with
`still_landable` and against pin expiry. It is never combined with One: the spec requires the
wallet to overwrite `recentBlockhash`, which is where the nonce value lives.

**Four: a Privy server wallet per Telegram user is not built until the founder rules on Open
question 2.** If it is built, it follows the existing rulings. Bind on CREATE, never on
assertion. Identity comes only from a Telegram-authenticated update or Mini App `initData`
verified by HMAC. `signTransaction` only. An in-enclave Privy policy with a program allowlist
and a per-transaction cap. Disabled unless explicitly configured, and the first live create is
founder-run.

**Five: a PayBox `pbxk1.` key per user on our server is refused.** It is an extractable user
signing key in our process: worse than an enclave key we can only ask to sign, and a breach of
invariant #1.

**Six: the routing record.** `opaque_ref → {chat_id, pin, created_at, expires_at}` lives in a
TTL store. A link lives at most 15 minutes. After a POST, the record lives until
`last_valid_block_height` plus one confirmation pass, and is deleted as soon as the receipt is
delivered. It never goes into the corpus, a durable database or a log line. The receipt we keep
carries no `chat_id`. This is our own user's identifier, allowed under the 2026-08-13 identity
ruling, and not an API response payload or customer data. Invariant #1's wording should say so.

## What this forbids

- Building a transaction-request transaction anywhere except the POST handler.
- Any Gecko signature on a user's transaction, including a partial signature to "freeze" the bytes.
- A `feePayer` other than the POSTed account on the transaction-request path, and therefore a
  Kora or gasless relay on that path.
- Returning a transaction whose rehearsal disagreed with the pin.
- Claiming a pre-broadcast check on a surface with no window. The claim there is "rehearsed when
  your wallet asked, verified by field after it landed", and nothing stronger.
- Durable nonces on the transaction-request path, and Gecko as any nonce authority.
- Accepting, storing or proxying a user's `pbxk1.` key or PayBox token on any hosted Gecko component.
- Binding a wallet by assertion, or `signAndSendTransaction`, on any hosted signer.
- Keeping `chat_id` past its TTL, or anywhere but the routing record.
- Adding a fifth adapter seam for "the signer".

## Alternatives, and what they cost

**Privy server wallet per user (Four), first.** No window, no race, one tap. It is custody by
authorization: the key cannot be extracted, but we operate it on request, and a hijacked
Telegram account spends within the policy. It needs a regulatory read, and a founder ruling
that the 2026-09-26 goal does not overrule the 2026-08-13 one. Possibly right later; not the
first thing to ship.

**PayBox `pbxk1.` key per user (Five).** Refused. Everything Four costs, plus key custody without
an enclave.

**Durable nonce everywhere (Three).** Fixes the race on PayBox hosts. It turns an expiry that
protected the user into a signed transaction that never expires, allows one in-flight
transaction per nonce account, and costs rent plus a setup signature for every user. It does
not work with Solana Pay.

**Instructions only (Two).** Free. It narrows the race without removing it, and does nothing for
Telegram.

## Reversibility

**One-way:** the custody line (Five, and Four's gate), and the public claim for surfaces with no
window. Once a surface says "verified after it lands", walking it back to "checked before it
counts" needs a mechanism, not an edit. The link URL shape becomes one-way once links are out
in chats. **Two-way:** file layout, TTL values, the wording of Two.

## Consequences

**The guarantee is two-tier, and we say so.** Hosts that draw a window in time keep the
pre-broadcast exact binding (`verify_signed_transaction`). Surfaces with no window get
"rehearsed at request, verified after landing". The receipt names which tier it came from.

**One endpoint serves every surface with no window.** Telegram, WhatsApp, and Grok as the
fallback get the same engine path with different renderers. Adding a chat surface is a renderer
change, never an engine change.

**The receipt reads the chain, not a reference key.** We correlate by account plus structural
binding, so the program instruction does not have to carry a Solana Pay `reference`. If wallets
turn out to re-serialize, correlation falls back to a field comparison of the decoded landed
transaction, and the receipt reports the mutation.

## Open questions (founder)

1. On a surface with no window, do you accept "rehearsed at request, verified by field after
   landing" in place of "the signed bytes are the checked bytes"? Yes supersedes the
   2026-08-15 line against `prepare_payment_link`.
2. Does "Gecko never holds a key and never signs" (2026-09-26) supersede "hosted signer YES"
   (2026-08-13)? If it does, Four is dead, not deferred.

## Evidence

Mainnet `4pYeAX…` (Claude web, window path). The 2026-09-26 connector test: `pending_signature`
at 10, 55 and 100 s with `autonomous` and a recorded signer. PayBox `llms-full.txt`
(concepts/requests#the-signing-window) and the `@paybox-sh/sdk@1.0.0` source. The earlier
readings in `docs/specs/2026-08-14-who-signs.md` §"Ruled out". Tx #12 (Privy enclave, per-method
deny-by-default policy) for what a hosted signer would look like.

**To build before this is Accepted (Pattern B):** an offline simulation of the POST handler:
pin → POSTed account → built transaction → rehearsal → refusal by field. Its tests include a
wallet that overwrites the blockhash and one that re-serializes. Then one founder-run mainnet
purchase from Phantom via a Telegram link.
