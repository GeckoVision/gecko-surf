# Next moves — written 2026-08-15, from a session that ended with a working loop

Everything below is grounded in something measured or observed in the last two days, not
in a wish list. Where a claim is unverified it says so.

## Where we actually are

The full chain closed on mainnet through a hosted signer reached from Claude web:
browse → get a signer → fund → prepare + check → sign → verify → submit → land → a human
is told to make the drink. Signature `2KxAbvtke5s…`, 27,617 CU predicted and charged, and
the prediction was made by a readiness probe **before** the session started.

Since then: prepare went 4,022 ms → ~2,200 ms, the blockhash window went ~46 s → ~60 s,
and geckocoffee gained Cappuccino and Mochaccino so "a coffee" is genuinely ambiguous.

Two commits landed **directly on main** (`d899caa`, `29b2409`) because a checkout fell back
to the default branch after #437 merged. CI passed on both; no revert requested. Recorded
here so it is not discovered later as a mystery.

---

## 1. Deploy — main is ahead of the hosted surface

`d899caa` and `29b2409` are on main and NOT live. That means the deployed surface still
has substring-only product matching and no store-name case hint, so both near-miss bugs
reported from real sessions are still reproducible in production.

    uv run python scripts/live_parity.py     # expect STALE until deployed, MATCH after

**Do this first.** Everything in §2 is testing against a surface that already has it.

## 2. Run the three purchase tests

Funded and ready: the Privy enclave wallet `HNUE5KKT…` (id `vhjrgx9dao8x2nv725n0050o`)
holds 0.75 USDC and 0.0099 SOL; the three cost 0.35 total.

| test | store / product | what it exercises |
|---|---|---|
| water | jonasbar / Water (0.10) | CROSS-STORE discovery — two shops sell water under different names |
| Cappuccino | geckocoffee / Cappuccino (0.15) | an exact name on a four-product menu |
| a black coffee | geckocoffee (0.10 once resolved) | AMBIGUITY — three of four products are coffees |

Driver: `scripts/autonomous_purchase.py --privy --network mainnet`. Note that script's own
header — *"MAINNET IS THE FOUNDER'S CALL, NEVER AN AGENT'S"* — and that it has no `--send`
flag by construction, because a confirmation prompt outlives a blockhash.

The third test is the interesting one: the correct outcome is a REFUSAL that hands the
choice back, then a second call with the exact name. If an agent picks a coffee on its own,
that is the bug, not the feature.

## 3. Sweep gecko-dev — blocked on a destination

`DMjTEZJu…` holds 0.006207 SOL and 0.45 USDC (token account `AzNx1xhh…`, 0.002039 SOL of
reclaimable rent). No destination has been named, and three things make this not a
one-liner:

* It is **geckocoffee's merchant authority** — the account every purchase pays into. The
  0.45 is partly today's takings.
* **Zero SOL means the store cannot be administered.** `add_product` /
  `update_telegram_channel` cost ~5,000 lamports to sign. Leave ~0.002 SOL unless it is
  meant to be truly dormant.
* **Closing the USDC account pushes rent onto buyers.** A purchase credits
  `ATA(authority, mint)`; if it does not exist the instruction creates it and the BUYER
  pays ~0.00204 SOL. Reclaiming 0.002 costs the next customer the same amount.

Order it after §2, so the takings are swept once rather than twice.

## 4. Tell letmebuy about the triple notification — it is not ours

Every purchase produces exactly **three** identical Telegram messages carrying the same
event timestamp. Nine messages, three on-chain transactions, three copies each. All 22
transactions that have ever touched geckocoffee succeeded, so nothing is failing and
retrying.

Most likely cause: **a listener subscribed at three commitment levels** —
`processed` → `confirmed` → `finalized` fires exactly three times per signature, which fits
"always exactly three, never two or four, identical content". Alternatives are three
indexer replicas on one channel, or webhook retries without idempotency. All three are
fixed by deduping on `signature`.

Owner: Jonas (the bot is `@letmebuybot`, his `TELEGRAM_BOT_TOKEN`). Send it as an
observation with the evidence, not a bug report against their competence.

## 5. PayBox autonomous mode — one run settles a documentation contradiction

Their docs disagree with themselves, and the answer decides how much the mode buys:

* `/concepts/model` — *"Regardless of mode, sensitive operations (… signing …) require a
  fresh passkey assertion."*
* `/reference/mcp-tools` — autonomous returns `pending_signature`, *"the signing window can
  sign right away."*

The consistent reading is that autonomous removes the per-operation approval SCREEN while
still needing a live presence token. **Unverified.** One run before/after, watching whether
`request_wallet_sign` returns `pending_signature` or `pending_approval`, settles it.

