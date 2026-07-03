# Refugios Venezuela — a live shelter registry, publishable-key gated

**The API:** [Refugios Venezuela](https://refugiosvenezuela.com) (open-source —
[dnsantosuosso/refugio-mapa-venezuela](https://github.com/dnsantosuosso/refugio-mapa-venezuela))
is a collaborative map of shelters + food centers after the 2026 earthquake. It's
**Supabase-backed**, and every call carries a **publishable `apikey`** header (public by
design — printed in their own docs, like a Stripe `pk_`).

**What Gecko does:** comprehends the surface (spec hand-authored from their `/api` page)
into a first-call-correct tool an agent picks by intent — *"a shelter with water and
medical care near me"* — and **injects the publishable key at call time so it never
appears in the tool the agent sees** (invariant #4: auth is invisible to the agent).

| | |
|---|---|
| Operations → agent tools | 1 → 1 |
| Publishable `apikey` hidden from the agent tool | ✅ |
| Without the key (public session) | 0 tools — the gated op is correctly hidden |
| Rich humanitarian filters | `has_water` · `has_medical` · `has_electricity` · `pets_allowed` · status · bbox · coords |

**Served, gated:** `gecko/serve_mcp.py` serves this at
`mcp.geckovision.tech/refugios/mcp` **only when `REFUGIOS_APIKEY` is set** (the repo
carries no key). It's the first `StaticHeaderSession` surface — a static-header session
injects the publishable key; SOS + reportavnzla stay no-auth.

```bash
uv run python examples/refugios_demo/demo.py     # the showcase, offline $0
uv run pytest examples/refugios_demo/ -q          # pin the claims + the auth gating
```

**Notes:** read surface only (the `POST /refugios` write is excluded — the bot is a
consumer). The `apikey` is publishable/public, but treated as auth: injected via the
session, never committed to the repo, never shown to the agent, never logged.
