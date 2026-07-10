# The concrete map + build order — INTEGRATOR spec (brief 4d)

Status: SPEC / DESIGN only. A build PLAN, not code. Engine files untouched by this doc.
Date: 2026-07-09
Owner: `software-engineer` lane — "the code runs." Ties the three deep specs into one
strict, dependency-ordered build sequence where **every phase ships a $0 offline falsifier
FIRST** and **no phase touches `ingest`/`catalog`-core/`tools`/`caller`**.
Integrates (does not re-derive): `2026-07-09-prd-roadmap-coordination.md` (PART 4 §4d;
PARTS 2/3 matrix+roadmap; PART 5 locked decisions), `2026-07-09-context-contract-spec.md`
(frozen SurfaceNote schema, S0/S1/S2 + `with_notes`, refusal→remediation, NOTE_CAP 280),
`2026-07-09-semantic-depth-spec.md` (tier classifier, `evaluate_tier`, L0–L3-now/L4–L5-
corpus, tier-never-blocks-alone), `2026-07-09-corpus-observed-first-spec.md` (reuse
`corpus.CallOutcome`, 3-key egress, `--report-failures` nudge, DB gate 50/100k).

---

## 0. Executive summary (~12 lines)

- **One control point, built in five dependency-ordered phases; each opens with a named $0
  offline falsifier that can DISPROVE the phase before any wire/MCP work** (Pattern B).
- **The engine-core-frozen set is `ingest` / `catalog`-core / `tools` / `caller` + `apply_gate`
  + the `search_capabilities` return contract.** Every build item lands in a *sanctioned edit
  surface*: `risk.py` (the ai-ml signal home), `enforce.py` helpers (additive only, never
  `apply_gate`), `access.py`, `agentnative.py`, `serve.py`, `mcp_server.py`, `credentials.py`,
  `corpus.py`/`preflight_corpus.py`, and three NEW small modules (`policy.py`, `identity.py`,
  `notes.py`). None require an engine-core edit; §7 asserts this and names the one alarm to watch.
- **Phase 0** ships the ungated foundations: `keyring`→base dep (decision #4), and confirms
  the already-shipped `get_capability` + tokenizer fix + eval harness are green.
- **Phase 1** is the centerpiece software deliverable: ai-ml's `evaluate_tier` gatekeeper +
  tier classifier behind `score_call`, then `AgentPolicy` (new) + the **two governance
  blocking signals** (`cap.exceeded`, `recipient.not_allowlisted`) that block ONLY at the
  intersection with `tier==transfer` — honoring "tier never blocks alone."
- **Phase 2** wires context's emit plumbing (SurfaceNote store + `with_notes` + SKILL.md
  sub-line + additive `remediation`) and the identity shape (`identity.py` + `GovernedSession`).
- **Phase 3** wires data's observed-first runner capture hook + `/registry/feedback`.
- **Phase 4** is measurement-gated: note re-injection, `remediation` ship, the owed
  experiment (parallel), and the corpus-gated L4/L5 + BM25 arm.
- **Shared input on the critical path:** a transfer-bearing fixture (hand-authored
  `payments_tier.json`, Nora spec later) unblocks BOTH the tier golden set (Phase 1) and the
  owed experiment (Phase 4). Commit it early; do not block the code plan on Nora.

---

## 1. Module-by-module map (what changes, where)

