# Jito — the painful-API showcase

**The problem:** Jito's Block Engine (low-latency sends, atomic bundles, tips — real
money infrastructure on Solana) publishes **no OpenAPI, no llms.txt, nothing
machine-readable**. The entire API lives in one ~93KB human doc page that even
**403s default script user-agents**. It's JSON-RPC (the method rides in the body),
the real routes are per-method, auth is an optional rate-limit UUID header, and the
gotchas — max 5 transactions per bundle, a ≥1000-lamport tip *inside* the
transaction, base64-vs-base58 encoding flags — are buried in prose. An agent
reading those docs gets the first call wrong.

**The Gecko path (all reproducible offline, $0):**

1. **`gecko from-docs https://docs.jito.wtf/lowlatencytxnsend/`** recovered a draft
   OpenAPI from the prose — all 5 JSON-RPC methods, auth scheme included. The draft
   is born quarantined (a `from-docs` surface is poisoned-until-proven).
2. **A human review pass** produced [`spec/jito_openapi.json`](spec/jito_openapi.json):
   real routes confirmed from Jito's own curl examples, JSON-RPC envelopes pinned
   (`method` as a `const` in the body), gotchas encoded in the descriptions, and the
   landed-vs-in-flight near-dup pair disambiguated.
3. **The engine comprehends it** into question-shaped tools an agent picks by intent.

**The numbers (regenerated live by `demo.py` + the tests — they can't drift):**

| Check | Result |
|---|---|
| Operations comprehended → agent tools | 5 → 5 |
| First-call-correct scorecard (intent → op → well-formed call) | top-1 **5/5** · well-formed **5/5** |
| `gecko test` auto-generated suite | **10/10** |
| `sendBundle` wire target | `POST https://mainnet.block-engine.jito.wtf/api/v1/bundles` (method in body) |
| Agent-native emit (`--emit-dir`) | Jito's own `llms.txt` · `gecko.json` · `/.well-known/gecko.json` · `tools.md` |

The whole bundle journey — *which accounts do I tip* → *send an atomic bundle* →
*track it in flight* → *did it land, which slot* — routes to the correct operation
at rank 1, and every call is falsified in **recorded mode** before a lamport moves.

```bash
uv run python examples/jito_demo/demo.py            # the showcase, offline
uv run pytest examples/jito_demo/ -q                # pin the claims (7 tests)
uv run gecko test examples/jito_demo/spec/jito_openapi.json   # the generated suite

# Jito's agent-ready surface, generated (what a provider would host):
uv run --extra serve python -m gecko.serve examples/jito_demo/spec/jito_openapi.json \
  --emit-dir /tmp/jito-agentnative --site-url https://docs.jito.wtf
```

**Honest scope:** recorded-mode only — nobody demos live MEV sends; a live
`sendBundle` needs real signed transactions and real tips. The REST tip-floor
endpoint (`bundles.jito.wtf`) is excluded: it lives on a different host and the
engine doesn't support per-operation server overrides yet (roadmap). Two genuine
engine findings from this build: Solana's 88-char base58 *transaction signatures*
false-positive the secret-shape detector (44-char pubkeys don't), and doc sites
that 403 script user-agents need a browser-shaped fetch path.
