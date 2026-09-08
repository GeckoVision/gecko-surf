# Onboarding, the shelf, launch-your-own MCP, and the autonomous-agent loop

**Date:** 2026-09-07 · **Status:** plan, unbuilt, founder review · **Feeds:** the Dev3Pack cookbook (notebooks 12–19), `docs/front-door-spec.md`

**Boundary, stated first.** Nothing in this spec adds a key, a signature, or a broadcast to Gecko. Three invariants are already stated in code, and every proposal below composes with them rather than around them. (1) Gecko holds no key, signs nothing, broadcasts nothing (`gecko/signer.py`, module docstring, lines 1-11). The 1Claw integration is a seam: a verdict, a message binding, and unsigned bytes go out; a signature comes back. (2) A public mount may not hold a key for a network anybody's money is on, and may not amplify a partner's API (`gecko/providers/catalog_surface.py:18-32`; `GECKO_ORQUESTRA_CATALOG_PAGES` at `gecko/serve_mcp.py:284-294`, default 0, and the call-site reason at lines 400-406). Anything that provisions on behalf of a user is gated-mount work, the `store` mount pattern (`wallet_aware_store_surface`, `gecko/serve_mcp.py:240`): built only when it will be gated, absent otherwise. (3) Catalog yes, marketplace no. The founder ruled on 2026-08-27 that a public catalog is in and brokering stays out, and reaffirmed on 2026-09-04 that the edge is no take-rate and no custody, not the shape of the thing. The shelf in section (a) lists surfaces Gecko itself serves, so the line in `docs/context7-integration.md` ("A catalog, not a marketplace, we list only ourselves", line 196) stays true. The "not a marketplace" sentence in the checkout's `CLAUDE.md` (line 24) is pending the founder's rewrite. This spec does not edit it. Business rules (willingness-to-pay, provider-as-buyer) are out of scope here. The audience is about 200 bootcamp students and the waitlist developers.

## What already exists (don't rebuild)

| Piece | Where | State |
|---|---|---|
| Local add + Claude wiring | `gecko/onboard.py` `add()` L652, `configure_claude()` L292 (`claude mcp add`) | shipped |
| Hosted per-surface mounts at `/<name>/mcp` | `gecko/serve_mcp.py` `_SURFACES` L68, `_build_surfaces()` L296 | live |
| Discovery files from the same list | `gecko/wellknown.py`: server card L63, ARD catalog L167, host `llms.txt` L213, onboard breadcrumb L334 | live |
| Meta surface `/gecko/mcp` | `gecko/mcp_server.py`: `comprehend_api` L712, `list_surfaces` L808 | live |
| Root streamable-http endpoint declared | `server.json` line 23: `https://mcp.geckovision.tech/mcp` | declared, aggregator deferred (`docs/front-door-spec.md` L89-93, L120) |
| Unlisted-but-served surfaces | `gecko/serve_mcp.py` `UNLISTED_SURFACES` L166 (`reportavnzla`, `sosvenezuela`, `txline`) | live |
| Program shelf behind one surface | `gecko/providers/cli.py` `PROGRAMS` L150; `OrquestraCatalogSurface` | live |
| Add-a-program gate | `gecko/ingest_gate.py` `_REGISTRIES` L302-311 (R1-R6) | shipped |
| Submit-your-API doors | `gecko/comprehend_service.py` (`POST /comprehend`, `comprehend_api`) | live |
| Boot-time provider mounts, no rebuild | `gecko/provider_sync.py` | shipped |
| Effects before signing | `gecko/effects.py` `describe_effects()` L137 | shipped |
| Message binding | `gecko/txbind.py` (`evaluate_tx`) | shipped |
| Wallet binding | `gecko/wallet_binding.py`; `_resolve_buyer()` `gecko/prepare_purchase.py` L335 | shipped |
| Spend policy | `gecko/spend_policy.py` `SpendPolicy` L387, `TokenCap` L305, `AllowedInstruction` L269 | shipped |
| The autonomous run | `gecko/autonomous_purchase.py` `run_purchase()` L331 | shipped, no unattended run anywhere |
| Signers known to work | `gecko/prepare_purchase.py` `SIGNERS_KNOWN_TO_WORK` L931 (PayBox, Orquestra signer MCP at ~L1012, Phantom, Privy CLI) | shipped |
| Fork rehearsal | `gecko/sandbox/try_purchase.py` | shipped, local fork only |

## (a) Onboarding like Context7: one connector, the whole shelf

### What exists