| Concern | Module(s) | New/changed | Implements (upstream spec) |
|---|---|---|---|
| keyring base-dep | `pyproject.toml`, `uv.lock` | changed (move `keyring` extra→base) | PRD decision #4 |
| tier classifier + tier eval | `gecko/risk.py`, `gecko/evaluate.py`, `tests/fixtures/golden/tier_labels.jsonl` | changed + new fixture | semantic-depth §2, §6.1 |
| `AgentPolicy` + gov signals | `gecko/policy.py` (NEW), `gecko/risk.py` | new + changed | semantic-depth §2.5; PRD §5 governance falsifier |
| `remediation` refusal field | `gecko/enforce.py` (`refusal_payload` only) | changed (additive) | context §6.2 |
| SurfaceNote schema + store | `gecko/notes.py` (NEW, <300 lines) | new | context §2, §3; corpus §5 |
| `with_notes` + breadcrumb + T2 | `gecko/mcp_server.py`, `gecko/client.py` | changed (additive) | context §4 |
| SKILL.md note sub-line | `gecko/agentnative.py` (`_skill_md`) | changed (additive) | context §1 consumer 4, §5 |
| `SessionIdentity` | `gecko/identity.py` (NEW, <300 lines) | new | PRD §5 decision #3 |
| `GovernedSession` | `gecko/access.py` | changed (one adapter) | PRD PART 2 governed-auth |
| corpus canonical home + routing | `gecko/corpus.py`, `gecko/preflight_corpus.py` | changed (additive) | corpus §2.2, §1.3 |
| runner capture hook | `gecko/serve.py` / call path, `gecko/http_server.py` | changed | corpus §1.1, §6 Phase 1 |
| `/registry/feedback` route + flusher | `gecko/registry/` (route), `gecko/corpus.py` (batcher) | new route + changed | corpus §2.4 |
| owed experiment harness | `gecko/fcc_eval.py`, `scripts/` | changed (extend) | semantic-depth §5 |

**Frozen (this plan asserts NO edit):** `gecko/ingest.py`, `gecko/catalog.py` core,
`gecko/tools.py`, `gecko/caller.py`, and `enforce.apply_gate`. `catalog.CatalogEntry.blurb`
and the `search_capabilities` synthetic tool are *existing consumer slots* — additive use is
allowed; a signature change to either is the abstraction alarm (§7).

---

## 2. The strict dependency-ordered build sequence

Each phase: **FALSIFIER FIRST** (named, $0, offline) → the wire items → the phase gate. A
later phase never starts until the prior phase's falsifier is green.

### PHASE 0 — Ungated foundations (roadmap Phase 0, non-governance)

**Falsifier 0 (plain-install import + keychain-degrade).** A no-network test that (a) imports
`gecko.credentials` and resolves via the env fallback with `keyring` NOT installed-as-extra
still working, and (b) confirms `KeyringBackend.available()` degrades cleanly. Node ids:
`tests/test_credentials.py`, `tests/test_resolved_session.py`.

