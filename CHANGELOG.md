# Changelog

## Unreleased

### Security
- **Untrusted spec prose could mint a value domain that read as a declared `format:`.**
  `graph._domain_signal` scanned a field's free-text `description` for
  `"base58"` / `"pubkey"` / `"solana address"` and wrote `base58` into the signature's
  **format** slot — the same slot a spec-declared `format:` occupies. Two failures, both
  reachable from an ingested spec with no schema access at all:

  1. **Basis laundering.** `correlate` reported the shared domain as `format-eq`, so a
     guess derived from prose was indistinguishable from a fact the spec stated. That is
     INFERRED reading as EXTRACTED one level below the ladder itself.
  2. **Genericity escape.** `_demote_generic` quarantines **tier 1** only. A name common
     enough to be genericity-demoted is correctly non-plan-eligible — until the domain
     signal lifts the link to tier 2, where the demotion never runs. Measured: a
     `sessionId` shared by 8 operations goes from **0 to 64 plan-eligible links on
     description text alone**, and identically from a planted `example`.

  The prose channel is removed: a curated keyword scan over untrusted free text is the
  same best-effort class as the description injection scanner, and a best-effort text
  signal must not write a slot that reads as a structural claim. It also misfired badly
  on real input — across the committed fixtures **all 23 derivations (17 distinct graph
  nodes) came from prose and none from an example**, including `circulating_supply` (a
  number) and `pubkeys_b64` (a base64 blob) classified as Solana addresses,
  `transaction_signature` on *Ethereum* endpoints, and `transaction_hash` landing in the
  same domain as `input_token`.

  Restricting the channel is not sufficient on its own — `example` is equally
  attacker-controlled (`sanitize` deliberately does not address-scan hint channels), so
  it reproduces the escalation exactly. Two further controls close it: a domain derived
  from the surviving example-shape channel is **marked** (`base58~`, reported as
  `format-eq~derived`) and can never corroborate a declared `base58`; and a derived
  domain **cannot raise a link's tier**, so the genericity demotion always applies. A
  spec-declared `format:`/`pattern:` still lifts a name match to tier 2 — asserted.

  No provenance value was added; both ladders in `gecko/provenance.py` are unchanged.

  Blast radius, measured before/after on the committed fixtures (privy 159 ops, pegana,
  pegana_p0, txline): derived-domain nodes **17 → 0**; graph nodes, edges and `feeds`
  edges **unchanged on every fixture** (privy 8171/11800/3704, pegana 292/352/90,
  pegana_p0 360/432/104, txline 997/1134/147); `correlate` summaries **unchanged on every
  fixture** (privy 2439/19, pegana 12/9 — the pinned R8 counts do not move). The
  adversarial spec goes from 64 plan-eligible links to 0, by either channel. The one real
  loss is
  reporting: Pegana declares no `example` for `mint`, so `metrics.domains` no longer
  lists `base58` for that surface. `find_start` retrieval eval unchanged (recall@1 0.74,
  recall@3 0.89, MRR 0.81, 4/4 out-of-scope rejections, 0 false accepts).
- **An account nothing knew about was reported as a fact the surface stated.**
  `find_start._account_step` ended in
  `return DeriveStep(account=name, provenance="extracted")` for any account it held no
  knowledge of. `extracted` means "straight off the surface" — an affirmative claim — so
  absence of knowledge and a real IDL-declared account produced the same answer, and a
  typo in a hand-authored `StartSpec.accounts` shipped as a confident spec-stated step:

  ```python
  _account_step("totally_made_up_account", None, recovered={}, overlay_pdas=frozenset(),
                overlay_why={})
  # -> DeriveStep(account='totally_made_up_account', provenance='extracted', note='')
  ```

  `extracted` is now EARNED. It requires positive evidence in one of exactly two forms:
  the packaged program config holds a PDA node for the name (the IDL states the recipe),
  or the intent's `StartSpec` lists it in the new `surface_named` field — an affirmative,
  reviewed claim that the program artifact names this plain non-PDA slot (a token program
  id, the signer, a mint, the program id itself). Anything else is **FLAGGED**, with a
  note saying so, and is never dropped from the plan.

  No provenance value was added; both ladders in `gecko/provenance.py` are unchanged.

  Measured before/after across all 11 wired start cards: the histogram is **identical**
  (`extracted` 35, `recovered` 42, `flagged` 9). Nothing wired today was actually unknown
  — 27 of the 35 `extracted` accounts carry a config PDA node and the other 8 are
  Jupiter's declared route slots, now stated explicitly. The defect was the DEFAULT, and
  the default is what changed; the visible difference appears the first time a phantom
  name is authored, which is now a test. `find_start` retrieval eval unchanged (recall@1
  0.74, recall@3 0.89, MRR 0.81, 4/4 out-of-scope rejections, 0 false accepts).

  On W3c (`cross_surface` unreachable from `find_start`): measured, the two code paths
  already **agree**. `jupiter_landing._label_provenance` labels the 9
  `DECLARED_ROUTE_ACCOUNTS` `extracted` (bar the event authority, `recovered`) — the same
  answer `find_start` gives, now asserted by a test that reads the landing orchestrator's
  own list. The 16 `cross_surface` accounts are the AMM route legs, which are absent from
  the derive plan because they do not exist until the aggregator's HTTP quote answers.
  Declaring them on a `StartSpec` would fabricate accounts at plan time, so `StartSpec`
  deliberately gained no `cross_surface` field.

