# `@orquestradev/signer-mcp` — what "hosted + OAuth" would take, and whether it should

**Status:** DRAFTED, NOT SENT. For the founder to raise with Berkay.
**Companion to:** `2026-08-14-signer-mcp-binding-proposal.md` — **the two are independent.**

> The `binding` argument makes his signer **safer**. Hosted + OAuth makes it **reachable**.
> Neither implies the other, and only the first is a small ask.

---

## The problem in one line

```ts
const transport = new StdioServerTransport();   // src/index.ts
```

A stdio server is a **local process the client spawns**. Claude Desktop, Claude Code and
Cursor can do that. **Claude web, ChatGPT and Grok cannot** — they mount a URL, not a
process. So today his signer is unreachable from exactly the surfaces where a
non-developer sits.

That is the whole of the gap. Nothing about his code is wrong; it was built for a different
room.

## What a remote, authenticated MCP server has to provide

Six things. He has the first, and it is the easy one.

| # | Requirement | Notes |
|---|---|---|
| 1 | **Public HTTPS + Streamable HTTP** (or the legacy SSE pair) | mechanical; swap `StdioServerTransport` for `StreamableHTTPServerTransport` |
| 2 | **`/.well-known/oauth-protected-resource`** | tells the client which issuer guards this server (RFC 9728) |
| 3 | **`/.well-known/oauth-authorization-server`** | the issuer's own metadata — endpoints, supported flows |
| 4 | **Dynamic Client Registration** (RFC 7591) | the client registers ITSELF; nobody hand-creates an app per user |
| 5 | **Authorization Code + PKCE, with a consent screen** | the user signs in and chooses what to grant |
| 6 | **Token → identity on every call** | map the bearer to a user, then to that user's signing backend |

PayBox does exactly this — `api.paybox.sh/mcp`, and its docs advertise the OAuth issuer at
`/.well-known/oauth-authorization-server`, an OAuth 2.1 + PKCE flow, and *"no API key to
copy"*. It is the shape, and it is shipping.

*(Requirements 2–5 are the MCP authorization spec's OAuth profile. Verified against
PayBox's published surface; worth re-checking against the current spec revision before
anyone writes code.)*

## The requirement that is not mechanical

**Somebody has to hold the custody credentials.**

Today his 14 backends — Privy, Turnkey, AWS/GCP KMS, Fireblocks, DFNS, Vault, CDP,
Crossmint, Openfort, Para — are configured by **env vars on the user's own machine**. The
user's Turnkey API key never leaves their laptop. That is a genuinely strong position, and
it is a direct consequence of being stdio.

Hosting it inverts that: to sign on a user's behalf from a server, the server needs that
user's backend credentials. Which means **he would be holding custody credentials for his
users**, with the storage, rotation, compromise story, and liability that implies.

**That is not a port. That is becoming a custody product.** PayBox is MoonPay — a regulated
company with MPC infrastructure (MoonX/Sodot) and a compliance function. Reasonable for a
solo open-source maintainer to decline, and no criticism if he does.

## The three honest options

**A. Stay stdio.** Desktop-only, user holds their own keys, zero new liability. His signer
already works there today, and with the `binding` argument it becomes the safest option on
that surface. **This is a legitimate destination, not a failure.**

**B. Hosted, credentials still the user's.** The middle path, and the interesting one: host
the transport and the OAuth, but never hold a raw backend credential — e.g. the user
authorizes his server against *their* Turnkey/Privy org directly, so he holds a delegated,
revocable grant rather than a key. Whether each backend supports that varies per vendor and
is the thing to check before promising it.

**C. Hosted, credentials his.** Full PayBox shape. Maximum reach, maximum liability.

## Why we are not pushing for any of them

We compose with whatever the user already has. Our surface hands back base64 wire bytes,
and every signer we found takes that same primitive — his, PayBox's, Privy's CLI. **We do
not need him to host anything.** For the Claude-web case we point at PayBox; for the
desktop case his signer is already better, because it gives the user 14 backends and keeps
their keys on their own machine.

The reason to raise it at all is that he asked about our protocol, and this is the honest
map of why one signer reaches one room and another reaches a different one.

## The same wall, on our side

Worth saying to him plainly, because it makes the point non-preachy: **we have the identical
gap.** Our public storefront is unauthenticated, so it mounts in Claude web today — but our
*gated* surfaces expect `Authorization: Bearer gecko_sk_…`, and a web connector UI has no
field to paste a header into. We exposed no OAuth endpoints at all (`404` on both
well-knowns as of 2026-08-14).

So our authenticated surfaces are unreachable from Claude web for exactly the same reason
his signer is. The difference is only that our *public* half needs no auth, and a signer's
never can be public.

One thing we do have that shortens the job: `keyauth.AccountResolver` is
`(token) -> account_id | None`, which is precisely the shape an OAuth access-token verifier
needs. The gate, the allowlist and the per-surface grants would not change — only the
resolver. The unbuilt part is the issuer itself.

## Questions to put to him

1. Is a hosted deployment something you want at all, or is stdio the intended shape?
2. If hosted: would you hold users' backend credentials, or only a delegated grant against
   their own Turnkey/Privy org?
3. Which of the 14 backends can even express a delegated, revocable grant to a third-party
   server? (This decides whether option B exists.)
4. Would you rather we point Claude-web users at PayBox and keep your signer as the
   desktop/self-custody path — and is that framing fair to you?
5. Independently of all this: would you take the `binding` argument as a PR?