A developer today does one of two things. With a spec of their own they run `npx @geckovision/gecko add <spec>`; `add()` comprehends it, caches the surface, seals any key, and calls `claude mcp add` for them. With a hosted surface they add one URL per surface, `mcp.geckovision.tech/<name>/mcp`. The discovery files (`server-card.json`, `ard.json`, host `llms.txt`, `onboard.md`) are generated from the same `_SURFACES` list, so nothing here disagrees with itself. The meta surface at `/gecko/mcp` already answers `list_surfaces` and `comprehend_api`.

### The gap

There is no single step that gives a code assistant, or Claude web, the whole shelf. Every hosted surface is a separate add. `server.json` already declares a root endpoint at `/mcp`, and `docs/front-door-spec.md` files the root aggregator with `search_capabilities` under "V2, deferred". With eight programs and five API surfaces on the shelf, per-surface adds are now the friction the front-door spec said would trigger it.

### Proposal: promote the root aggregator to the front door

One URL, `https://mcp.geckovision.tech/mcp`. It exposes `list_surfaces`, `search_capabilities` (intent in, surface + tool out), and forwards a call to the mounted surface by name. Read-only tools only. No auth. It reuses the `McpSurface` at `/gecko/mcp` and the lexical `gecko/catalog.py` path; hybrid retrieval stays unwired until it earns its place (see the paraphrase-recall record in `CLAUDE.md`).

| Client | The one step |
|---|---|
| Claude Code | `claude mcp add --transport http gecko https://mcp.geckovision.tech/mcp` |
| Cursor / VS Code | one `mcpServers` block: `{"gecko": {"type": "http", "url": "https://mcp.geckovision.tech/mcp"}}` |
| Claude web | Settings, Connectors, add custom connector with that URL. Read-only tools, no auth, no sign-in |
| BYO API | `npx @geckovision/gecko add <spec-url>`, unchanged |

### Drift to fix in the same pass

The front-door hero names `txline` (`docs/front-door-spec.md` L17, L33, L58; `docs/cursor-plugin-listing.md` L11, L36). `txline` is now in `UNLISTED_SURFACES`: served, advertised nowhere. Pick a listed, keyless, live hero. `jupiter` is mounted live and keyless (`gecko/serve_mcp.py` ~L359) and is the one a DeFi student recognises. Both docs change in this pass; nothing else does.

### The shelf today, honest inventory

Programs, from `PROGRAMS` (`gecko/providers/cli.py` L150), all behind `OrquestraCatalogSurface` at `/orquestra/mcp`: `jupiter`, `jurassic_fi`, `let_me_buy`, `metadao_ico`, `meteora`, `ore`, `pumpfun`, `whirlpool`. That surface's tools, in the order an agent reads them (`catalog_surface.py` L205-232): `start`, `find_start`, `list_programs`, `comprehend_program`, `prepare_purchase`, `try_purchase`, `list_stores`, `plan_payment`, `plan_swap`, `verify_signed_transaction`, `submit_transaction`, plus the account readers and derivers.

API surfaces, from `_build_surfaces()`: `jupiter` (live, keyless), `jito` and `jito-tips` (reads live, writes recorded), `pegana` (live, keyless), the `paysh` aggregate (`_build_paysh_surface` L442), `birdeye` (gated, `GATED_SURFACES` L154). `refugios` mounts only when its key is present.

The hosted `find_start` answers offline from packaged configs (`find_start_pages=0`). It does not page the partner's catalog for an anonymous caller. That stays.

### Growing the shelf

Adding a program is a fixed cost, and `gecko/ingest_gate.py` names it. A program is wired when it is in all six registries (L302-311): R1 listed in `provider.json`; R2 a packaged `<api_id>.json` with a program block; R3 every declared intent has a start card; R4 in `PROGRAMS`; R5 a `drift_watch` dispatch key; R6 at least one row in `gecko/providers/configs/orquestra/find_start_golden.jsonl`. No program is claimed on the shelf until its golden row passes.

| Backlog | Why students ask for it | Cost |
|---|---|---|
| Raydium | the AMM most tutorials start from | R1-R6, plus the fee-tier trap (fee tier is never derivable from mints) |
| Kamino | lending, the next verb after swap | R1-R6 |
| Drift | perps, the one with the most account plumbing | R1-R6 |
| Marginfi | lending, simpler account model than Kamino | R1-R6 |
| Sanctum | LST routing, pairs with `jupiter` | R1-R6 |

Carry the measured caveats from `CLAUDE.md`: 56 of 60 catalog programs build a graph, 6% of PDA accounts still need a human, and the independent witness for a wrong-but-well-formed address is hand-written per program. Each backlog row inherits those numbers until its own are measured.

## (b) Launch your own MCP and test it

### What exists

Two lanes, and both are real today.

**Local.** `gecko add <spec>` then `gecko serve`. Recorded mode by default, $0, every response synthesized from the schema. The same `/mcp` endpoint the hosted surfaces use. Auth headers never appear in a tool definition.