- **0.1 `keyring` → BASE dependency (decision #4).** In `pyproject.toml`: move
  `keyring>=25` from `[project.optional-dependencies].credentials` into `dependencies`
  alongside `pyyaml`. Keep the `[credentials]` extra as an empty back-compat alias (so
  `gecko-surf[credentials]` never errors). Regenerate `uv.lock` (`uv lock`). **Touches PR #89's
  manifest** — coordinate: rebase/land after #89 or fold the manifest delta in. Remove
  `keyring` from the `[[tool.mypy.overrides]]` ignore list only if types now resolve; else
  leave it. Falsifier: 0 above + `uv run mypy gecko` clean + a plain `uv sync` (no extras)
  can `import keyring`.
- **0.2 Confirm shipped Phase-0 items are green (verify, don't rebuild).** `get_capability`
  (already in `mcp_server.McpSurface.get_capability` + `client.get_tool`), the camelCase
  tokenizer fix, and the offline eval harness are marked [T→S] and have tests:
  `tests/test_get_capability.py`, `tests/test_tokenizer_eval.py`, `tests/test_golden_set.py`,
  `tests/test_fcc_eval.py`. Gate: all green on the current tree. If any is red, it is a Phase-0
  bug fix (failing test first) before Phase 1 opens.

**Phase-0 gate:** falsifier 0 + 0.2 suites green.

### PHASE 1 — Semantic tier + governance (roadmap Phase-0 governance falsifier; the centerpiece)

This is the "$0/no-network, steered over-scope/over-cap transfer blocked before it fires"
falsifier. Tier is comprehension (semantic-depth spec); the block predicate is policy (this
spec). They co-land because the block needs `tier==transfer`.

**Falsifier 1a (the gatekeeper — tier eval).** `evaluate_tier(operations, labels) ->
{precision, recall, confusion}`, pure/offline, run over a frozen sha256-pinned
`tests/fixtures/golden/tier_labels.jsonl` spanning read/write/transfer across ≥2 specs. If
L1+L2 cannot clear **precision ≥ 0.95 @ recall ≥ 0.80**, the tier signal does NOT ship
(semantic-depth §2.6, §6.1). Node id: `tests/test_tier_eval.py` (NEW). **Input dependency:**
the transfer-bearing fixture (§6) — commit a hand-authored `payments_tier.json` now.

**Falsifier 1b (the governance falsifier — the headline claim).** A pure-Python, no-network
test against a fake client + a hand-authored transfer op + an `AgentPolicy` (spend cap +
recipient allowlist) asserting:
1. a `tier==transfer` call **over the spend cap** → `decision == "block"`, refusal returned,
   upstream never called;
2. a `tier==transfer` call **to a non-allowlisted recipient** → `block`;
3. a `tier==transfer` call **within cap + allowlisted** → `allow`/`step_up`, never `block`;
4. **tier alone never blocks** — a transfer with NO governance predicate set →
   `step_up`, `score < block_at` (25 < 60);
5. `cap.exceeded` / `recipient.not_allowlisted` **alone on a non-transfer write** →
   `step_up`, never `block` (intersection-only).
Node id: `tests/test_governance_gate.py` (NEW).

- **1.1 Tier classifier (pure fn) behind `score_call` inputs** (semantic-depth §2.3–2.4).
  A deterministic weighted vote (Feature A money-verb lexicon on tokenized `path`+`operation_id`;
  Feature B amount∧recipient arg-shape co-occurrence; Feature C security-scope corroboration)
  → `tier ∈ {read, write, transfer}` + `tier_confidence ∈ {high, low}`. Lives in `risk.py`,
  reusing the catalog identifier tokenizer (do not re-invent). Ships only if falsifier 1a green.
- **1.2 Wire tier into `_op_risk` weighting** (semantic-depth §2.5). Replace the flat
  `_op_risk(method)` with a tier-aware sibling emitting `op.transfer` (25 pts, high conf) /
  `op.transfer_maybe` (12 pts, low conf) Reasons — **additive, never categorical.** `transfer`
  is NOT added to `BLOCKING_SIGNALS` (stays exfil/injection/quarantined). Falsifier: existing
  `tests/test_risk.py` + `tests/test_apply_gate.py` stay green + a new assertion that tier alone
  never yields `block` (falsifier 1b #4).
- **1.3 `AgentPolicy` (NEW `gecko/policy.py`) + the two governance signals** (semantic-depth
  §2.5, §2.7; PRD §5 #2). `AgentPolicy` is the OPERATOR-authored governance record (per-call
  `spend_cap`, `recipient_allowlist`) — distinct from the auto-derived `risk.RiskPolicy`
  (allowed_tools/trusted_hosts/thresholds). `score_call` gains an **additive keyword-only param**
  `agent_policy: AgentPolicy | None = None` (backward-compatible; existing callers unchanged).
  Two new signal functions in `risk.py`:
  - `cap.exceeded` — fires when `tier==transfer` AND an amount-shaped arg extracts over
    `agent_policy.spend_cap`. **Amount-parse fails SAFE** (unparseable ⇒ cannot assert over-cap
    ⇒ `step_up`, not block).
  - `recipient.not_allowlisted` — fires when `tier==transfer` AND a recipient-shaped arg is not
    in `agent_policy.recipient_allowlist`.
  **Weighting design (intersection-blocks-only):** each predicate is an additive Reason weighted
  so `transfer(25) + predicate ≥ block_at(60)` blocks, but `write(15) + predicate < 60` and
  `predicate alone < 60` only step_up. Candidate: predicate = **35 pts** (25+35=60 block ✓;
  15+35=50 step_up ✓; 35 alone step_up ✓). Final points are an impl detail validated by
  falsifier 1b, NOT by adding to `BLOCKING_SIGNALS`. This is the design that makes the two
  payoffs cleanly separable: **tier is comprehension, the block is policy** (§7 decision).
- **1.4 `remediation` refusal field (shape lands, ship gated to Phase 4)** (context §6.2).
  Additively extend `enforce.refusal_payload` with two OPTIONAL keys: `signals` (=
  `blocked_signals(assessment)` code-constant NAMES) and `remediation` (a frozen
  `signal → fix-string` map, NO arg values, control-plane safe). **`apply_gate` is untouched.**
  Byte-for-byte testable. Falsifier: `tests/test_enforcement_gate.py` +
  `tests/test_apply_gate.py` stay green; a new `remediation` map test asserts no arg value present.

**Phase-1 gate:** falsifiers 1a (precision/recall cleared) + 1b green; `test_risk`,
`test_apply_gate`, `test_enforcement_gate` green.

### PHASE 2 — Context emit plumbing + identity shape (roadmap Phase 1/2)

**Falsifier 2 (context structural contract).** Pure-Python, recorded mode, fixture surface +
fixture notes (context §7 Phase 0):
1. **Off-by-default:** `list_tools()` and default `search_capabilities(query)` contain NO note
   body — breadcrumb substring only (grep the projected JSON, assert note text absent).
2. **T0/T1 body-free:** `to_lightweight_ref` and the below-scale full-def path never carry a
   note body; only `with_notes=true` / `get_capability` surface it.
3. **Validated-at-write refuses:** a note with `..`, a `looks_like_secret_value` token, or
   `len > NOTE_CAP` (280) is REFUSED, and the rejected text does NOT appear in the raised error.
4. **Untrusted label present:** every injected body wrapped in the code-constant label between
   trusted desc and body; `author:"agent"` carries the stronger hedge.
5. **Token-budget bound:** T0-with-notes ≤ T0-baseline + `13·a`; one note body ≤ NOTE_CAP.
6. **Stale downgrade:** a `surface_rev`-mismatched note is breadcrumb-only, never auto-injected.
Node id: `tests/test_notes_contract.py` (NEW).

- **2.1 SurfaceNote schema + store (NEW `gecko/notes.py`, <300 lines).** The frozen schema
  (context §2): `schema_version, surface_id, surface_rev, target, note, author, updated_at`.
  Write-path validation REUSES existing guards — `sanitize.sanitize_text`,
  `sanitize.looks_like_secret_value`, path allowlist `^[a-zA-Z0-9._\-\/]+$` + `..` reject +
  key escape `/`→`--`, NOTE_CAP=280. Refuse (never truncate-then-store). Store at
  `~/.gecko/notes/{surface_id}/{surface_rev}/{target}.json`, append-or-replace-whole-record
  (data spec §5 owns the physical bytes contract; this module upholds the invariants).
- **2.2 `with_notes` + breadcrumb + T2 body-load** (context §4). In `mcp_server.py`:
  `search_capabilities` gains an **additive optional** `with_notes` property; default
  (absent/false) output is BYTE-IDENTICAL to today. Breadcrumb per annotated hit at S1;
  `get_capability(name)` is the body-load door at S2. The `_SEARCH_TOOL.inputSchema` gains one
  optional property — the required contract `{query}` and the returned hit shape
  `{name,summary,path,method,inputSchema}` are unchanged (§4 seam test).
- **2.3 SKILL.md note sub-line** (context §1 consumer 4, §5). In `agentnative._skill_md`,
  append an op-level, `surface_rev`-CURRENT note as an indented `- note: <text>` under that
  tool's bullet, routed through `_safe` (same sanitize/redact/cap as every emitted field).
  Stale notes are NOT emitted. Node id: `tests/test_skill_emit.py` (extend).
- **2.4 `SessionIdentity` (NEW `gecko/identity.py`, <300 lines, leak suite)** (PRD §5 #3).
  Shape-now-token-later: an identity binds a session to an `AgentPolicy` (comprehension-derived
  allowed_tools + operator governance) and an anon free-tier id; token derivation is
  **pass-through** until a customer demands per-session revocation. Leak suite:
  `repr()`/`str()` carry no secret; a bound credential never appears on the instance. Node id:
  `tests/test_identity.py` (NEW).
- **2.5 `GovernedSession` in `access.py` (one adapter, seam-identity test)** (PRD PART 2).
  Wraps a `ResolvedSession` + a `SessionIdentity`; `auth_headers() -> dict[str,str]` returns the
  SAME header dict as the underlying session (the whole `AuthSession` seam held) while
  credential-selection-by-policy happens inside. Recorded mode never constructs it. Node id:
  `tests/test_governed_session.py` (NEW) — asserts seam parity + repr-safety.

**Phase-2 gate:** falsifier 2 green; `test_list_tools_scale_projection`, `test_skill_emit`,
`test_client_mcp`, `test_access` green.

### PHASE 3 — Corpus observed-first (roadmap Phase 2)

**Falsifier 3 (control-plane + transport contract).** Pure-Python, recorded/fixture mode
(corpus §6 Phase 0):
1. **Payload-free by construction:** `outcome_from` cannot receive a body (`status: int | None`
   only); grep the produced row — no fixture body/URL substring in any field.
2. **Source routing:** `mode="live"`→`observed`→`outcomes.jsonl`; `mode="reported"`→
   `reported.jsonl` and EXCLUDED from the observed-only FCC aggregate; `mode="recorded"`→
   `synthetic.jsonl`. A naive `outcomes.jsonl` reader sees neither (segregation-by-path).
3. **Cross-request keying:** rows under one `(surface_id, surface_rev)` across two fixture
   `session_id`s → read-path returns BOTH (not partitioned by session).
4. **Exact posted body byte-for-byte:**
   `{"items":[{"error_class":"unprocessable_422","surface_id":"api.example.com","surface_rev":"<rev>"}],"v":1}`
   (canonical `sort_keys`, compact separators); assert NO `arg_shape`/`operation_id`/value
   substring crosses the wire.
5. **Endpoint allowlist fails closed:** off-vocab class / extra key / creds-in-`surface_id` /
   malformed `surface_rev` dropped; over-cap batch (>500 items or >64 KB) refused whole.
6. **Default-nudge, not default-on:** with `--report-failures` OFF, an observed failure row is
   written LOCALLY and NO upload attempted (fake sink received nothing); nudge line once.
7. **Note/row separation:** `CallOutcome.__dataclass_fields__` has no `note`/free-text key.
Node ids: `tests/test_corpus_controlplane.py`, `tests/test_corpus_source.py`,
`tests/test_integrity_corpus.py` (extend) + `tests/test_feedback_route.py` (NEW).

- **3.1 Canonical corpus home + `reported` divert + layout** (corpus §2.2). Add
  `~/.gecko/corpus/{surface_id}/{outcomes,reported,synthetic}.jsonl` + built `index.json` +
  `_classes.json`. Route `reported` by path exactly as `synthetic_sibling` already diverts
  synthetic. `--accept-reported` (default OFF) gates the reported write.
- **3.2 Runner capture hook** (corpus §1.1, §6 Phase 1). In the runner call path
  (`serve.py`/`http_server.py`/`McpSurface.call_tool`'s result boundary), call
  `corpus.outcome_from(status=..., mode=...)` at the WIRE-STATUS boundary → `corpus.record()`
  to the canonical home. The body is structurally unreachable (`outcome_from` takes `status`,
  never the result dict). **This is the observed-first hook.**
- **3.3 `/registry/feedback` route + batched flusher + nudge** (corpus §2.4). New registry
  route mirroring `events.py`'s allowlist receiver (reject off-allowlist keys, `error_class ∈
  ERROR_CLASSES`, `_safe_surface_id`, `surface_rev` hex shape, ≤500 items/≤64 KB). Batched
  best-effort flusher (100 items / 60 s / shutdown), fire-and-forget. `--report-failures`
  default-OFF with the one-line default-nudge.

**Phase-3 gate:** falsifier 3 green; `test_corpus_controlplane`, `test_corpus_source`,
`test_integrity_corpus`, `test_registry_api` green.

### PHASE 4 — Measurement-gated (roadmap Phase 2/3; ships only on measured lift)

No new blocking falsifier — these ship ONLY when the ai-ml harness shows lift.
- **4.1 SurfaceNote re-injection** lands only where `fcc_eval` shows a note fixes a call
  (context §7 Phase 2). If FCC doesn't move, the note is rot — don't inject.
- **4.2 `remediation` ships** only on measured self-correction lift (context §6.2 rule).
- **4.3 The owed experiment harness** (semantic-depth §5) — extend `fcc_eval` to BARE
  (docs-fed) vs GECKO + tokens-to-first-correct-call; recorded first, live final on selected
  APIs; **runs in PARALLEL with Pegana WTP** (decision #5). Needs the transfer/painful fixture
  (§6). Records go to `private/` (gitignored). No edge claim ships before it reports.
- **4.4 L4/L5 + BM25 arm** — corpus-gated (`fcc_eval.lift_corpus > 0`) and >50-op-gated
  respectively; both stay OFF until their evidence gate fires.

---

## 3. How each build item maps to an upstream spec's output

- **Context's emit + refusal** → items 2.1–2.3 (SurfaceNote store, `with_notes` injection,
  SKILL.md sub-line) + 1.4 (`remediation`/`signals` additive on `refusal_payload`). The
  off-by-default state machine (S0/S1/S2), NOTE_CAP=280, validated-at-write refusal, and the
  code-constant untrusted label are implemented verbatim; nothing is re-authored.
- **ai-ml's tier `Reason` behind `score_call`** → items 1.1–1.2. Tier is a `Reason`
  (`op.transfer`/`op.transfer_maybe`), additive, never in `BLOCKING_SIGNALS`, gatekept by
  `evaluate_tier`. The governance BLOCK (1.3) is the intersection `tier==transfer AND predicate`,
  keeping comprehension and policy separable.
- **data's capture hook in `serve.py` + the feedback route** → items 3.1–3.3. Observed-first
  runner hook at the wire-status boundary; `reported` quarantined by path; the 3-key egress
  asserted byte-for-byte; DB gate (50 surfaces / 100k rows → Mongo) is a documented later
  trigger, NOT built here.

---

## 4. Seam-identity test list (proves the engine contract held)

The invariant this whole plan defends: the comprehension engine is untouched and its
agent-facing contracts are byte-identical. These tests are the proof.

1. **`search_capabilities` frozen shape.** Default call (no `with_notes`) returns output
   byte-identical to today; the enriched hit is still
   `{name,summary,path,method,inputSchema}`; `with_notes` is an additive optional property, and
   `{query}` stays the sole required field. Node: `tests/test_client_mcp.py`,
   `tests/test_get_capability.py` + a new default-parity assertion in `tests/test_notes_contract.py`.
2. **`apply_gate` unchanged.** `enforce.apply_gate` behavior and source are untouched (no new
   gate); every verdict×mode→action mapping identical. Node: `tests/test_apply_gate.py`.
3. **Engine-core byte-identical.** `ingest.py`, `catalog.py` core, `tools.py`, `caller.py`
   carry no diff; a CI guard asserts `git diff --quiet` on those paths, and
   `tests/test_ingest.py` / `test_catalog.py` / `test_tools.py` / `test_caller.py` stay green.
4. **`BLOCKING_SIGNALS` unchanged.** The frozenset is still
   `{exfil.host, poison.injection, provenance.quarantined}` — `transfer`/`cap.exceeded`/
   `recipient.not_allowlisted` are NOT members. Node: `tests/test_risk.py`.
5. **`list_tools` projection byte-identical** when `with_notes` off / honeypots off, at both
   scales. Node: `tests/test_list_tools_scale_projection.py`, `tests/test_surface_all.py`.
6. **`GovernedSession` seam parity.** `GovernedSession.auth_headers()` returns the same
   `dict[str,str]` as its underlying `ResolvedSession`/`Session` (the `AuthSession` seam held);
   repr carries no secret. Node: `tests/test_governed_session.py`.
7. **`CallOutcome` allowlist unchanged.** `__dataclass_fields__` has no `note`/free-text key;
   `ALLOWED_KEYS`/`ERROR_CLASSES` fail closed. Node: `tests/test_integrity_corpus.py`.
8. **Credential contract intact after keyring→base.** The leak suite and chain precedence hold;
   plain install still resolves via env. Node: `tests/test_credentials.py`,
   `tests/test_resolved_session.py`, `tests/test_auth_cli.py`.

---

## 5. Pre-commit for every phase

```bash
uv run ruff format && uv run ruff check --fix && uv run mypy gecko && uv run pytest <targeted node ids>
uv run python -m gecko.demo    # $0 recorded E2E smoke, when the call path is touched
```
Run targeted node ids per phase (never a bare background full sweep). The demo smoke is
mandatory for Phases 1–3 (all touch the call path).

---

## 6. Shared input on the critical path (flag, don't block)

Both **Phase-1 falsifier 1a** (tier golden set) and **Phase-4 owed experiment** need a
**transfer-bearing fixture** — the committed golden specs (txodds/pegana) are read-heavy with
no real transfer op (semantic-depth §2.8). Recommendation: **commit a hand-authored
`tests/fixtures/payments_tier.json`** (labeled read/write/transfer ops with money-verbs +
amount∧recipient bodies) NOW to unblock the code plan; fold in the **Nora** spec (spec only, no
key) or a **Stripe-subset** fixture when available for breadth. The owed experiment (decision
#5) runs in parallel and depends on this + a painful spec-less API — flag it as an INPUT, do
not serialize the build behind it.

---

## 7. Escalation — the abstraction alarm (staff-engineer)

**This plan asserts NO build item forces an `ingest`/`catalog`-core/`tools`/`caller` edit.**
Every item lands in a sanctioned surface. Two borderline cases are flagged, not hidden:

1. **`score_call` gains an additive keyword-only `agent_policy` param + `risk.py` gains signal
   functions.** `risk.py` is the explicitly-sanctioned home for ai-ml's signals (semantic-depth
   §2.5, §3.3) — NOT an escalation. Guardrail: additive, backward-compatible, `apply_gate` and
   `BLOCKING_SIGNALS` untouched. If a reviewer deems the signature change breaking → route to
   `staff-engineer`.
2. **SurfaceNote consumer #1 (blurb folding) and the `with_notes` property on `_SEARCH_TOOL`.**
   Context §1 treats `catalog.CatalogEntry.blurb` and `search_capabilities` as existing consumer
   slots; additive use is allowed. **IF folding a note into `CatalogEntry.blurb` requires a
   `catalog.py` core signature change, STOP and escalate to `staff-engineer`** (that is the
   abstraction alarm — a comprehension-engine edit the API-agnostic invariant forbids). Default
   posture: keep the note→blurb join at read time in `notes.py`, never in `catalog` core.

Any other item that appears to need an engine-core edit is a design error in THIS plan —
escalate before writing code.

---

## 8. Top 3 decisions / risks for the founder

1. **The two governance signals block ONLY at the intersection with `tier==transfer`
   (rec: YES).** `cap.exceeded` / `recipient.not_allowlisted` are additive high-weight Reasons
   (candidate 35 pts) tuned so a transfer + predicate blocks (25+35=60) but a predicate alone or
   on a benign write only step_ups. This honors BOTH "tier never blocks alone" (semantic-depth)
   AND "block the steered over-cap transfer" (governance falsifier). Alternative — making them
   categorical `BLOCKING_SIGNALS` — would block a metered-write false-positive and drift toward
   the "generic agent firewall" PRD risk. Confirm the intersection semantics.

2. **`keyring`→base touches PR #89's manifest — sequencing risk (rec: land after #89).** Moving
   `keyring` to base + regenerating `uv.lock` collides with #89's manifest changes. Recommend
   rebasing 0.1 onto #89's merge (or folding the one-line dep delta in) to avoid a lock-file
   conflict. This is the earliest item and blocks nothing downstream, so a short wait is cheap.

3. **A transfer-bearing fixture is the single shared input gating Phase 1 AND Phase 4
   (rec: hand-author `payments_tier.json` now).** Do not wait on the Nora sandbox key — commit a
   hand-authored labeled fixture to unblock the tier golden set (falsifier 1a) and seed the owed
   experiment, then add Nora/Stripe-subset for breadth. Without it, Phase 1's gatekeeper cannot
   run and the whole governance phase stalls.
```