### Added
- **`gecko/arazzo.py` — the derived plan as a portable handoff artifact.** `to_arazzo()`
  is a **pure serializer** (no I/O, no network, no store, no execution) that turns a
  `compose.Plan` or a safety-gated `safechain.SafeChainResult` into an Arazzo 1.0
  document any runtime can execute. Approved 2026-07-29
  (`docs/specs/2026-07-29-arazzo-spdg-orchestration-plan.md`): *export now, ingest later,
  never the executor.* Until now the DAG we derive was a Python object only our own CLI
  and MCP could read. Thin transport: `gecko export-arazzo` + `McpSurface.export_arazzo`.

  Three properties make it ours rather than anyone's Arazzo:

  1. **A refused hop is never a callable step.** Arazzo's Step Object schema requires
     exactly one of `operationId`/`operationPath`/`workflowId` — every member of `steps[]`
     is by construction a *call*, so the format has **no shape for "deliberately not
     runnable"**. Rather than force one, a chain carrying a Skill-Guard-quarantined hop
     emits **no workflow at all**: the hops move to the root `x-gecko-refusals` /
     `x-gecko-withheld` extensions and `workflows` stays empty, deliberately violating the
     spec's `minItems: 1`. A refused export therefore does not load in a conformant
     runtime — fail-closed, and the *whole* chain is withheld, not just the poisoned hop,
     so a surviving clean prefix can never read as a partial plan we endorsed.
  2. **Per-edge provenance travels with the edge.** Class (DECLARED/INFERRED — never
     EXTRACTED for a `feeds` edge), basis, confidence, supplier step/field, and — on a
     cross-surface join — the `gate: customer-confirmed` that `compose.cross_plan`
     enforced, carried as `x-gecko-provenance` on the Parameter Object (or on
     `requestBody` for a body-carried join). `cross_plan`'s refusal of an unconfirmed
     join survives too, as a non-executable document.
  3. **No values, ever.** Every wire-bound leaf is an Arazzo *runtime expression*
     (`$inputs.<name>`, `$steps.<id>.outputs.<field>`, `$response.body#/<field>`) — name
     references the runtime resolves against data it holds. A literal would put Gecko on
     the data plane and trade away unilateral ingest.

  Conformance is checked against the **vendored official OAI schema**
  (`tests/fixtures/arazzo/`), not an assertion list: a clean export validates, a refused
  one is asserted *not* to. `gecko-surf` gains no dependency (the module is pure stdlib);
  `jsonschema` is dev-group only and the test `importorskip`s it.

### Fixed
- **A cyclic seed dependency was rendered as a confident derivation order.** When two
  PDAs of an instruction seed from each other — or a PDA seeds from itself — the graph
  cannot order them, but `_derivation_order` fell back to declaration order and emitted
  it with `resolvable=True` and no marker. An orchestrator ingesting that derives `a`
  first, which needs `b`, and fails at build time far from here. The loop was bounded
  against hanging; nothing guarded against lying.

  Now the residual of Kahn's algorithm is reported: the affected accounts stay in
  `derivation_order` (dropping an account is the worse failure) but are each
  `resolvable=False`, and `InstructionGraph.cycle` — carried into `to_json()` — names
  them. `find_start` reads the same signal through the new
  `derivation_order_with_cycle` seam and emits those accounts as **FLAGGED** steps with
  the cycle members in the note, so they surface as honest gaps rather than plan
  positions. No provenance value was added; `flagged` already existed for exactly this.

  Measured: **0 of 11** wired start cards and **0 of 144** instructions across the four
  packaged Orquestra IDLs contain a cycle, so no shipped plan changes. `find_start`
  retrieval eval unchanged (recall@1 0.74, recall@3 0.89, MRR 0.81, 4/4 out-of-scope
  rejections, 0 false accepts).
- **The most common chain in REST produced no edge.** `GET /customers` →
  `GET /customers/{id}` yielded zero `feeds` edges and no plan. A bare `id` carries no
  entity on either side, and the consumer-side `or` chain resolved a path param named
  `id` to the entity `"id"` — which never matches the producer's `"customer"`. The
  `rnoun` term meant to fix that sat third in the chain and was structurally
  unreachable, and the `scoped-id:` basis it was supposed to mint appeared in no test
  and no output.

  New rule 1b scopes both sides by the RESOURCE the operation names — the response
  object's title on the producer (falling back to the producing path's noun), the
  consuming path's noun on the consumer — and mints an INFERRED edge with basis
  `scoped-id:<entity>`. The scope is the control: a `Customer.id` does not feed
  `/orders/{id}`. Only a PATH param is scoped this way; a query `id` says nothing about
  which resource it belongs to, so it falls through to the existing rules unchanged.

  Measured across every committed spec (`scoped-id` edges admitted / edges removed):
  pegana **+3 / 0**, pegana_p0 **+3 / 0**, privy **+0 / 0**, txline **+0 / 0**, and
  +0 / 0 on the six smaller fixtures. All three pegana edges are the real webhook
  chain (`list_webhooks.id` → `delete_webhook{id}`, → `patch_webhook{id}`,
  `patch_webhook.id` → `delete_webhook{id}`); pegana gains two plans, both sourced
  from the `GET`. `find_start` retrieval eval unchanged (recall@1 0.74, recall@3 0.89,
  MRR 0.81, 4/4 out-of-scope rejected, 0 false accepts).