**Hosted.** A provider hands one URL to `POST /comprehend` or the `comprehend_api` tool (`gecko/comprehend_service.py`). The engine comprehends it and hands the result straight back; it stores nothing. `gecko/provider_sync.py` lets the hosted server mount surfaces a control plane names at boot, without an image rebuild.

The gecko-app side (`/home/nan/PycharmProjects/Gecko/gecko-app/docs/provider-platform-plan.md`, WS-2 "Comprehend and deploy", and `mcp-selector-plan.md`) is: scan, then comprehend and deploy at `mcp.geckovision.tech/<provider>/mcp`, then point the playground at it. State checked today: `lib/gecko/providers.ts` calls the engine's `POST /comprehend/servable` door and exposes `listBootSurfaces()` for the boot list; `app/api/providers/[id]/ingest`, `generate`, and `artifacts` routes exist; the code is on gecko-app `main`. What is not verified today: a stranger's spec going from the app to a live mount without a founder in the loop. Mark the hosted lane **built, end-to-end run pending**.

### What "test it" means

A launched MCP is not done when it answers `tools/list`. Three checks, in order:

1. **Scanner score.** `@geckovision/scan` against the mount. Ours scores 94; the doc-only surfaces score 26. A student's number goes in their notebook.
2. **Recorded-mode calls.** Every tool called once in recorded mode. First-call-correct means the prepared request matches the spec, not that a server said 200.
3. **Playground transcript.** One intent, in plain language, through the playground against their own mount. The transcript is the evidence. Keep it.

### Gap

The student-facing doc for the hosted lane does not exist. Notebook 14 (section d) is that doc. It does not need new engine code.

## (c) The autonomous-agent loop with 1Claw

### Vocabulary

There are zero hits for 1Claw in this repo's code today (the only mentions are in `docs/architecture.md` and one gap-map spec). Three things are called "binding" and they are not the same thing. Always write:

- **1Claw execution binding**: a partner API registered with 1Claw, called by name, credential never in the agent's process.
- **message binding** (ours): the sha256 over the exact message a signer is about to sign, `gecko/txbind.py`.
- **wallet binding** (ours): which wallet an authenticated account may spend from, `gecko/wallet_binding.py`.

### What 1Claw offers (paraphrased from their briefing note of 2026-09-05)

Full automation runs through their Platform API, every call authenticated with Gecko's platform key. The provisioning sequence: upsert a user; bootstrap a connection from a template (one template per strategy), which returns a `claim_url`, a `claim_token`, a `vault_id`, and agent ids; sign-in happens backwards, the user claims after the agent exists, and an expired claim is reissued; create a runtime; create a signing key per chain, held in an HSM, never exported; set a spend policy.

Execution bindings register the partner's API once with a credential source of the form `{type: vault_ref, vault_id, path}`. Agents call the binding by name. The credential never enters the agent's process or its context. Ten executor types: http, graphql, postgres, grpc, s3, smtp, mysql, redis, webhook, soap. An agent cannot create its own binding; creation from an agent is refused and routed to the operator.

The Intents API signs with the HSM key and enforces guardrails before signing: `tx_to_allowlist`, `tx_max_value`, `tx_daily_limit`, `tx_allowed_chains`. It has a simulate mode. Their advice, and it matches ours: re-validate an agent-created agent's authority at execution time, not only at creation.

### The map

| 1Claw step | Gecko side | Why this shape |
|---|---|---|
| upsert user, bootstrap connection, claim | Bind on CREATE, never on ASSERT. `_resolve_buyer()` (`gecko/prepare_purchase.py` L335-385) refuses a supplied `buyer` that is not the wallet bound to the account; the binding is written when the wallet is created, never from a claim. `docs/specs/2026-08-13-wallet-enrolment.md`, "The ruling" | a wallet id is not a secret; accepting an asserted one is the custody bypass |
| signing key per chain, HSM | `TransactionSigner.sign()` (`gecko/signer.py`) takes a `SignerHandoff`, never bytes alone. The 1Claw client is one more backend behind that seam. Gecko never sees the key | the seam is the product boundary |
| spend policy | The enclave twin. The same caps exist in `SpendPolicy` (`gecko/spend_policy.py` L387): per-transaction, hourly and daily velocity, allowed program + instruction, allowed destinations, per-mint caps via `TokenCap`. Both predicates must pass. Neither is inferred from the other. `docs/specs/2026-08-12-autonomous-signing-blockers.md`, B3 and B4 | our ledger is advisory and writable by the process it bounds (B4); theirs is not resettable by us; B3 needs a caller-supplied idempotency key |
| execution binding registration | The comprehended surface is what they ask for ("send the shape of the partner's API: auth, endpoints"). One comprehension pass yields two artifacts: the MCP surface for agents and the binding map for 1Claw. Gecko's backend is the operator that registers bindings. Agents only run them | no second description of the API to drift |
| the run | `run_purchase()` (`gecko/autonomous_purchase.py` L331). Policy is fixed before the call; `agent_supplied_policy` is pinned to `None` inside the signer (docstring L36-37) | the gate reserves budget once; a second consultation halves every rolling cap |
| sign | The message binding travels with the sign request. 1Claw must sign the raw bytes unmodified and echo the binding. This is the Orquestra-signer pattern already in `SIGNERS_KNOWN_TO_WORK` (~L1012): `binding` + `binding_strength: "exact"` in, the same binding back, then `verify_signed_transaction` before `submit_transaction` | a signer that ignores the binding silently is recorded there too (npm 0.1.3); a 1Claw entry is added only after the same verification |
| re-validate at execution | `verify_signed_transaction` re-checks the binding and the blockhash window on the signed bytes; the spend gate re-runs on the run, not the enrolment | authority at creation is not authority now |

