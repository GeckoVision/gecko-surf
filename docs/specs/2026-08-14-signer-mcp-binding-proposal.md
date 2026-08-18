# Proposal for `@orquestradev/signer-mcp`: an optional `binding` on `sign_transaction`

**Status:** DRAFTED, NOT SENT. Posting to a repo we do not own is the founder's call
([[confirm-before-outward-push-pr]]). Channel undecided — GitHub issue vs. direct message.

---

## The message (issue body, ready to post)

### Optional `binding` argument on `sign_transaction` — refuse bytes that were not the ones checked

Hi Berkay — we compose with this server from the Gecko side and it works well. One small
addition would make it strictly safer for anyone doing verify-then-sign, and it costs you
about ten lines.

**What we do today.** Gecko simulates a Solana transaction against a live node, produces a
receipt attesting *those exact bytes*, and hands the caller the unsigned base64 plus a
`binding` — a digest over the transaction's **message**. The agent then calls your
`sign_transaction` with that base64.

**What your server does, and why it is already a good fit.** You decode the wire
transaction, merge signatures into the map, and re-encode:

```ts
const signedTx = { messageBytes: decodedTx.messageBytes, signatures: mergedSignatures };
```

You never rebuild the message. That is the property that makes verify-then-sign possible at
all, and several other paths break it — Solana Actions and Solana Pay both *mandate* that a
client overwrite `feePayer` and `recentBlockhash` on an unsigned transaction, which destroys
any attestation taken beforehand. Yours does not, and we would rather say so than assume it.

**The gap.** `sign_transaction` signs whatever base64 it is handed. If an agent holds a
receipt for transaction A and passes transaction B — through a prompt injection, a
compromised peer tool, or an ordinary bug — your server produces a perfectly valid signature
for B, and nothing in the chain of custody notices. Custody protects the key; it does not
protect the action.

**The proposal.** One optional argument:

```ts
sign_transaction({
  transaction: string,     // base64 wire, unchanged
  binding?: string,        // sha256 hex over the message; when present, MUST match
  binding_strength?: 'exact' | 'structural',   // default 'exact'
})
```

When `binding` is present and does not match, refuse and sign nothing. When it is absent,
behave exactly as today — so this breaks no existing caller.

**The canonicalization, so you can implement it without reading our source.** Publishing
this is on us; it has lived only inside our `txbind.py` until now.

```
message_bytes = the serialized MESSAGE (not the transaction — signatures are not included)
version       = "legacy" | "v0"        # the wire layout of that message
strength      = "exact" | "structural"

# `structural` zeroes the 32 blockhash bytes first, so the digest is stable across
# re-quoting. `exact` covers the blockhash, and is what an attestation should use.

binding = sha256(
    utf8(strength) || 0x00 || utf8(version) || 0x00 || message_bytes
).hexdigest()
```

Two rules that matter as much as the formula:

* **Refuse, never digest, when the message loads accounts from an address lookup table.**
  The bytes commit to the table and the indexes, never to the addresses they resolve to, so
  a digest there would attest something it cannot see.
* **The strength and the version are folded INTO the digest**, so a `structural` digest can
  never be mistaken for an `exact` one, and a legacy digest can never be mistaken for a v0
  one.

Worked example, if useful for a test — a one-instruction memo transaction, fee payer
`DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL`:

```
exact      4b82fbf3cb15567875fcff4679cd50d45e88061746b694195c2bda815ea07e7d
structural 07a5dd373695eb24d5f04612413c24627f8d68fba1d400e8e6cf4540a83ff48c
```

**One more thing, take it or leave it.** `sign_and_send_transaction` signs and broadcasts in
one call, which removes the only window in which signed bytes can be checked before they
reach the chain. If an agent has both tools mounted, it can bypass any verification by
choosing the convenient one. We refuse that method on our own signing path for exactly this
reason. A note in its description would be enough.

Happy to send a PR rather than an issue if you'd prefer — say the word and we'll open one
against your interface, not our idea of it.

---

## Why we are proposing rather than routing around it

We already ship the other half: `verify_signed_transaction` on our public surface takes the
signed bytes plus the binding and answers whether they match, **before** anyone broadcasts.
That is the strongest thing a party holding no key can do — it makes a substitution
detectable while it is still free.

Refusing at the signer is strictly better, because the bad signature is never produced.
Neither replaces the other: ours works with any signer, his would work with any verifier.

Same shape as the Arazzo arity contribution — we found a gap, and the useful move is to
propose the fix upstream rather than build a private workaround.

## What sending this costs us

Nothing technical: the canonicalization is a digest over public bytes, and publishing it
does not weaken anything. An attacker who can rewrite our response can already recompute a
digest over their own bytes — which is precisely why the binding is a checksum against
corruption and not, on its own, a control against an adversary. This proposal narrows that:
with the binding carried out-of-band and demanded by the signer, a substitution has to
survive two independent origins instead of one.