## 0.10.2 — 2026-08-06

### Fixed
- **`gecko prove` was STILL broken in 0.10.1.** The 0.10.1 fix patched one multi-argument
  `joinpath`; a second one in `find_start._packaged_overlay` spanned three lines, so the
  regex sweep that "confirmed the tree was clean" never saw it. Both are one segment per
  call now.

  Two point fixes in a row is a signal to stop fixing points, so the guard is now
  structural: a test walks the package AST and fails on **any** `joinpath` with more than
  one argument. A new call site now fails in CI rather than in someone's terminal — which
  is where both of these were found, by running the published binary.

## 0.10.1 — 2026-08-06

### Fixed
- **`gecko prove` crashed in the published binary.** Every packaged-config read went
  through a multi-argument `joinpath`, which works from source but raises
  `TypeError: MultiplexedPath.joinpath() takes 2 positional arguments but 3 were given`
  inside a PyInstaller binary — so 0.10.0 shipped with the new command broken while the
  full suite passed. One segment per call now.

  **The gap was the test surface, not the code.** Nothing exercised the frozen artifact,
  so the one environment users actually get was the one nothing looked at. Added a fake
  that reproduces `MultiplexedPath`'s one-segment constraint, plus a guard asserting the
  fake still rejects the broken call — a fake that quietly accepted two segments would
  pass against the very bug it exists to catch.

## 0.10.0 — 2026-08-06

The release where a verified plan became a landed transaction.

### Added
- **`gecko prove "<intent>"` — a sentence in, a receipt out.** Routes an intent to the
  right call, shows the candidate field it searched (scores, matched terms, and what was
  demoted to a guess), lists the account set with provenance, simulates, prints the
  receipt. Exit `0` lands / `1` routed but fails / `2` nothing routed.
- **`gecko watch <plan.json>` — drift as CI.** Re-simulates the calls a team depends on
  on an interval and reports N-confirmed drift. Exit `1` on confirmed drift, or on a
  target that has been unrunnable for N consecutive passes — a call that can no longer be
  *built* is not a lesser break than one that reverts.
- **`evaluate_tx` — the receipt bound to the message a signer will sign.** Hashes the
  MESSAGE, not the transaction, so the binding survives signing. Two strengths, named
  honestly: `structural` (blockhash normalised out — what a simulation with
  `replaceRecentBlockhash` can truthfully claim) and `exact` (the whole message,
  available with a real blockhash, expiring with it). Fail-closed throughout.
- **Versioned (v0) transactions + address-lookup-table resolution.** A legacy message caps
  out near 35 accounts and 1232 bytes; a multi-hop aggregator route exceeds that routinely.
- **Real blockhash and priority-fee helpers** (`latest_blockhash`,
  `priority_fee_microlamports`) — the two things a replaced-blockhash simulation never
  needed and a real send cannot do without.
- **Jupiter as a program surface**, and a fourth account provenance tier:
  **`cross_surface`**, for accounts supplied by a *different* surface at request time. The
  program surface declares 9 accounts for `route`; the instruction that lands carries 25.
  The other 16 are route legs that exist in no IDL — not a deficient IDL, *any* IDL.
- **Runnable landing flows** for Pump.fun buy/sell, Meteora swap, ORE claim and MetaDAO
  fund, each proven on a mainnet fork.

### Fixed
- **Usage was unattributable.** `install_id` rode on `surf.onboard` alone, so the events
  that prove value carried no identity; a TOCTOU race minted a fresh id per process; and
  `gecko serve` — the same entry point as the hosted server — declared a local identity,
  which would have stamped the server's own id onto every visitor's traffic.
- **The retrieval floor had no teeth.** A single incidental token overlap produced a
  *runnable* start. A runnable start now needs a term that names the program or its
  instruction, or two independent distinguishing terms.

### Notes
- Derivation is proven on five program surfaces, not "any Solana program".
- Drift detection is opt-in: `gecko watch` runs when you run it. There is no hosted
  scheduler.

## 0.9.5 — 2026-07-31

