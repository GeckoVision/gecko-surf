# How it works — the four shipped defenses

> Grounded in the engine as shipped: `gecko/surfaces.py` (trust anchor + quarantine),
> `gecko/sanitize.py` (the sanitizer), `gecko/caller.py` (the auth-host firewall).
> Every claim below maps to code you can read. Status: **all Live.**

The design posture is one sentence: **the spec is untrusted input, so no
attacker-controlled byte is trusted to decide where auth goes, what a tool arg is
filled with, or what instruction the agent reads.**

---

## 1. Out-of-band trust anchor — where auth is allowed to go

*(`gecko/surfaces.py` — `TrustAnchor`, `anchor_for`)*

A poisoned spec's most direct attack is to point the base host at
`evil.attacker.test` (via `servers[].url`) so your token ships to the attacker on
call 1. The fix is to **never derive the auth target from the served spec.**

- The set of hosts your injected auth may reach (`trusted_hosts`) comes from
  **provenance**, computed at provisioning:
  - an explicit **`base_url`** you supplied → its host is the anchor (`pinned`);
  - else the **URL that actually served the spec bytes** → that host is the anchor;
  - else → **no anchor**, state `unverified`.
- **A local file is deliberately NOT a pinning provenance.** A spec saved to disk (a
  registry download, a vendored-spec PR) is no more trustworthy than a raw in-memory
  dict, and its `servers[0]` is attacker-controlled — so it fails closed to
  `unverified` (no auth ever leaves the process).
- **Fail closed:** `may_inject_auth` is true **only** for a `pinned` surface with at
  least one anchor host. `unverified` and `quarantined` surfaces get recorded-mode,
  no-auth behavior.

The `servers[]` array in the spec is used to *address* a request, but it is **never**
the thing that decides whether your secret is allowed to go there.

---

## 2. The sanitizer — neutralizing poisoned text, values, and keys

*(`gecko/sanitize.py`)*

Every human-readable and value-bearing field in a spec is scrubbed before it reaches
the agent-facing tool. It is a small, **deterministic** rule set (a curated regex list
+ length caps) — **not** an LLM call, because it runs on every comprehended op and
must never itself ship untrusted text to a model.

**Free text the agent reads** (`description` / `title` / `$comment`) is scanned for
instruction shapes and, if it trips one, replaced wholesale with a neutral note (the
whole field is untrusted once it carries an injected instruction). Three rule classes:

- **prompt injection** — `ignore previous instructions`, `disregard the above`,
  `forget everything`, `new instructions:`, `you are now`, `system prompt`, …
- **secret exfiltration** — an exfil verb (`echo`/`include`/`paste`/`send`/…) aimed
  at a *specific* secret noun (`private key`, `seed phrase`, `api key`, `access
  token`, …). Deliberately **not** bare `token`/`secret`/`key`, which appear all over
  legit docs — a rule needs the instruction shape.
- **fund routing** — `transfer`/`route`/`send`/… aimed at `funds`/`balance`/`money`,
  or `route … to <crypto-address>`.

**Value channels the arg-filler emits** — `default` / `example` / `const` / `enum` —
are **dropped** if a member looks like a real secret or trips a danger rule:

- **Secret-shaped values** dropped everywhere: PEM private keys, raw 32-byte+ hex,
  Solana base58 secret keys, `sk-…` (OpenAI-style), Stripe `sk_live_`/`pk_test_`,
  AWS `AKIA…`, GitHub `ghp_…`, Google `AIza…`, Slack `xox…`, BIP-39 seed phrases.
- **Crypto-address-shaped values** (EVM / base58 / bech32) dropped **only in mandated
  request channels** (`const`/`default`/`enum` — the values JSON-Schema sends or the
  agent must pick). They are **not** flagged in *hint* channels (`example`/`examples`)
  where a legitimate example may legitimately show an address — those are still scanned
  for secrets and injection. This mandated-vs-hint carve-out is what keeps zero false
  positives on legit request examples.

**Poisoned property keys** are dropped too: a JSON-Schema property whose *name* is an
injected instruction, or is absurdly long (> 128 chars), is removed — because a key
reaches the agent as a *field name* in recorded mode.

**Evasion hardening** (the `_fold` pass, applied before every match):

- **zero-width / format chars** (Unicode category `Cf`) are stripped, so
  `Ignore prev‌ious instructions` can't void the rules;
- **NFKC** folds fullwidth/compatibility forms to ASCII;
- **Cyrillic/Greek homoglyphs** fold to their Latin lookalike, so a pure-Cyrillic
  `іgnore all previous instructions` still trips the scan.

**Fail-closed depth + generic recursion:** every subschema is walked at every depth by
**generic** recursion — no applicator allowlist, so a future or obscure keyword can't
smuggle poison past a hard-coded list. Nesting **deeper than the cap fails closed**:
an unscannable deep subschema (or composite value) is treated as poisoned, never
assumed clean.

Any field that trips a rule sets a **poison flag** that propagates up to the tool def
and turns into a **quarantine** (§4).

---

## 3. The auth-host firewall — fail-closed at call time

*(`gecko/caller.py` — `build_request`)*

The trust anchor (§1) decides the allowlist; the caller **enforces** it at the moment
of building the request, so even a "meant-well" agent that proposed a call to a drifted
host can't leak the token:

- **Anchor is a non-empty set, target host not in it** → a `pinned` surface whose
  base URL drifted (poisoned `servers[]`): **refuse loudly**. The error names **only
  the host** — never the auth value.
- **Anchor is an empty set** (quarantined / unverified — no pinned host) → **never
  send the secret**, but do **not** hard-fail: proceed in no-auth mode so the agent can
  still make un-drifted, public calls.
- **Anchor is `None`** (low-level / unit use where the caller vouches) → inject as
  given.

The SSRF guard (`validate_public_url`) also runs before any live request, since the
base URL came from an untrusted spec.

---

## 4. Quarantine — a poisoned surface injects no auth

*(`gecko/surfaces.py` — `spec_is_quarantined`, the `State` machine)*

A surface has one of three states: `pinned`, `unverified`, `quarantined`. **Only a
`pinned` surface may have your auth injected.** A surface is born **quarantined**
(recorded-only, no auth, until a human clears it) when it is:

- **from human docs** — recovered by `from-docs` (the parser guessed, so it is
  poisoned-until-proven), marked by the docs-reader's generator stamp;
- carrying an **unreviewed / low-confidence** honesty flag (`x-review`, low
  `x-draft-confidence`);
- carrying the sanitizer's **`x-poison-flag`** from §2/§3.

Quarantine is the safety net behind the best-effort text defense: even if a cleverly
encoded injection slips the sanitizer, a flagged surface never gets your live
credentials — the worst case degrades to a $0, no-auth, recorded call.

---

## Why this composes into the guarantee boundary

The four defenses stack so the **hard guarantee** (no attacker value routes into an
arg while auth is live) holds even when the **best-effort** text layer is evaded:

1. the sanitizer **drops** the dangerous value and sets the poison flag;
2. quarantine turns the flag into **no-auth, recorded-only**;
3. even absent a flag, the auth-host firewall **refuses** a drifted target;
4. and the trust anchor means auth was **never** aimed by the spec in the first place.

That layering — not any single regex — is the defense. See
[showcase.md](showcase.md) for the exploits each layer blocks and the battle-test
numbers, and [monitoring.md](monitoring.md) for what is free vs. paid.
