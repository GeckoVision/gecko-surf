# From "offline-proven" to "a blind agent lands a gasless espresso"

Written 2026-09-16, after PR #548. Every "done" below is measured in this repo; every
"open" names the artifact that closes it.

## 1. Pending, in order

| # | item | state | closes when |
|---|---|---|---|
| 1 | PR #547 (spend gate keyed on the authority) | open, mergeable | merged, branch deleted |
| 2 | PR #548 (relay-paid purchase, offline-proven) | open, stacked on #547 | merged after #547; **delete #547's branch on merge** or #548 lands nowhere |
| 3 | Fork rehearsal steps 7-9 with `scripts/gasless_purchase.py` | never run with this code | `buyer SOL after == before == 0`, signature in the runbook |
| 4 | The **convert leg** relay-paid (USDG->USDC on Orca) | blocked: `scripts/prepare_whirlpool_swap.py:331` welds payer to signer and `:462` refuses payer != keypair | swap prepared with `fee_payer=relay`, then settled through `settle_sponsored`; the same authority-role path, one more allowlisted program |
| 5 | Orquestra builder emits `num_required_signatures=2` with a 1-slot signature array when `feePayer != signer` (memory, 2026-09-08) | worked around by rebuild in the fork proof; **unverified for `prepare_purchase`** | a test on the tool's bytes: `signature_slots(tx) == (relay, buyer)` against a real build |
| 6 | Re-tag v0.11.0, redeploy hosted MCP | founder-side | npm and `mcp.geckovision.tech` report 0.11.0 |
| 7 | `plan_payment` latency (20.5s -> ~5s: IDL cache, peg once, in-process legs) | offered, not started | measured before/after in the PR |
| 8 | `geckovision.tech/llms.txt` in Maxxwell's shape | awaiting the one-line choice | published |
| 9 | Phases 3-5 of the plan (catalog callable rate; CPMM adapter + Raydium; selector demo) | not started | per plan file |

## 2. What is missing to integrate with Kora on mainnet

The code path exists (PR #548). What does not exist is the **operated relay**.

| need | today | what closes it |
|---|---|---|
| A running Kora node we control | fork only, throwaway key, killed after each run | a long-lived process: `kora --config examples/kora_demo/kora.gecko.toml --rpc-url $HELIUS rpc start --signers-config signers.<backend>.toml` |
| The relay key held **outside** our machine | fork: `memory` signer from env | Kora's `privy` (or `turnkey`) signer type. We already run Privy (`scripts/privy_backend.py`, tx #12 proved enclave signing). The relay key then never touches a disk we own, which is the same line CLAUDE.md draws for gecko/ |
| Funding and its alert | none | fund with what we can afford to lose (the balance IS the budget); scrape `[metrics.fee_payer_balance]`, alert under N SOL |
| Auth on the wire | `KORA_API_KEY` proven 401/405 on the fork | re-run the six probes in runbook step 6 **against the live node** before the first mainnet transaction |
| Pricing and usage | Mock price source, in-memory usage store (validator warns) | decide: free-for-demo with `[kora.usage_limit]` per wallet, or price in USDG. Free is honest for the demo and bounded by the balance |
| Fee-payer parsers | none for whirlpool / let_me_buy (Kora validator says so) | nothing to build; Lighthouse is the backstop and #548 enforces it stays appended |
| Where it runs | nowhere | a `gecko-relay` service next to `surfcall-be` (same host as `mcp.geckovision.tech`), api-key gated, reachable only from our settle path, never on a public MCP mount (`kora_surface.py` keeps `signTransaction` recorded for exactly this reason) |
| Mainnet USDG in the buyer wallet | nobody holds USDG (read 2026-09-08) | founder money call: swap USDC->USDG once, or fund `4jccRjip…` directly |

Deliverables: `examples/kora_demo/docker-compose.yml` (kora + config + signer file mounted, no key in the image), `examples/kora_demo/signers.privy.toml.example`, and `docs/runbooks/mainnet-relay.md` (fund, probe, first transaction, alert).

## 3. "Our own relay"

Two readings, and the answer is the same for both.

- **Run our own node**: yes, that is section 2. Kora is the Foundation's relay, it ships Lighthouse, and it has a config validator that already scored our hardening (0 errors vs the shipped 1/35).
- **Write our own relay inside gecko/**: no. A relay is a funded key on a box, which is the one thing CLAUDE.md forbids `gecko/` from being. What we own is everything **above** the relay: `accept_relay_signature` (the relay may append, never alter), the spend gate keyed on the buyer, the authority role, the cosign merge. That is the part no other relay user has, and it works over any fee payer that signs `signTransaction`-style, not only Kora. If Kora ever disappears, the seam (`FeePayerRelay`, two members) is what a replacement implements.

## 4. The dynamic graph: an e2e run that draws itself

"Dynamic" means generated from a **real execution**, not drawn by hand. Design:

1. `gecko/trace.py`: a `Trace` collector that `run_purchase`, `settle_sponsored`, `prepare_purchase_result` and `sponsor` write into through an injected `trace=` callable. Control plane only: step name, party (`gecko` / `relay` / `buyer` / `node`), outcome (`ok` / refusal code), CU, duration, the binding prefix. **Never** bytes, keys, addresses beyond the first 8 characters, or node payloads. JSONL, one row per step.
2. `scripts/trace_to_graph.py`: trace JSONL -> archify `workflow` spec -> HTML and PNG (memory rule: archify, never Mermaid, 9 checks). A refusal renders as the failure node carrying its code; a landed run ends at the signature. Every run produces its own graph, so a fork run and a mainnet run are two files that should look identical except for the network label. That comparison is the demo.
3. `scripts/gasless_purchase.py --trace out.jsonl` and the chain runner get the flag.

Done when: one command runs the fork purchase and writes `graph.html` whose nodes are the steps that actually executed, with the refusal code visible when one fires.

## 5. Activity tests with a blind agent

The blind-eval system exists (`evals/blind/missions/`, `blind-tester` agent, `scripts/blind_eval_score.py`, baseline 8/10). Add two missions.

**`gasless-espresso-discovery`** (hosted, keyless, curl only, today's rules):
operator instruction: *"Buy an espresso at geckocoffee on Solana paying no SOL. The buyer holds only USDG. Start at https://geckovision.tech."*
Checks: found the `fee_payer` argument and the `gasless` block from the docs before any clock; knew `signatures_required` names two parties; knew the relay's answer will carry one extra instruction and which program; knew never to call `signAndSendTransaction`; stopped at the signer step with no key; no retries.

**`gasless-espresso-landing`** (fork, a throwaway buyer key handed to the tester, relay running):
same instruction plus a keypair path and `KORA_RPC_URL`/`KORA_API_KEY`.
Checks: relay asked exactly once; transaction sent exactly once; two signatures on the sent bytes; `buyer SOL after == before`; charged CU == predicted CU; the trace graph from section 4 attached as the evidence.

Scored with the existing scorer into the 8/10 checklist; the failures are the work.

## Sequence

1. Merge #547, #548 (delete the base branch).
2. Fork rehearsal 7-9 with the runner. First real evidence that the code path lands.
3. Convert leg relay-paid (item 4) and the builder-shape test (item 5). Now the whole USDG->espresso chain is gasless on the fork.
4. Trace + graph (section 4). The fork run draws itself.
5. Relay as a service on the fork (docker-compose), then the two blind missions on the fork.
6. Mainnet relay (section 2, founder-run: funding, first transaction). Then the discovery mission against the hosted surface, and one landing on mainnet.
