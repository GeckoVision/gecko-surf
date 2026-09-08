# How we use MongoDB now

Measured 2026-09-08 against the live cluster (server 9.0.0). No content read, field names
and counts only. Total **505.2 MB across 10 databases**.

## The picture

| DB | Size | What it is | Verdict |
|---|---|---|---|
| `gecko_rag` | **411.6 MB** | chunks, embedding cache, judge corpus | 81% of everything |
| `gorilla` | 82.7 MB | 12.3M football odds, a different product | not Gecko |
| `gecko_cache` | 7.0 MB | bot behaviours, trading decisions, judge transcripts | not Gecko |
| `gecko_events` | 3.6 MB | telemetry: `events` + `surf_events` | product |
| `__mdb_internal_search` | 0.2 MB | Atlas search internals | not ours to touch |
| `gecko` | 0.1 MB | 26 objects, kamino positions | dead |
| `gecko_registry` | 0.0 MB | 16 API keys | product |
| `gecko_firewall_dev` | 0.0 MB | 51 verdicts, `_dev` on a prod cluster | dead |
| `gecko_trade_agent` | 0.0 MB | 2 docs across 6 collections | dead |
| `geckovision` | 0.0 MB | one empty collection | dead |

**The product is `gecko_registry` plus `gecko_events`: 3.6 MB, 0.7% of the cluster.**

## Invariant #1: clean, and worth stating

Nothing found stores an API response payload, a secret, or third-party user data.

- `gecko_keys` keys documents by a 64-char `_id` with no plaintext key field. Correct design.
- `gecko_rag.chunks` holds `text` and `source_url` from ingested public documentation, which
  is the API *surface* and exactly what we are supposed to store.
- `surf_events` carries `ts, event, surface_id, k`. No wallet, no identity.

One thing to watch: `gecko_events.events` has a `wallet_ts_desc` index, so it stores wallet
addresses. It is our own first-party telemetry with a 180-day TTL, which is defensible, but
it is the only identifiable data here and should stay deliberate.

## The five hygiene problems, ranked

**1. `surf_events` has 80,484 documents, zero indexes beyond `_id`, and no TTL.** In the same
database, `events` has 2,293 documents and four indexes including `ts_ttl_180d`. The small
collection is well tended and the big one grows forever with every query a full scan. This is
backwards and it is the one that will hurt first.

**2. `gecko_rag` stores the same text twice.** `chunks` (24,208 docs, 271.6 MB) carries
`text` plus a 1024-dim `embedding`. `chunk_embedding_cache` (12,257 docs, 138.7 MB) carries
`text` again plus a **1536-dim** embedding from a different model. Two dimensions means two
models, so one of them is stale, and neither the code nor the data says which.

**3. Eight databases are named `gecko`-something.** `gecko`, `gecko_cache`, `gecko_events`,
`gecko_firewall_dev`, `gecko_rag`, `gecko_registry`, `gecko_trade_agent`, `geckovision`.
Nobody can tell from the name which one the product depends on.

**4. Four databases are dead.** `geckovision` is empty. `gecko_trade_agent` holds 2 documents
across 6 collections. `gecko_firewall_dev` has `_dev` in its name on a production cluster.
`gecko` holds 26 objects of a Kamino experiment.

**5. Loaded collections with no index at all:** `bot_behaviors` (11,856), `simulations` (687),
`decisions` (514 docs / 5.0 MB), `gecko_keys` (16, but every `account_id` lookup scans).

## The uncomfortable read

81% of our storage serves retrieval, and per `gecko/search.py` the hybrid retrieval path is
built and **not wired into `mcp_server.py`**. The two databases the shipped product actually
needs total 3.6 MB. Our storage says we are a RAG company; our thesis says we comprehend
APIs. Worth resolving before we grow either.
