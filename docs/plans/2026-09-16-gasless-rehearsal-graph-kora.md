# Implementation plan: gasless rehearsal on the fork, the graph it draws, and the Kora PR

2026-09-16. Three pieces, in build order. Each has a done-when that is measured, not claimed.

## 0. Correction first: option B is already refuted

I proposed measuring the USDC leg from the buyer's token account snapshot. The repo
already measured that on 2026-08-30 (`gecko/simulate.py`, `parse_token_deltas` docstring):
on surfpool 1.1.1 through 1.5.0 a successful simulation returns `postTokenBalances: null`
**and** `accounts: [null]`. The fork offers no channel. The spend gate cannot be exercised
on a fork; it is exercised on mainnet. The fork's one measurement is
`gecko.sandbox.rehearse`: land the transaction, then read what moved.

So the plan is not "make the gate pass on the fork". It is: give the sandbox lane a relay,
land the gasless espresso there, judge by what moved (buyer SOL 0 before, 0 after), and
keep the gate for mainnet, where the arrays exist. Nothing is bypassed; the repo's own
fork lane is extended.

## 1. The relay-paid rehearsal (`gecko/sandbox/rehearse.py`)

Extend `rehearse_purchase` with an optional `relay: FeePayerRelay`. The seven steps:

| step | self-paid (today) | relay-paid (new) |
|---|---|---|
| FUND | price in tokens + SOL for the fee | price in tokens only. Buyer SOL stays 0 by construction |
| PREPARE | `prepare_purchase_result`, the production path | same, with `fee_payer=relay.pubkey` |
| SPONSOR | none | `gecko.relay.sponsor`: relay signs, answer accepted by name (may append Lighthouse, may not alter) |
| SIGN | ephemeral buyer signs slot 0 | ephemeral buyer signs **its** slot over the relay's extended bytes (`partial_sign`) |
| LAND | send at the proven endpoint | `cosign.merge_signatures` over the relay's message, then send |
| JUDGE | buyer token -price, store +price, fee, receipt row | same, plus **buyer SOL delta == 0** and relay SOL delta == -(fee), reported as a `LamportDelta` |
| RESET | `surfnet_resetAccount` on touched accounts | same, plus the relay's account |

Fork bindings stay: signer proven against this fork, `submit.rpc_url` is the proof's, and
now a third: the relay's URL is the operator's, checked against a loopback fork by the
runner, never inferred.

Tests, offline (`tests/test_rehearse_relay.py`): fake surfnet RPC, fake relay in Kora's
shape (from `tests/test_relay.py::kora_extend`), ephemeral buyer. Assert: the relay was
asked once with the original bytes; the sent bytes carry two valid signatures over the
extended message; `buyer_sol.moved == 0`; a relay that alters an instruction is refused
before the buyer signs; a fee payer that is not the relay is refused before anything.

Runner: `scripts/gasless_purchase.py --network fork` switches to the rehearsal lane;
`--network mainnet` keeps `settle_sponsored` with the gate. One script, two lanes, stated
in its output.

Done when: the fork run prints `LANDED`, `buyer SOL 0 -> 0`, `charged CU == predicted CU`,
and the signature is recorded in the runbook.

## 2. The dynamic graph (`gecko/trace.py`, `scripts/trace_to_graph.py`)

A graph generated from a real run, never drawn by hand.

- `gecko/trace.py`: a `Trace` sink. `rehearse_purchase`, `settle_sponsored`, `run_purchase`
  and `sponsor` write one row per step through an injected `trace=` callable. Row fields:
  `step`, `party` (gecko / relay / buyer / node), `outcome` (`ok` or the refusal code),
  `units`, `ms`, `binding_prefix`. Never bytes, never a key, never an address beyond its
  first 8 characters, never a node payload. JSONL.
- `scripts/trace_to_graph.py`: JSONL -> archify `workflow` spec -> `graph.html` + PNG.
  A refusal is the failure node carrying its code. A landed run ends at the signature.
- The runner gets `--trace path.jsonl`. A fork run and a mainnet run produce two graphs
  that must differ only in the network label. That comparison is the demo.

Tests: a trace from the offline rehearsal test renders to a spec that passes
`archify validate workflow --quality showcase` with 0 errors.

Done when: one command runs the fork rehearsal and writes `graph.html` whose nodes are
the steps that executed.

## 3. The Kora upstream PR

**What we found that Kora does not tell a client.** With Lighthouse enabled,
`signTransaction` returns a transaction whose message is not the one sent: an assertion
instruction is appended before signing. The response carries no signal of that. A client
that co-signs, or that binds a receipt to the message it sent, has to diff the bytes to
learn it. We wrote that diff (`gecko/relay.py`); every other client will write it too, or
sign the wrong message.

**The change, one problem, small:** `SignTransactionResponse` gains
`lighthouse_assertion_added: bool`. True when the assertion was appended; false when
Lighthouse is disabled or when the append was skipped for size
(`fail_if_transaction_size_overflow = false` logs a warning and skips today, so config
alone does not tell the client). Rust: `add_fee_payer_assertion` and
`append_lighthouse_assertion` return `Result<bool>`; `VersionedTransactionResolved::
sign_transaction` returns the flag beside the transaction; the RPC method copies it into
the response; TS SDK type updated. No policy, fee, or signer code changes, so the trust
boundary is untouched, and the PR says so in the words CONTRIBUTING asks for.

**Tests:** in `sign_transaction.rs`: enabled -> true and the returned message has one
more instruction than the request; disabled -> false and the message is byte-identical;
skipped-on-overflow -> false. Each test has an allowed case and a refused case, which is
what CONTRIBUTING demands of a test.

**Process, from `CONTRIBUTING.md`:**
1. Fork `solana-foundation/kora` under `GeckoVision` (`gh repo fork`), branch
   `feat/sign-transaction-reports-lighthouse` from `main`.
2. `just check`, `just unit-test`; the change touches the RPC surface, so
   `just integration-test` too (starts a local validator).
3. Commits **signed** (the repo refuses unsigned commits) and **without AI attribution
   trailers** (CI labels them `ai-unreviewed` and fails). So the founder commits and pushes;
   the code and the text are prepared here. AI use is declared in the template's
   disclosure section, honestly.
4. PR title in Conventional Commits: `feat(rpc): report whether signTransaction appended
   a Lighthouse assertion`. Body in plain English, three short sections: the problem (what
   a client cannot know today), the change (one field), the tests (three cases). Nothing
   about Gecko beyond one line naming where it was found.

A second, docs-only candidate if maintainers prefer no API change: the `kora-client`
skill guide gains two sentences: co-signers sign after Kora when Lighthouse is on;
`user_id` is required under free pricing with usage tracking.

## Sequence

1. §1 rehearsal lane + runner + fork run (today).
2. §2 trace + graph (next).
3. §3 Kora fork and PR text prepared in parallel; the founder signs and opens it.
