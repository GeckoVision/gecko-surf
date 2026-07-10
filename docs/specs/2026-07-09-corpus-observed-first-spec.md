# The correctness corpus, observed-first — DEEP spec (brief 4c)

Status: SPEC / DESIGN only. No implementation in this document. Engine files untouched.
Date: 2026-07-09
Owner: `data-engineer` lane — "the correctness corpus is correctly stored and retrievable."
Founder decision LOCKED: **observed-first = YES.** The local runner (`gecko serve`) on-path
is the primary, control-plane-clean capture point; `reported` is a supplementary,
quality-quarantined channel excluded from the published FCC rate.
Extends: `2026-07-09-context-hub-adoption-data.md` (the storage/cache slice this deepens),
PRD-roadmap-coordination PART 1 §4 + PART 4 §4c.
Ties: `gecko/corpus.py` (frozen `CallOutcome`, closed `ERROR_CLASSES`, `source`/`tenancy`),
`gecko/preflight_corpus.py` (`known_classes_from_corpus` = the flywheel read-path),
`gecko/events.py` (closed-allowlist telemetry writer), `gecko/serve.py` (the capture point).
Pairs with: `2026-07-09-context-contract-spec.md` (the SurfaceNote prose sibling — §5 here),
`2026-07-09-semantic-depth-spec.md` (L4/L5 depth is corpus-gated on these rows).

---

## 0. The question this spec answers

> **What is the minimal payload-free row that still teaches the next agent — and at what
> observed volume does file-based storage stop being retrievable enough?**

Answer, in one line: **the teaching-minimal row is the six-field core of the already-frozen
`corpus.CallOutcome` — `(surface_id, surface_rev, operation_id, arg_shape, error_class,
first_call_correct)` — captured observed-first by the runner from the wire STATUS (never the
body), keyed on `(surface_id, surface_rev)` and NEVER on `session_id`; file-based JSONL + a
built `index.json` stays retrievable up to the DB gate of ~100k observed rows in a single
surface OR ~50 surfaces under `~/.gecko/corpus/`, at which point it migrates to MongoDB
(already in the stack for keys + the Atlas dense index) with the row schema unchanged.**