### Fixed
- **Cap `mcp[cli]<2` so serving works from the binary / `uvx` / `pip`.** mcp 2.0.0
  removed the low-level `Server.list_tools`/`call_tool` decorators the serve path uses;
  unpinned installs (the PyInstaller binary and `uvx`/`pip`, which don't read `uv.lock`)
  resolved 2.0.0, so **every bundled surface crashed on serve** with `'Server' object has
  no attribute 'list_tools'` — including the `npx @geckovision/gecko orquestra … --stdio`
  path. Reproduced with a minimal PyInstaller build and verified the cap fixes the frozen
  binary. (Tests, hosted server, and dev already used 1.x via `uv.lock`, so this was
  invisible until the frozen-binary serve was smoke-tested.)

## 0.9.4 — 2026-07-31

### Added
- **`gecko orquestra --program <name>` — the npx path.** The Orquestra provider surface
  is now a `gecko` subcommand, so `npx @geckovision/gecko orquestra --program meteora`
  reaches it (parity with `uvx … gecko-orquestra --program meteora`). The standalone
  binary is now built with the `[solana]` extra so `solders` is bundled and PDA
  derivation works in the frozen binary / npx path.

## 0.9.3 — 2026-07-31

### Changed
- **`gecko-orquestra --program <name>` — the provider-level entry.** The Orquestra
  provider surface is now invoked by *provider*, with the program as a parameter
  (`gecko-orquestra --program meteora`), so a new Orquestra program is a registry entry —
  not a new CLI. Matches the per-provider integration model.
- **`meteora-demo` is now a deprecated alias** for `gecko-orquestra --program meteora`
  (kept working, prints a deprecation hint to stderr).

## 0.9.2 — 2026-07-31

### Added
- **Orquestra provider surface + the Meteora instance.** The agent front door for the
  Gecko × Orquestra integration: the agent connects to a Gecko surface, plans in plain
  English, Gecko derives the PDAs Orquestra can't (the helper-seeded roots its IDL drops)
  and returns a plan that points at Orquestra's own builder to execute — control plane, we
  never proxy or sign. `gecko.providers.orquestra.OrquestraProgramSurface` (generic) +
  `gecko.providers.meteora` (first instance, recipes as data). New `meteora-demo` CLI:
  `uvx --from "gecko-surf[serve,solana]" meteora-demo` — `plan_swap(WSOL, USDC, bin_step=4)`
  derives the real mainnet SOL/USDC pool + reserves/oracle and points at the swap builder.

## 0.9.1 — 2026-07-31

### Added
- **Derive the min/max pool-pair seed the IDL drops (#4057).** New
  `OrderedPairPdaSeedNode` (`left`, `right`, `select=min|max`) makes the canonical AMM
  pool-pair ordering — Meteora's `min/max(token_x, token_y)`, Anchor's `max_key(a, b)` —
  a **resolvable** seed instead of a flagged gap. `derive_pda` sorts the two bound
  operands by their on-chain bytes and takes the selected end; `from_source` recognizes
  `min/max/min_key/max_key(a, b)`; `PdaNode.required_bindings` lists every operand a
  caller must supply. Proven end-to-end: `from_source` on Meteora's real
  `commons/src/pda.rs` recovers `lb_pair` as resolvable and derives the exact live
  mainnet SOL/USDC pool. (A genuinely-unknown helper still becomes an honest resolver.)

## 0.9.0 — 2026-07-31

### Added
- **The Program Surface — the instruction↔PDA graph for Solana programs.** The on-chain
  twin of the Agent Surface: turn any Solana program into the deterministic
  instruction ↔ account ↔ PDA ↔ seeds graph an agent traverses to build a correct
  transaction, by **recovering the PDA seed recipes an IDL/llms.txt loses** (Anchor #4057;
  Steel/native programs emit no IDL). New `gecko.pda` (Codama-shaped seed model +
  `derive_pda`, `solders` behind the optional `[solana]` extra, with an honest
  `ResolverPdaSeedNode` for seeds it won't fabricate), `gecko.pda_extract` (`from_source`
  + `from_anchor_idl` + `merge_pda_nodes` — "both, joined"), `gecko.program_graph`
  (`build_program_graph` → the derivation DAG + JSON), `gecko.program_mcp`
  (`get_program_graph` / `plan_instruction` / `derive_pda` tools), and `gecko.pda_testkit`
  (a surfpool `$0` derive→verify harness). Proven against real ORE mainnet PDAs.
- **`program-mcp` and `ore-mcp` CLIs.** Serve a program's PDA graph over MCP from local
  IDL/source (`program-mcp`) or the bundled ORE surface (`ore-mcp`). Keyless, never signs.
- **Hosted `/ore/mcp`.** The ORE Program Surface is mounted on the hosted multi-surface
  server (public, ungated), so an agent can add `<host>/ore/mcp` and derive ORE's PDAs live.
- **`[solana]` optional extra** (`solders`) — the PDA-derivation backend; the model and
  extraction stay pure stdlib.

### Fixed
- The hosted image now installs the `[solana]` extra, so hosted `/ore/mcp` `derive_pda`
  works (was returning a "missing extra" error).
- `SurfpoolFork` reaps surfpool's child validator on exit (process-group signal) instead
  of leaking it on the RPC port.

## 0.4.18 — 2026-07-24

### Added
- **The Agent Surface, named.** A new `Surface` value object (`gecko.surface`) composes the
  shipped engine — the deterministic call graph + provenance, the question-shaped tools, the
  anti-poisoning `SafetyVerdict`, and the `llms.txt`/`gecko.json`/`SKILL.md` projections —
  behind one handle (`.graph`, `.plan(intent)`, `.tools()`, `.safety`, `.project(kind)`).
  Behavior-preserving: it holds an `AgentApiClient` and delegates. `Surface`/`SafetyVerdict`
  exported from the package. The README leads with the three-layer frame: shape (OpenAPI) →
  transport (MCP) → **surface** (Gecko), composing on both, never replacing them.
- **Render the Surface as an SVG call graph — "graphviz for APIs".** `Surface.render_svg()`
  and `gecko graph svg <spec>`: operations as nodes, `feeds` edges as arrows colored by
  provenance (emerald DECLARED, amber INFERRED). Deterministic, self-contained (pure stdlib,
  no graphviz binary), control-plane clean (structure only). In-degree layered layout,
  width bounded.
- **`gecko connect <surface> --probe`** — a one-shot self-test that connects, lists tools,
  prints the result, and exits (so `connect` is verifiable from a terminal instead of a
  server that silently waits for an MCP client).

### Changed
- **A `gecko login` account is the verified email, not the `did:privy:…` subject.** Grants
  are now against a human-readable id (`gecko keys grant you@example.com --surface X`)
  instead of an opaque subject that silently mismatched the login key. Not retroactive:
  keys already minted against a subject stay bound to it (re-login to rebind).

### Fixed
- **A broken keychain read no longer blocks the env fallback.** `ChainResolver.resolve`
  treated a backend that *raised* (a present-but-broken macOS keychain, -25244) as fatal,
  crashing `gecko connect` before it could use `GECKO_CRED_GECKO_IDENTITY` — the MCP client
  reported the crash as "couldn't connect", indistinguishable from a host problem. A raising
  backend is now a miss (fall through), while a deliberate `CredentialError` still
  propagates. `connect` errors now surface the real reason (DNS/TLS/refused) and the target
  URL; `gecko doctor` does a real keychain write→read→delete round-trip instead of a
  misleading "available".

## 0.4.17 — 2026-07-23

### Fixed
- **`gecko login` no longer crashes (and loses the key) when the OS keychain refuses the
  seal.** A macOS keychain that is present but blocks the write (an unsigned frozen
  binary → `errSecInteractionNotAllowed` -25244, a locked keychain, a non-interactive
  session) raised `keyring.errors.KeyringError`, which is not an `OSError`, so it escaped
  every caller's `except` and tracebacked the CLI *after* the key had been minted
  server-side (and returned exactly once). `KeyringBackend.store` now maps it to a
  redacted `CredentialError`, and `run_login` degrades: it shows the key ONCE with the
  `export GECKO_CRED_GECKO_IDENTITY=<key>` fallback `gecko connect` reads, instead of
  losing a valid key.

### Added
- **`.md` twin fetch in `gecko from-docs`** — when a docs page recovers almost nothing,
  the `<url>.md` twin (Stripe/Mintlify authored markdown) is tried before the browser
  render: cheaper and higher-signal than a scraped DOM.

## 0.4.15 — 2026-07-22

### Fixed
- **`gecko connect` never exited when its client closed stdin**, leaking an orphaned
  process on every MCP-client restart. Each transport runs a writer task that loops
  over its write stream and its context manager will not exit while that task lives;
  the bridge held both sinks open, so on stdin EOF the bridge returned but
  `stdio_server.__aexit__` waited forever. Closing stdin is exactly how an MCP client
  shuts a server down, so this fired on every restart. The bridge now closes both
  sinks once forwarding ends.

  Caught by a live smoke, not the suite: the original teardown test used in-memory
  streams, which do not model a transport waiting on its writer, so nothing hung and
  the test passed. Both new regression tests fail without the fix.

## 0.4.14 — 2026-07-22

### Security
- **Self-service login granted ACCESS, not just identity.** `store_key` set
  `enabled=True` unconditionally and `gecko login` mints through that same call, so any
  address that passed the email OTP received a key the gate accepted — every gated
  (paid) surface was reachable by anyone who could receive email. `keyauth.authorize`
  already documented "not enabled BY THE FOUNDER ⇒ deny"; login silently made that
  untrue. Login-minted keys now land `enabled=False`: login establishes identity,
  access stays a deliberate founder act. Founder-run `gecko keys mint` still lands
  enabled — that IS the grant.
- **One key opened EVERY gated surface.** `KeyGate.decide` took no surface argument, so
  "enabled" meant "may reach every paid API". Invisible with a single gated surface, but
  adding a second would have silently handed every developer enabled for API #2 access
  to API #1. The gate is now scoped per mount, and access requires two independent
  switches: `enable/disable` (the account is live at all) AND `grant/revoke` (it may
  reach THIS surface). Fail-closed throughout — a store that cannot express grants
  denies rather than degrading to a bare enabled check, grants default-deny, and
  `disable` still beats a surviving grant.

  **Migration:** existing keys carry no grant and therefore reach nothing. Restore
  access explicitly with `gecko keys grant <account> --surface <name>`.

### Added
- **`gecko connect <surface>`** — use a gated hosted surface with the Gecko key held in
  the OS keychain, so no secret is pasted into an MCP client config. It runs as the
  client's stdio MCP server, resolves the key through the normal credential chain
  (keychain → env), and bridges JSON-RPC frames verbatim to the hosted
  Streamable-HTTP mount. The client config holds a command, not a credential:
  `{"command": "gecko", "args": ["connect", "birdeye"]}`. Surface names are validated
  as mount names (no path traversal) and `--host` goes through the SSRF guard, so a
  bearer token can never be sent to loopback, a private range, or link-local.
- **`gecko keys grant|revoke <account> --surface <name>`** and `gecko keys mint
  --surface` (mint + grant in one act). `gecko keys list` now shows each account's
  grants.
- **`gecko serve` first-run ping** (`mode="serve"`) — the skill/plugin install
  channel (`/make-agent-ready` runs serve) becomes visible. Same envelope, same
  `GECKO_TELEMETRY=off` opt-out, same transparency line (on stderr — stdout may be
  the stdio JSON-RPC channel), fired once per install+surface, never per boot. The
  hosted ingest accepts `mode="serve"` in the same change (client/server lockstep).
- **`plane` event field** (`engine`|`surface`, closed set) on `surf.prepare` /
  `surf.first_call_correct` (engine) and `surf.call` (surface), documenting why
  all-time fcc > call is expected: fcc fires on every engine call outcome (demo,
  `gecko test`, recorded $0 flows included); `surf.call` only on MCP-surface
  invocations. The honest funnel queries are documented in `gecko/events.py`.

### Fixed
- **A rejected key hung the MCP client.** The transport's HTTP 403 escaped `gecko
  connect` as a raw ExceptionGroup: the process died with no JSON-RPC response and the
  client waited on `initialize`. Transport failures are now mapped to one redacted line
  naming the status and the remedy (exit 2, stdout left byte-clean because it is the
  protocol channel).
- **The documented headless credential fallback was unusable.** Slot `gecko-identity`
  produced `GECKO_CRED_GECKO-IDENTITY`, which no POSIX shell can `export`. `_env_key`
  now normalizes `-` to `_`; the pre-normalization name is still honoured on read, so a
  Docker `-e` or an MCP client `env` block that sets it does not regress.
- **Adoption telemetry counts adopters, not runs.** The install id is written
  atomically (temp + rename, never a torn file) and reused forever; an unwritable
  HOME degrades to a per-run id instead of crashing. The `gecko add` onboard ping is
  now idempotent per install+surface (a local `~/.gecko/pinged/` marker, written only
  after a ping actually left), so re-running `add` no longer re-counts. The ping URL
  is resolved at call time, so a dev/test harness that redirects it can no longer
  post into the production ingest. The test suite is structurally unable to post
  telemetry (suite-wide transport kill-switch).

## 0.4.13 — 2026-07-19

### Added
- **Chain plans reach the agent** — `search_capabilities` now attaches a `plan`
  block (ordered supplier steps + provenance-carrying `explain`) to the top hit
  when its required inputs aren't satisfiable from the stated intent. The agent
  gets the right *sequence* of calls first try instead of discovering it by
  trial and error; flat search is byte-identical when no chain is needed. Backed
  by the chain-FCC harness (`gecko/chain_eval.py`): both known TxLINE chains
  score first-plan-correct in recorded mode ($0).

## 0.4.12 — 2026-07-19

### Fixed
- **`npx @geckovision/gecko jupiter-mcp` / `colosseum-mcp` crashed** — the
  PyInstaller onefile binary did not bundle `gecko/examples/*.json|yaml`, so bundled
  surfaces raised `FileNotFoundError` on the npx channel (`txline-mcp` survived via
  its raw-URL fallback). Added `--collect-data gecko` to the build (~128 KB, no code
  change) so all three bundled surfaces work offline in the frozen binary.
- **`gecko auth test --live` could report ✓ on a call that never hit the wire** — it
  classified on HTTP status alone, but a `mode="live"` call silently degrades to
  recorded (quarantined / auth-unsafe surface) and returns a synthesized 200. It now
  treats any non-live run mode as inconclusive — the exact false-confidence `--live`
  exists to prevent.

## 0.4.11 — 2026-07-19

### Added
- **`gecko auth test --live`** — proves a credential actually *authenticates* (one
  safe auth-gated GET → HTTP status), not just that the keychain resolves a value. A
  resolvable-but-expired token now reports ✗ instead of a misleading `resolved ✓`.
  Auto-targets bundled surfaces (`txline`); `--spec`/`--base-url`/`--op` for any API.

## 0.4.10 — 2026-07-19

### Added
- **Bundled `txline-mcp` surface** — serve the TxLINE (TxODDS) API with no local spec
  file and no spec URL (`npx @geckovision/gecko txline-mcp --mode live --stdio`).
  Two-token auth sealed via `gecko auth set txline --account httpAuth|apiKeyAuth`.

## 0.4.9 — 2026-07-19

### Added
- **Multi-scheme (two-token) auth injection** — `gecko serve --auth-keychain` now
  injects *every* header-shaped security scheme a spec declares (e.g. TxLINE's
  `Authorization: Bearer` + `X-Api-Token` together), not just the first. Spec-driven
  and API-agnostic; single-scheme APIs are unchanged. `gecko serve` prints the exact
  per-scheme `gecko auth set` commands for a multi-token surface.

## 0.4.8 — 2026-07-16

### Added
- **`gecko login`** — hosted-identity enrollment via Privy passwordless email-OTP
  (email → one-time code → sealed credential). Gates only the hosted plane; local
  `gecko add` (recorded, $0) stays zero-login. Client-side flow, public app id only —
  the Privy app secret never enters the CLI. (#148)

### Fixed
- **Cloudflare `1010` User-Agent ban.** Outbound provider calls now send a real
  `gecko-surf/<version>` User-Agent instead of the default `Python-urllib/*`, which
  Cloudflare-fronted providers (e.g. Privy) reject with HTTP 403. (#148)

## 0.4.7 — 2026-07-15

### Fixed
- **macOS TLS blocker.** Frozen (npx) binaries bundle `certifi` and point the SSL
  context at it at startup, so every https call works out of the box — no more
  `CERTIFICATE_VERIFY_FAILED` on a clean Mac. (#143)
- **`gecko add <bare-domain>`** resolves via `https://` + spec discovery instead of
  treating the domain as a local file. (#142)
- **`gecko --version`** added at the top level (was falling into the serve parser). (#142)
- **npx-aware MCP wiring** — the registered serve command survives the npx cache so
  Claude can still spawn it. (#142)

### Added
- **Jito hosted surface: read ops live.** `getTipFloor` and the status/account reads
  serve live against mainnet; the money-movers (`sendBundle`/`sendTransaction`) stay
  catalog-only (the agent submits those directly to Jito — we are the catalog, not the
  relay). (#144)

### Note
- **Realigns npm + PyPI.** 0.4.6 was an npm-only re-stamp: `pyproject.toml` was never
  bumped, so the binary reported `0.4.5` and PyPI never advanced past 0.4.5. This release
  moves every marker to 0.4.7 in lockstep, so `gecko --version` is honest again and both
  registries land on the same version.

## 0.4.5 — 2026-07-14

### Added
- **Onboard ping (attribution).** `gecko add` emits one anonymous, control-plane-only
  event (API host, CLI version, OS, a random install id) to the hosted
  `/events/onboard` route — default-on with a printed transparency line;
  `GECKO_TELEMETRY=off` disables it entirely. Adopters finally become countable. (#137)
- **x402 live settlement client.** `HttpFacilitatorClient` (fail-closed, SSRF-validated,
  token-redacting) + `facilitator_from_env()` reading `X402_FACILITATOR_URL`,
  `X402_FACILITATOR_TOKEN`, `X402_PAY_TO`, `X402_ASSET`, `X402_NETWORK`.
  `X402_MODE=stub` remains the shipped default; the go-live sequence is documented in
  `docs/x402-go-live.md`. (#139)

### Fixed
- **Live mode on a multi-server spec fails closed.** `AmbiguousServerError` lists the
  spec's servers and asks for an explicit `base_url`/`--base-url` instead of silently
  calling `servers[0]` (often production — the money-API footgun). `gecko add --mode
  live` refuses up front on ambiguous specs; the hosted Jito provider surface is now
  pinned explicitly to mainnet. (#138)
- CLI copy: "wired" → "integrated" in the `add --mode` help text. (#136)

### Note
- 0.4.4 was an npm-only re-stamp release; PyPI stayed at 0.4.3. This release realigns
  npm and PyPI in lockstep.

## 0.4.3 — 2026-07-13

### Added
- **`gecko add <domain>` auto-discovers the spec.** When the ref isn't itself an OpenAPI
  document, `resolve_spec` probes common locations on the host (`/openapi.json`,
  `/swagger.json`, `/v1/openapi.json`, `/.well-known/openapi.json`, …) before falling
  back to docs recovery. Each probe is SSRF-validated and best-effort. So a dev can point
  `gecko add` at a bare domain, a docs page, or a spec — one command, any API — instead of
  hunting for an `openapi.json` a painful API probably doesn't publish. Direct spec URLs
  still short-circuit (no extra probing).

## 0.4.2 — 2026-07-13

### Added
- **Bundled example surfaces are now `gecko` subcommands** — `gecko jupiter-mcp` and
  `gecko colosseum-mcp` (previously only standalone console scripts). This gives them a
  zero-install path through the single `gecko` binary, so **`npx @geckovision/gecko
  jupiter-mcp`** (and `colosseum-mcp`) work with no Python and no local spec file. Lazy-
  imported, so they add nothing to `gecko add`/`doctor`.

## 0.4.1 — 2026-07-13

### Fixed
- **`gecko add` no longer crashes without a TTY.** For an API that declares auth, the
  hidden key prompt previously raised a raw `getpass`/`termios` traceback when run
  under an agent, in CI, or with piped stdin — the exact non-interactive contexts our
  agent-first users onboard in. It now degrades gracefully off a TTY: no key is read,
  the surface still comprehends and wires (recorded/$0 needs no key), and the CLI
  prints the documented "add later with `gecko auth set`" hint. The secret is never
  echoed or logged.

## 0.3.0 — 2026-07-10

Governance + sessions. This release turns Gecko from "call the API correctly" into
"call it correctly **and** govern what the agent does" — plus real handling for the
short-lived-token auth pattern most production APIs use.

### Added
- **Governance tier + policy gate** — a deterministic classifier reads whether an
  operation is a `read` / `write` / `transfer` from the parsed spec (money-verb
  lexicon + amount∧recipient co-occurrence). An operator-authored `AgentPolicy`
  (`spend_cap` + `recipient_allowlist`) blocks a call **only** at the intersection
  with `tier == transfer` — a steered over-cap/off-allowlist transfer is refused
  while a benign read/write only ever steps up. Tier feeds `score_call` as a
  reason; it is never a blocking signal on its own.
- **Session identity** — `SessionIdentity` binds a session to its `AgentPolicy` and
  a non-secret free-tier id (shape-now-token-later); `GovernedSession` wraps any
  session and returns byte-identical wire headers (policy rides out-of-band).
- **Session lifecycle — token refresh + self-heal** — for OAuth-style APIs with a
  short-lived access token + refresh token: a `RefreshableSession` refreshes
  proactively inside `auth_headers()` before expiry, and a bounded-once reactive
  self-heal retries a 401'd live call after re-authenticating. `OAuth2Lifecycle`
  refreshes via a `refresh_token` grant; `oauth2_from_dpo2u()` reads a local OAuth
  token file. All behind the frozen `AuthSession` seam — a plain session is
  byte-identical.
- **Bundled Jupiter Swap API example** — `uvx --from "gecko-surf[serve]"
  jupiter-mcp`. Keyless by default (free tier), optional `JUPITER_API_KEY` (Pro)
  injected at call time.
- **BM25F retrieval** — Okapi BM25F with OpenAPI-remapped field weights; adopted
  above ~50 operations where it lifts recall (gate-confirmed on a 159-op surface),
  a no-op below.

### Fixed
- **Comprehension summary on fully-gated APIs** — an API where every operation is
  behind a bearer token reported `0` usable tools (the served, auth-filtered view).
  It now reports the full comprehended surface with an honest "N tools require
  authentication — Gecko injects the credential at call time" warning.

## 0.2.0 — 2026-07-03

The first release since the MCP-Registry launch. Everything below is on PyPI for
`uvx --from "gecko-surf[serve]" gecko ...` and the Claude Code plugin.

### Added
- **Agent-native emit** — any comprehended API gets its own discovery surface:
  `llms.txt`, `gecko.json`, `/.well-known/gecko.json`, `tools.md`, generated from
  the comprehended surface (control-plane only). Served as routes on the MCP
  server and writable for provider hand-off via `gecko <spec> --emit-dir <dir>
  [--site-url ...]`. Every emitted field is sanitized (anti-poisoning +
  secret-shape redaction + markdown neutralization); the capability map lists
  usable operations only.
- **`gecko test`** first-call-correctness suites and **`gecko from-docs`**
  (recover a draft OpenAPI from a human doc page) are documented as shipped.
- **Usage events** (`[events]` extra, `gecko/events.py`) — control-plane
  `surf.search` / `surf.prepare` / `surf.call` metadata with a closed field
  allowlist. **No-op unless `MONGODB_URI` is set** — a plain install never
  phones home; `GECKO_TELEMETRY=off` hard-disables.
- **Dense-hybrid retrieval arm** (`[dense]` extra) — MongoDB Atlas `autoEmbed`
  dense search fused with the lexical catalog (RRF). Benchmark-only for now;
  the agent-facing `search()` is unchanged.
- **Correctness-corpus provenance rails** — call outcomes carry `source`
  (`observed` / `reported` / `synthetic`); synthetic (recorded-mode) outcomes are
  segregated and never counted in first-call-correct metrics.

### Fixed
- **Below-scale surfacing:** on surfaces ≤50 operations, `search()` now surfaces
  every usable tool instead of top-k truncating — Gecko is now strictly ≥ a raw
  spec dump on small APIs (this was a real first-call-correct regression on
  clean APIs).
- Recorded-mode outcomes no longer fabricate HTTP 200s into correctness metrics.
- Dockerfile: fixed the stale pre-rename package path and bundled the `events`
  extra so the hosted image can emit.

### Changed
- License references unified to **Apache-2.0** across the repo (the license
  itself was already Apache-2.0).
- Hosted-deploy account identifiers moved out of the repo into env config.

## 0.1.1 — 2026-07-01

- MCP Registry release: `mcp-name: tech.geckovision/surf` ownership marker in the
  PyPI description; `server.json` published to registry.modelcontextprotocol.io.

## 0.1.0 — 2026-06-29

- First public release: comprehend an OpenAPI 3.x → question-shaped,
  first-call-correct MCP tools; hidden auth; `$0` recorded mode; Streamable-HTTP
  serve with one-click add strings; SSRF guard; anti-poisoning defenses.
