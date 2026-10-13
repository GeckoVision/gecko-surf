# Gecko any-query

Ask once, in chat. Gecko plans the answer from a catalog of paid data APIs, quotes it,
takes one payment in USDC on Solana, calls each source and returns every value with the
source it came from and its status.

## What it does

1. **Plan** (`plan_query`). You ask a question about a list of subjects (domains,
   tickers, emails). Gecko returns the fields it can fill, the ranked sources for each
   field, the fields it cannot plan and why, and a quote for the whole run. Planning
   spends nothing, and the quote expires.
2. **Pay and run** (`run_query`). You confirm the quote. Your wallet pays it once, in
   USDC (or SOL converted at the pool price) on Solana. The charge must land before any
   paid call runs. If it does not land, nothing runs.
3. **Read the values** (`run_rows`, `run_changes`). Each row has a subject, a field, a
   value, the source that answered and a status: `fresh`, `repaired` (the first source
   failed and a ranked fallback answered), `stale` (the last good value is kept) or
   `no data`. Contact details such as email addresses are masked by default.
4. **Keep it** (`save_bundle`, `create_dataset`, `refresh_dataset`). Save a plan as a
   bundle and run it again on new subjects without new planning. Or turn a run into a
   dataset, refresh it, and give readers their own read endpoint (`/d/<dataset_id>/mcp`,
   tools `schema`, `rows`, `changes_since`, `sources`) with a separate reader key.
5. **Check the money** (`payments`). Every charge, settlement, rebate credit and discount
   is in the payments ledger.

The code lives in `gecko/anyquery/` (planner, runner, billing, datasets),
`gecko/servicemap/` (the source registry the planner reads) and `gecko/orthogonal/`
(the upstream catalog client). `docs/module-index.md` lists every module.

## Run it locally ($0)

The recorded demo runs the real planner, runner, Solana billing and tools. Only the
edge is recorded: no network, no model, no chain, no wallet provider, no database.

```bash
uv sync --extra serve --extra datasets
uv run python scripts/any_query_demo.py --mode recorded
```

It onboards a demo account, plans a query over three companies, pays for two runs,
repairs a broken source on the second run and prints what changed. Add `--json` for
the raw tool answers. All values, wallets and signatures in the fixtures are invented.

The test suite covers the same code paths offline:

```bash
uv run pytest tests/test_anyquery_*.py tests/test_servicemap_*.py tests/test_orthogonal_*.py
```

## Connect to the hosted endpoint

The hosted any-query MCP endpoint is:

```
https://mcp.geckovision.tech/anyquery/mcp
```

It needs a Gecko key, sent as a bearer token. Keys are issued on request. Keep the key
in your environment, never in a file you commit:

```bash
export GECKO_KEY=...   # issued on request
claude mcp add --transport http gecko-anyquery https://mcp.geckovision.tech/anyquery/mcp \
  --header "Authorization: Bearer $GECKO_KEY"
```

Any MCP client that sends custom headers works the same way. Then ask your question in
chat; the agent calls `plan_query` first and shows you the quote before anything is paid.