### Acceptance, before any live call

1. A test in which an agent-created execution binding is refused. The refusal comes from our client, before the request leaves, and again from 1Claw if the client is bypassed.
2. A test in which a stale authority is refused at execution: an agent whose claim expired, or whose spend policy was revoked after creation, cannot sign.
3. A `SIGNERS_KNOWN_TO_WORK` entry for 1Claw, added only after a measured signing round trip on a fork shows the raw bytes came back unmodified with the binding echoed.

All three are offline or fork-only. None of them touches a network anybody's money is on.

### Lanes for students

| Lane | Where | Who holds what |
|---|---|---|
| stub client | offline, in CI | nobody; the fake echoes bytes and refuses on cue |
| own account | devnet, or a surfpool fork | the student's own 1Claw account; the key is in their HSM slot, never on their machine |
| mainnet | under spend policy, founder go-ahead only | not a cookbook lane; no notebook can spend mainnet funds |

### Open questions for the founder

1. 1Claw pricing. Is there a free tier that holds for about 200 students at once?
2. Solana first? Assumed yes. The Intents API guardrails are chain-generic, so the second chain is a config change on their side.
3. Which partner platform is the first execution-binding target? Pegana is the warm candidate; it is already a listed keyless surface.
4. Claim-link delivery: in-app, or by email? This decides whether the bootcamp flow needs an inbox.

## (d) Cookbook outline: notebooks 12-19, not built

Same three rules as the bootcamp `cookbook/README.md`: offline by default, nothing can spend, honesty. Numbering continues from 11.

| # | Notebook | What it teaches | Runs |
|---|---|---|---|
| 12 | Add Gecko in one step | the one-URL front door in Claude Code, Cursor, Claude web; `list_surfaces` | manual (edits config) |
| 13 | Browse the DeFi shelf | `find_start` and `list_programs` over the eight programs; what a golden row is | offline fixtures; `GECKO_LIVE=1` read-only |
| 14 | Launch your own MCP | `gecko add`, `gecko serve`, the scanner score, the playground transcript | manual |
| 15 | Plan a swap, read the effects | `plan_swap`, then `describe_effects` (`gecko/effects.py` L137), then unsigned bytes; what "effects before anybody signs" means | offline |
| 16 | The seatbelt: spend policy | `SpendPolicy`, a refusal per cap, why the ledger is advisory | offline |
| 17 | Rehearse on a fork | `try_purchase` on a local surfpool; the receipt of what moved | manual |
| 18 | Provision an agent with 1Claw | the stub client, the refusal test, the binding echo | offline; `ONECLAW_LIVE=1` devnet |
| 19 | Run a strategy end to end | `run_purchase` with a fork signer, then the same on devnet | opt-in devnet |

Notebooks 12, 14, and 17 are lab guides in the `manual-run` sense the README already defines. Notebook 18 ships with the stub client and the two refusing tests from section (c) as its cells. Notebook 19 is the only one that touches a public network, and only devnet.

## Also in this pass (doc-only)

- `docs/front-door-spec.md`: replace the `txline` hero with `jupiter`; move the root aggregator from "V2, deferred" to "the front door".
- `docs/cursor-plugin-listing.md`: the same hero swap.
- No engine code. No test. No `CLAUDE.md` edit.

## What is still open

- The root aggregator itself: `search_capabilities` over all mounts, and call forwarding. Engine work, one PR, after founder review of this spec.
- The hosted launch lane, end to end, without a founder in the loop. gecko-app WS-2 is merged; the live run is not recorded.
- Every backlog program in (a). Nothing is claimed until R1-R6 pass and the golden row is green.
- The 1Claw client and its two refusing tests. Nothing is written yet.
- The four founder questions in (c).
- The eight notebooks in (d).
