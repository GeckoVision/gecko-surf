# Gecko comprehends the Jito Block Engine

**Jito lands your bundle; Gecko comprehends Jito's JSON-RPC so an agent shapes the call correctly the first time.**

The [Jito Block Engine](https://docs.jito.wtf) (by [Jito Labs](https://www.jito.wtf/))
is how Solana searchers, dApps, and bots submit **MEV bundles** (up to 5
transactions, executed sequentially and atomically) and **low-latency
transactions**. Its surface is a small JSON-RPC family — `sendBundle`,
`getBundleStatuses`, `getInflightBundleStatuses`, `getTipAccounts`,
`sendTransaction` — plus a REST tip-floor read. Powerful, but an agent can't
*use* it without knowing the exact method names, which share a wire path, the
positional `params` shapes, and where the tip accounts come from.

This example points Gecko at that surface and turns it into question-shaped,
first-call-correct agent tools.

## The honest part: this is the docs→draft-OpenAPI on-ramp

Jito ships **no OpenAPI**. Worse than a clean spec or an `llms.txt`, its API
reference lives entirely on a **JS-rendered docs page** — `docs.jito.wtf` is a
Sphinx/Read-the-Docs site whose request/response tables and `curl` examples only
appear after client-side rendering. A plain fetch of the landing page returns
marketing copy and nothing callable; the surface had to be read from the
**rendered** page (a headless browser), then cross-checked against source.

Sources for this spec:

- **The rendered docs page** [`docs.jito.wtf/lowlatencytxnsend/`](https://docs.jito.wtf/lowlatencytxnsend/)
  — method names, endpoint paths, request/response tables, `curl` examples, tip
  accounts, the min-tip rule, the optional-UUID auth note.
- **Official SDK source** (exact URL construction), cross-checked:
  [`jito-labs/jito-py-rpc`](https://github.com/jito-labs/jito-py-rpc/blob/master/jito_py_rpc/jito_jsonrpc_sdk.py)
  (`endpoint="/bundles"` for `sendBundle`/`getTipAccounts`, `"/getBundleStatuses"`,
  `"/getInflightBundleStatuses"`, `"/transactions"` for `sendTransaction`),
  plus [`jito-go-rpc`](https://github.com/jito-labs/jito-go-rpc),
  [`jito-js-rpc`](https://github.com/jito-labs/jito-js-rpc),
  [`jito-rust-rpc`](https://github.com/jito-labs/jito-rust-rpc), and
  [`mev-protos/json_rpc/http.md`](https://github.com/jito-labs/mev-protos).

We authored those into [`spec/jito_blockengine_openapi.json`](spec/jito_blockengine_openapi.json)
(OpenAPI 3.1). The **unmodified Gecko engine** comprehends that spec — no
Jito-specific code in `gecko/`. Nothing here is invented: every operation maps to
a real documented method/endpoint, and every parameter name and example is quoted
from the docs or SDK.

## The JSON-RPC ↔ OpenAPI mismatch (handled, not hidden)

The Block Engine is **JSON-RPC over HTTP POST, not REST**, and — unlike a clean
REST API — it is **not one endpoint per method**. `sendBundle` and
`getTipAccounts` both `POST /api/v1/bundles`; the method lives in the envelope:

```json
{ "jsonrpc": "2.0", "id": 1, "method": "sendBundle", "params": [[transactions], {"encoding": "base64"}] }
```

OpenAPI requires a unique `(path, method)` per operation, so — as in the
[`surfpool`](../surfpool) example — each JSON-RPC method is surfaced as **its own**
question-shaped operation:

- `operationId` = the exact JSON-RPC method (`sendBundle`),
- `path` = a virtual `/{method}` route (so the catalog has distinct operations),
- `requestBody` = that method's params **by name**, with genuinely-required params
  marked `required`,
- the true wire target is carried per-op on `x-jsonrpc-endpoint` (`/api/v1/bundles`)
  and `x-jsonrpc-method`, and
- because Jito's `params` is a **positional array**, the order to assemble it is
  carried on `x-jsonrpc-params` (e.g. `["transactions", "encoding"]`).

**Recorded vs live:** this is a comprehension / **recorded** demo — each method is
surfaced directly so the agent can *discover and shape* the call. A **live** caller
assembles the JSON-RPC envelope (named fields → positional `params` in
`x-jsonrpc-params` order) and POSTs to `x-jsonrpc-endpoint`. A thin JSON-RPC
transport adapter at the `Session`/caller seam is the natural next step to close
the live loop.

## What's comprehended

| Tag | Operations | Wire |
|---|---|---|
| `bundles` | `sendBundle`, `getTipAccounts` | `POST /api/v1/bundles` |
| `bundles` | `getBundleStatuses` | `POST /api/v1/getBundleStatuses` |
| `bundles` | `getInflightBundleStatuses` | `POST /api/v1/getInflightBundleStatuses` |
| `transactions` | `sendTransaction` | `POST /api/v1/transactions` (`?bundleOnly=`) |
| `tips` | `getTipFloor` | `GET https://bundles.jito.wtf/api/v1/bundles/tip_floor` (REST, **different host**) |

Base host: `https://mainnet.block-engine.jito.wtf` (regional hosts exist —
`amsterdam.`, `frankfurt.`, `ny.`, `tokyo.`, … — with identical paths; a testnet
host too).

**Auth:** default sends now need **no auth key** (docs: *"you no longer need an
approved auth key for default sends"*). An **optional UUID** unlocks higher rate
limits, sent as the `x-jito-auth` header or a `?uuid=` query param — modeled as
the optional `jitoUuidAuth` scheme so it stays **invisible to the agent** and no
tool is auth-gated.

## What's uncertain / not modeled (honest)

- **`params` are positional.** We model them by name for first-call-correct
  discovery and record the order in `x-jsonrpc-params`; the by-name→positional
  mapping is a live-adapter concern, asserted here only structurally.
- **`getTipFloor` lives on a different host** (`bundles.jito.wtf`) and is plain
  REST, not JSON-RPC — carried on `x-endpoint-url` and noted on the operation.
- **Not modeled:** the tip-amount **WebSocket** stream
  (`wss://bundles.jito.wtf/api/v1/bundles/tip_stream`), the **gRPC** Block Engine
  interface (bundles can also be submitted over gRPC per `mev-protos`), and
  **ShredStream** (`docs.jito.wtf/lowlatencytxnfeed/`) — those are separate
  transports/surfaces beyond a JSON-RPC-over-HTTP comprehension.
- Response envelopes carry documented **example** values so recorded mode returns
  something recognizable; they are illustrative, not a guarantee of live shape.

## Run it ($0, offline, no API key)

```bash
uv run pytest examples/jito/ -q
```

The tests assert the comprehension directly:

- `client.search("submit a bundle of transactions")` → **`sendBundle`**
- `client.search("get the tip accounts")` → **`getTipAccounts`**
- `client.search("status of in-flight bundles")` → **`getInflightBundleStatuses`**
- `client.search("send a single transaction fast")` → **`sendTransaction`**
- `client.search("recent tip amounts")` → **`getTipFloor`**
- a recorded `sendBundle` call → `200` + a well-formed JSON-RPC envelope with a bundle_id
- dropping the required `transactions` → **`gecko.caller.CallError`** (the empty
  bundle is *caught*, not fired at the engine)

```python
from gecko import AgentApiClient, public_session

client = AgentApiClient("examples/jito/spec/jito_blockengine_openapi.json",
                        session=public_session())  # default sends = no auth
hit = client.search("submit a bundle of transactions")[0]     # -> sendBundle
client.call(hit["name"],
            {"body": {"transactions": ["<base64-signed-tx>"], "encoding": "base64"}},
            mode="recorded")                                   # "live" once the JSON-RPC adapter lands
```

## What's real today vs. later

- **Live today:** the comprehension above — docs/source → a spec → first-call-correct
  agent tools an agent can search and shape, recorded and $0-falsifiable offline.
- **V2 / cloud (not claimed here):** continuous re-ingest as Jito's docs drift, a
  hosted MCP endpoint, and the JSON-RPC transport adapter that closes the live loop
  end-to-end against a running Block Engine host.

No metrics are claimed — this is a comprehension showcase, not a benchmark.

## Credit

The Block Engine, its docs, and the SDKs are built by [Jito Labs](https://www.jito.wtf/):
<https://docs.jito.wtf> and <https://github.com/jito-labs>. This example only
*reads* Jito's public surface to make it agent-usable; Gecko stays control-plane
only and stores no Jito response data.