Three things this spec freezes: (1) the **corpus row** (reuse `CallOutcome`, no new type),
(2) the **capture transport** (`--report-failures` opt-in semantics, batching, the exact
posted body — asserted byte-for-byte in the falsifier), (3) the **DB-gate number**. One thing
it reconciles: the **SurfaceNote** (context-engineer's prose hint) joins the categorical row
by keys and is NEVER copied into it (§5).

---

## 1. Observed-first capture — the runner on-path

### 1.1 Why the runner is the capture point (by construction, not policy)

`gecko serve` builds the `PreparedRequest` from surface metadata, injects auth via
`ResolvedSession.auth_headers()`, and `caller.execute()` fires at the provider. **The HTTP
status returns through the runner before the agent ever sees the body.** So the runner can
build a real, published-eligible row through `corpus.outcome_from(status=..., mode="live")`
→ `source="observed"` **without the body ever entering** — recall `outcome_from` takes
`status: int | None`, never the result dict that holds `data` (body) and `request` (filled
URL). The payload physically cannot reach the writer. This is the same architectural argument
the credential-resolver spec makes for the secret: the value lives in runner process memory
for the call and is never persisted because the only function that writes the corpus cannot
receive it.

### 1.2 The three capture sources — routing already modeled by `OutcomeSource`

| `source` | `mode` | Meaning | Feeds published FCC? | Uploads to flywheel? |
|---|---|---|---|---|
| **observed** | `live` (through the runner) | the runner saw the wire status | **YES** — the only published bucket | YES, on opt-in (§2.4) |
| **reported** | `reported` (agent called the API itself, reports the class back) | self-reported, quality-quarantined | **NO** (routed out) | **NEVER** |
| **synthetic** | `recorded` (validator's faked 200) | fabricated, segregated by path | NO | NEVER |

`source` is DERIVED from `mode` at the `outcome_from` boundary (`source_for_mode`), fails
closed to `synthetic`, and governs routing — a caller can never mislabel a faked 200 as
observed. This is the whole feedback-capture decision, and it is already load-bearing in
`corpus.py`. Observed-first means **the published FCC rate is `observed`-only**; `reported`
and `synthetic` exist but never inflate it.

### 1.3 The `reported` channel — supplementary, quarantined, opt-in

The direct-call remainder (agent bypasses the runner) is covered by `reported` WITHOUT
polluting the metric:

- **Segregated by path**, exactly as `synthetic.jsonl` is: `reported` rows land in
  `reported.jsonl`, so a naive reader of `outcomes.jsonl` FAILS CLOSED (never sees a
  self-reported row). Segregation-by-path, not an in-band tag — the same fail-closed
  discipline `synthetic_sibling` already enforces.
- **Off by default.** A `reported` row is accepted only when the operator sets
  `--accept-reported` (default OFF) on `gecko serve` / the client. The agent has no
  unsolicited write path into the corpus.
- **Never uploads.** Only `observed` rows are eligible for the registry flywheel (§2.4);
  `reported` is local-only, always.
- **Still allowlisted + closed-vocab.** A `reported` outcome is the same `CallOutcome`
  through the same `error_class ∈ ERROR_CLASSES` guard — self-reported does not mean
  free-text; the agent reports a CLASS, never prose.

### 1.4 The local write is unconditional; the upload is opt-in

Two distinct planes, do not conflate:

- **Local capture** (`~/.gecko/corpus/`) is unconditional for `observed`/`synthetic` and
  opt-in-gated only for `reported`. It never leaves the machine. This is the seed of the moat
  and costs nothing to keep local.
- **Registry upload** (`/registry/feedback`) is a SEPARATE, opt-in, batched, classes-only
  egress (§2.4) — off by default, `--report-failures`.

---

## 2. Storage — the frozen row, the layout, the endpoint

### 2.1 The frozen corpus row — reuse `CallOutcome`, do NOT fork

The V2 corpus row **is** `corpus.CallOutcome`. No new record type; the frozen dataclass IS
the persisted schema, backed by the `ALLOWED_KEYS` allowlist (fail-closed on any new key) and
the `ERROR_CLASSES` closed set (fail-closed on any stray class). Restated for this spec so it
is self-contained (all fields NAMES/TYPES/BOOLS/COUNTS — never a value):

```jsonc
// corpus.CallOutcome — FROZEN. The field set IS the schema (ALLOWED_KEYS).
{
  "ts":                1751990400000,        // epoch_ms
  "surface_id":        "api.example.com",    // cred-free host/slug, never a URL with creds
  "surface_rev":       "<opaque rev hash>",  // pins the comprehension; the version key (§2.3)
  "operation_id":      "verifyWebhook",      // spec-derived op NAME
  "method":            "POST",
  "path_template":     "/webhooks/{id}/verify", // TEMPLATED, never the filled URL
  "params_present":    ["id", "signature"],  // arg NAMES the agent supplied, never values
  "arg_shape":         {"id": "string", "signature": "string"}, // name -> JSON type, no values
  "body_present":      true,                  // a bool, never the body
  "status":            422,                   // the wire status — the core signal; null pre-flight
  "ok":                false,
  "error_class":       "unprocessable_422",   // a CLOSED ERROR_CLASSES member, never free text
  "first_call_correct": false,                // ok AND attempt == 1
  "attempt":           1,
  "latency_ms":        84,
  "mode":              "live",
  "auth_injected":     true,                  // a bool, never the token
  "source":            "observed",            // DERIVED from mode; gates the FCC metric
  "tenancy":           "local"                // may this egress? default local (fail closed)
}
```

**The teaching-minimal core (answers the first half of §0).** Of those fields, the six that
actually *teach the next agent* are:

> `(surface_id, surface_rev, operation_id, arg_shape, error_class, first_call_correct)`

That tuple says: *"on this op of this surface revision, an argument SHAPE like
`{amount:integer, to:string}` produced `unprocessable_422` and was NOT first-call-correct."*
The remaining fields (`method`, `path_template`, `params_present`, `body_present`, `status`,
`attempt`, `latency_ms`, `mode`, `auth_injected`) are corroborating context, kept because they
are already payload-free and free to store — but the minimal teaching signal is those six. No
field carries a value; the schema has **no slot** a response body, filled URL, or secret could
occupy (invariant #1 by construction).

### 2.2 The canonical home + layout (the one additive gap)

The only gap `corpus.py` leaves is a canonical default home. Fix it — a sibling of the
surfaces cache the runner already owns:

```
~/.gecko/
  surfaces/{name}@{rev}.json        # already exists — the surface (control plane)
  corpus/
    {surface_id}/
      outcomes.jsonl                # observed rows (published-eligible)      [append-only]
      reported.jsonl                # self-reported rows (quarantined, excluded from FCC) [append-only]
      synthetic.jsonl               # synthetic rows (segregated, fail-closed)  [append-only]
      index.json                    # BUILT: per surface_rev -> {counts by error_class,
                                     #   n_observed, fcc_rate, first_seen, last_seen}
    _classes.json                   # cross-API known-classes rollup (the flywheel cache;
                                     #   persisted form of known_classes_from_corpus)
  notes/                            # SurfaceNote prose store (§5) — SEPARATE from corpus/
```

- `outcomes.jsonl` / `reported.jsonl` / `synthetic.jsonl` are today's `corpus.record()`
  targets, relocated to a canonical home and routed by `source` at the single write boundary
  (already enforced in `record()` — synthetic diverts by path; this spec adds the `reported`
  divert by the same mechanism).
- `index.json` is the **built artifact** (the analog of chub's `registry.json`): a
  periodically-regenerated rollup so the read-path scans a small index, not a growing JSONL.
- `_classes.json` persists the flywheel rollup so a cold start does not re-scan every surface.

### 2.3 Keying — `(surface_id, surface_rev)`, NEVER `session_id`

The per-request-scoping trap (operating principle #2): a static, reused corpus **must not be
gated behind a request/session id**, or it silently disappears across requests.

- The corpus partition key is **`surface_id` + `surface_rev`**. A corpus built against rev N
  is never silently read as valid for rev N+1 (a drifted surface must not reuse stale
  correctness priors) — `surface_rev` is the version field, the analog of chub's
  `registry.json` version, cut to our unit.
- `session_id` exists in `events.py` ONLY as a connect↔call correlation token for telemetry.
  It is **not** a corpus partition key and must never become one. The read-path (§2.5) loads
  the whole per-surface store regardless of which request is asking — **cross-request reuse is
  the default**, exactly as the principle demands.
- The flywheel rollup `_classes.json` is deliberately **NOT rev-scoped and NOT surface-scoped**
  — a class learned anywhere is watchable everywhere (§2.6).

### 2.4 The `/registry/feedback` endpoint — classes-only, allowlisted, size-capped

The opt-in cross-customer egress. Only `observed` rows, only on `--report-failures`, only the
minimal triple — NOT the full row.

**Opt-in flag semantics (default-nudge):**

- `gecko serve --report-failures` (default **OFF**). Env alt `GECKO_REPORT_FAILURES=1`.
- **Default-nudge, not default-on** (chub defaults telemetry on; we do not). When OFF and a
  live surface produces its FIRST failure-class `observed` row, the banner prints ONE line,
  at most once per process:
  > `Enable --report-failures to help Gecko watch this failure class on other APIs`
  > `(classes + surface revision only — never your data, never a payload).`
  Local capture proceeds regardless; the nudge only affects egress.
- On opt-in, an uploaded row's `tenancy` is upgraded `local -> contributed` at the egress
  boundary (the one-way governance axis `corpus.py` already reserves). `reported`/`synthetic`
  are never eligible.

**Batching:**

- Local `outcomes.jsonl` append is synchronous and unconditional. The upload is a SEPARATE,
  best-effort, batched flush: accumulate up to **100 items** or **60 seconds**, whichever
  first; flush on process shutdown. Fire-and-forget — an upload failure never breaks a call
  and never blocks a local write (same posture as `events.py`).

**The exact posted body (asserted BYTE-FOR-BYTE in the falsifier, §6):**

`POST {registry_url}/registry/feedback`, `Content-Type: application/json`, body =
`json.dumps(body, sort_keys=True, separators=(",", ":"))` (canonical, deterministic):

```json
{"items":[{"error_class":"unprocessable_422","surface_id":"api.example.com","surface_rev":"<rev>"}],"v":1}
```

- Exactly THREE keys per item — `error_class`, `surface_id`, `surface_rev` — sorted; top-level
  keys `items`, `v` sorted; compact separators. This is the byte-for-byte contract the
  falsifier asserts. **No `arg_shape`, no `operation_id`, no counts** cross the wire — the
  richer correlation stays LOCAL (ai-ml reads local rows; §4). The egress surface is the
  minimum that seeds another machine's `known_classes` watch-list — nothing more.
- `v` is the body schema version (int); bump only on a breaking change.

**Receiver-side enforcement (the endpoint is an allowlist, mirroring `events.py`):**

1. Reject any item key not in `{error_class, surface_id, surface_rev}` (fail closed).
2. `error_class` must be a member of `corpus.ERROR_CLASSES` (fail closed — a free-text class
   could otherwise smuggle a value out; the identical guard `assert_fields_allowlisted` runs).
3. `surface_id` passes `_safe_surface_id` shaping (host-only, secret-shaped → hashed); a URL
   with creds cannot survive it.
4. `surface_rev` matches an opaque-hex shape (`^[a-f0-9]{8,64}$`), else drop the item.
5. **Size cap:** ≤ **500 items/batch** and ≤ **64 KB** body; over-cap → 413, batch dropped
   (never truncated-then-stored — a mid-truncation could split a value).

### 2.5 Retrieval (storage contract only; ranking is the ai-ml seam)

- **Cross-request, rev-scoped read.** Given `(surface_id, surface_rev)`: load `index.json` for
  the fast priors (per-class counts, `fcc_rate`); fall back to scanning `outcomes.jsonl` if the
  index is stale/absent. `reported.jsonl`/`synthetic.jsonl` are read ONLY by a caller that
  explicitly asks (segregation-by-path = fail-closed for naive readers).
- **What retrieval returns:** closed-set classes + counts + `fcc_rate` + `surface_rev`
  provenance. HOW those become a ranked "call it like this" hint — and whether vectors are ever
  warranted over this store — is the `ai-ml-engineer` seam (§4). Per operating principle #3, no
  vectors until evidence gates it; JSONL + a built `index.json` is correct at current scale.

### 2.6 The flywheel — a class learned on API#1, watched on API#2

The read-path already exists: `preflight_corpus.known_classes_from_corpus()` returns the
distinct classes every prior run on every surface produced, and Preflight UNIONS it with the
seed so a class first seen on API #1 is watched on API #2. This spec extends it from Preflight
classes to call-outcome classes and PERSISTS the rollup:

- `_classes.json` is the persisted `known_classes_from_corpus` rollup — read **across all
  `surface_id`s**, deliberately NOT rev-scoped. It is refreshed by `gecko corpus build`
  (§2.7). Cold start reads this one small file instead of re-scanning every surface's JSONL.
- **The compounding key is the CLASS, never the value** — the same reason
  `known_classes_from_corpus` only ever lets closed-vocabulary class NAMES leave the file. A
  corrupt/off-vocab line is skipped, not smuggled.

### 2.7 Retention & aggregation — `gecko corpus build`

- **Append-only JSONL is the source of truth** (structurally payload-safe: no UPDATE path that
  could accrete a payload). The index is a derived, disposable projection.
- `gecko corpus build` (design-named, not built here) folds `outcomes.jsonl` → `index.json`
  (cumulative per-class counts, `n_observed`, `fcc_rate`, `first_seen`/`last_seen`) and
  refreshes `_classes.json`. Runs periodically / on a size trigger.
- **Retention (the file-size relief valve, deferring the DB gate):** `index.json` holds the
  CUMULATIVE aggregate, so raw rows can be compacted AFTER they are folded. Policy: retain raw
  `outcomes.jsonl` to a rolling cap of **50k rows or 90 days per surface**, drop older raw rows
  once counted into the index. Counts are preserved (they live in the index); only the raw
  per-row detail ages out. This bounds file size until the surface genuinely exceeds the DB
  gate (§3).

---

## 3. The DB gate — the measurable trigger (the number)

Operating principle #3: don't introduce a DB before scale demands it; when it does, justify it.
Here is the justification and the number.

**File-based storage stops being retrievable enough at EITHER trigger, whichever fires first:**

1. **Volume trigger — ~100k observed rows in a single surface's `outcomes.jsonl`** (roughly
   30–50 MB). Below this, an index rebuild scans the raw log in well under a second and the
   rolling-retention cap (§2.7) keeps the file bounded. Above it, the rebuild and append-GC
   pressure make the file the bottleneck. (Per-surface volume is naturally bounded — a runner
   serves a handful of surfaces — so this is the SECONDARY trigger.)
2. **Breadth / cross-surface-query trigger — ~50 surfaces under `~/.gecko/corpus/` AND a query
   the file layout cannot answer cheaply.** The flywheel's growth is unbounded in surfaces, not
   rows-per-surface, so this is the **BINDING trigger.** File layout answers "for THIS surface,
   which classes/counts?" cheaply (one directory). It does NOT answer the L5 cross-surface
   aggregation ai-ml wants — *"across all surfaces, which `(error_class, arg_shape)` pairs
   co-occur and predict not-FCC?"* — without a full multi-file scan per query. When the flywheel
   spans **> ~50 surfaces** and ai-ml issues such a `GROUP BY`-across-surfaces more than at
   cold-start `_classes.json` rebuild, files stop being retrievable enough.

**Which DB: MongoDB.** Already in the stack for keys + the `gecko_events` telemetry sink + the
Atlas dense index — no new dependency, no new operational surface. Migration is mechanical
because the row schema does NOT change: the Mongo document IS the same allowlisted dict
`to_record`/`to_doc` already produce. Add a `gecko_corpus` DB with a `call_outcomes`
collection, indexed on `(surface_id, surface_rev)` (per-surface reads) and `error_class`
(cross-surface flywheel). `_classes.json` becomes a `distinct(error_class)` /
aggregation-pipeline query. The append-only JSONL stays the local/BYOD default; the DB is the
HOSTED aggregation tier, entered only past the gate.

**Headline number for the founder: migrate to MongoDB at ~50 surfaces, or ~100k rows in one
surface — whichever first.** Vectors remain a separate, later, evidence-gated flip (ai-ml's
call), not part of this gate.

---

## 4. Seam to ai-ml — rows as ground truth, no ranking here

- This spec fixes the STORAGE contract: the closed vocabularies (`ERROR_CLASSES`),
  `(surface_id, surface_rev)` keying, the `index.json` projection, `_classes.json` cross-API
  rollup, the DB gate. It does NOT decide ranking, which classes predict FCC, or whether
  vectors are ever warranted.
- ai-ml consumes these rows as ground truth for the L4/L5 semantic depth (semantic-depth spec
  §1, §3.2) and for the `fcc_eval.lift_corpus` gate — both are **corpus-gated on the rows this
  spec guarantees are clean.** The `(error_class, arg_shape)` correlation that unlocks L4
  distribution-anomaly and L5 corpus-observed-failure signals is a read over LOCAL
  `outcomes.jsonl` (or the DB past the gate) — never the minimal uploaded triple.
- Contract: the ai-ml layer may read any field on `CallOutcome` and any closed class, and may
  propose **new closed-set constants** (a reviewed append to the frozen set), but may NOT ask
  for a free-text field or a raw-query/candidate-text column.

---

## 5. Reconcile with the SurfaceNote — categorical row vs prose hint

Two artifacts, two stores, one join. Do not merge them.

| | **SurfaceNote** (context-engineer authors) | **CallOutcome row** (data-engineer stores) |
|---|---|---|
| What | one free-text `note` (~70 tokens) — the human prose HINT explaining the fix | the CATEGORICAL failure-class record — counts + shape, no prose |
| Author | context-engineer (I store/serve, NEVER author) | derived by the runner from the wire status |
| Store | `~/.gecko/notes/{surface_id}/{surface_rev}/{target}.json` | `~/.gecko/corpus/{surface_id}/outcomes.jsonl` |
| Key | `(surface_id, surface_rev, target)` | `(surface_id, surface_rev, operation_id)` |
| Payload rule | validated-at-write (secret-shaped/instruction-shaped → refused) | allowlist + closed vocab; **no `note`/free-text field exists** |

- **They join by keys** — `(surface_id, surface_rev, operation_id/target)` — at READ time. The
  corpus row *counts* an `error_class`; the note *explains the fix*. ai-ml reads both: "class
  `unprocessable_422` recurs on this op AND there is a note saying 'send the RAW request body'."
- **The note prose is NEVER copied into the categorical row.** `CallOutcome.ALLOWED_KEYS` has
  no `note` slot and must never grow one — a `note` field would fail the allowlist (a build
  break), which is the structural guarantee that the categorical corpus stays payload-free.
- **Note persistence contract (I own the bytes; context-engineer owns the schema/validation):**
  the note store is append-or-replace-whole-record (no update-in-place that accretes), keyed as
  above with filesystem key sanitization `/`→`--` and `..` rejected on read AND write, and is
  `surface_rev`-scoped so a drifted note is retained-but-flagged, never silently reused. The
  write-time secret/instruction refusal (context-contract spec §3) runs BEFORE persist — the
  store must never HOLD a secret-shaped string, only refuse it.

---

## 6. Build plan — the Pattern-B falsifier is deliverable #1

Per the shared rule, the FIRST deliverable is a free, offline, $0 falsifier that can disprove
the control-plane + transport invariants before any wire or registry work. It needs no network,
no secret, and asserts the posted body byte-for-byte.

**Phase 0 — the offline falsifier (build first).** A pure-Python test (no network, no MCP SDK,
recorded/fixture mode) that asserts, against a fixture surface + fixture statuses:

1. **Payload-free by construction:** `outcome_from` cannot receive a body — pass a fixture
   result dict positionally and assert it is a `TypeError`/rejected; the only status path is
   `status: int | None`. Grep the produced row: assert no fixture body/URL substring appears in
   any field.
2. **Source routing:** `mode="live"` → `source="observed"` → row in `outcomes.jsonl`;
   `mode="reported"` → `source="reported"` → row in `reported.jsonl` and EXCLUDED from the
   `observed`-only FCC aggregate; `mode="recorded"` → `synthetic.jsonl`. A naive reader of
   `outcomes.jsonl` sees neither reported nor synthetic (segregation-by-path fail-closed).
3. **Keying tolerates cross-request reuse:** write rows under one `(surface_id, surface_rev)`
   across two different fixture `session_id`s; assert the read-path returns BOTH (the corpus is
   not partitioned by session — operating principle #2).
4. **The exact posted body, byte-for-byte:** build a batch from fixture `observed` rows and
   assert the serialized upload body equals, exactly,
   `{"items":[{"error_class":"unprocessable_422","surface_id":"api.example.com","surface_rev":"<rev>"}],"v":1}`
   (canonical `sort_keys`, compact separators). Assert the body contains NO `arg_shape`,
   `operation_id`, `latency_ms`, or any value substring.
5. **Endpoint allowlist fails closed:** a batch item with an off-vocab `error_class`, an extra
   key, a URL-with-creds `surface_id`, or a malformed `surface_rev` is rejected/dropped; an
   over-cap batch (>500 items or >64 KB) is refused whole, never truncated.
6. **Default-nudge, not default-on:** with `--report-failures` OFF, an `observed` failure row
   is written locally AND no upload is attempted (assert the fake sink received nothing); the
   nudge line is emitted at most once.
7. **Note/row separation:** assert `CallOutcome.__dataclass_fields__` has no `note`/free-text
   key (adding one is a build break); assert the corpus write path never reads the note store.

**Phase 1 (gate: Phase-0 green).** `software-engineer` wires the runner capture hook in
`serve.py`/the call path (`outcome_from` at the status boundary → `record()` to the canonical
home) + the `/registry/feedback` route (the allowlist receiver) + the batched upload flusher.
Plumbing lane; escalate to `staff-engineer` if any item forces an engine-file change
(`ingest`/`catalog`-core/`tools`/`caller` stay untouched; `corpus.py`/`preflight_corpus.py`
are the existing seams).

**Phase 2 (gate: ai-ml `fcc_eval.lift_corpus > 0`).** The L4/L5 depth signals turn on only
after these rows exist and the harness shows they earn their keep (semantic-depth spec §1).
Storage ships before the signal; the signal is measurement-gated.

---

## 7. Seams (do not duplicate)

- **context-engineer authors the note; I store/serve it, never author.** Schema + write-time
  validation (secret/instruction refusal) are theirs; the physical note store, keying, `..`
  sanitization, `surface_rev`-scoping, and the join-by-keys-never-copy rule are mine (§5).
- **ai-ml consumes rows as ground truth** for L4/L5 depth + the `fcc_eval.lift_corpus` gate
  (§4). They own ranking/prediction and the vectors-yes/no call; I own the clean rows + the
  DB gate. They may propose new closed-set constants (reviewed append); never a free-text
  column.
- **software-engineer wires the runner capture hook + the feedback route** (§6 Phase 1). Code
  in the runner/call path + a new registry route; semantics specified here. Escalates to
  `staff-engineer` on any engine-file pressure.
- **staff-engineer owns the architecture call** — LOCKED here as observed-first (runner on-path
  primary, `reported` supplementary/quarantined, both payload-free). No proxy/observe body-read
  mode is ever required.

---

## 8. Guardrails (what NOT to do)

- Do NOT add a `note` / `comment` / `message` / `query` / `arg_value` field to `CallOutcome` or
  the feedback endpoint — there is no free-text escape hatch; a new signal gets a new
  closed-vocab constant (reviewed append) or is dropped.
- Do NOT key the corpus by `session_id` — cross-request reuse is the default (principle #2).
- Do NOT publish a corpus catalog or merge a community CDN — the corpus is LOCAL/BYOD; only the
  opt-in, classes-only triple ever egresses, `tenancy` fail-closed `local`.
- Do NOT let `reported` or `synthetic` feed the published FCC rate or the flywheel upload.
- Do NOT truncate an over-cap batch or an over-cap note — refuse whole (a mid-truncation splits
  a value).
- Do NOT introduce the DB or vectors before the §3 gate fires — JSONL + built index is right at
  this scale.
- Do NOT copy note prose into the categorical row, ever (§5).
```