The custody argument for trying it, recorded because it inverts the obvious intuition: the
passkey window shows `transactionBase64: "AQAAAA…"`. A human approving that is approving an
opaque blob. The actual review — accounts, merchant, price, revert class — happened in our
plan and simulation before the bytes reached PayBox. `always_approve` buys **presence**,
not **comprehension**. Keep the funded balance small; that is the real spend limit.

## 6. Retrieval — the gate fired, and the blurb pipeline is fixture-only

Measured on 2026-08-15 (`private/2026-08-14-local-dense-probe.md`,
`private/2026-07-09-retrieval-arms.md`):

| birdeye archetype | tasks | overlap | BM25 | + dense |
|---|---|---|---|---|
| keyword_echo | 14 | 0.93 | 1.00 | — |
| near_dup_disambiguation | 9 | 0.56 | 0.67 | — |
| **paraphrase_no_overlap** | 17 | **0.06** | **0.00** | see below |

A local 0.21 GB ONNX model (BGE-base via fastembed, no torch) reproduced the hosted
voyage lift: privy recall@3 0.80 → 0.93, birdeye 0.45 → 0.62, OOS held at 1.00. That makes
arm D offline, $0 and CI-runnable for the first time — the property the Atlas arm gave up.

**Two things must happen before any "buy a model" decision, in this order:**

1. **Wire blurbs into a shipped entry point.** No production path passes `blurbs=` today —
   the pipeline is fixture-only, and `scripts/dense_gate.py` structurally cannot run on a
   partly-blurbed surface (`raw[tool_name(op)]` at line 131). Every dense number so far was
   measured with the enrichment DELETED.
2. **Generate parsed blurbs for birdeye and re-run.** Its 17-task paraphrase bucket is the
   only cell large enough to decide anything. Wiring real blurbs took pegana's dense
   paraphrase@3 from 0.50 to 1.00 in a spot check — if that holds on birdeye, the data
   model matters more than the model, which is the founder's standing thesis and is
   currently supported by one small set.

Then: `LocalDenseIndex` behind the existing `DenseIndex` Protocol, as an optional
`gecko[dense]` extra so the core stays dependency-free. `client.search_hybrid_scored()`
already takes one, so this is a class, not a re-architecture.

**Not** a runtime LLM in the purchase path. The caller is already a frontier model and does
"a black coffee → Espresso" better than a 7B would; a wrong pick there produces a passing
receipt, a landed transaction and the wrong drink. If a small local model earns a place it
is at BUILD time — replacing the Haiku enrichment call, whose output is pinned, reviewed and
hash-frozen.

## 7. The data-model items still open

From the two audits, in the order they are worth doing:

* **A declared field-role model.** `_haystack` is one flat concatenation serving ranking,
  out-of-scope gating and embedding, and a gating bug shipped because of it. Roles
  (`intent` / `reference` / `identity` / `schema`) were applied ad hoc in `intent_tokens`;
  they should be the model, not a tuple in one method.
* **Provenance on catalog fields.** The program surface has a full ladder in
  `provenance.py`; the catalog has none, so a provider summary, a generated blurb and a
  scraped field name are concatenated indistinguishably.
* **Dense store discipline.** No delete/tombstone (orphans acknowledged at
  `search.py:189`), no `surface_rev` (which `CallOutcome` already has), and `upsert` never
  asserts `doc.surface_id == self.surface_id`.
* **The auth filter silently deletes retrieval.** privy under `public_session()`: 159 ops,
  6 usable, `search_scored(...) == []`, scorecard `r@k = 0.00` with **`OOS = 1.00`**. The
  never-empty invariant does not survive the filter.

## 8. The binding's second origin — the known weak point

`txbind.message_binding` is an **unkeyed digest shipped in the same response as the bytes
it covers**. Strong against a WRONG plan; weak against a SUBSTITUTED one, because anyone
who could rewrite the response could rewrite both. The only comparison in the repo whose
two sides have different origins is `--expect-binding`, and its second origin is a human.

Closing it needs a second origin: generalize `--expect-binding` (free), a
`GET /attestation/<binding>` pull endpoint (no key), or a Gecko attestation key (a
founder/staff call). This matters more as autonomous signing spreads, not less.

## Not doing, and why

* **Rust for the purchase path.** Measured: 99.6% of prepare's wall clock is network I/O,
  Python is 14.5 ms of ~60,000 ms of window, and the crypto is already native (`solders`
  ships 36 MB of compiled Rust). Concurrency returned ~1.8 s — roughly 120× what a rewrite
  would have.
* **`prepare_payment_link`** (Solana Pay transaction request). It trades away "what was
  checked is what gets signed", which is the product.
* **`refresh_purchase`.** Re-running `prepare_purchase` is already free and correct; a
  second path that skips derivation is a second thing that can disagree with the first.
