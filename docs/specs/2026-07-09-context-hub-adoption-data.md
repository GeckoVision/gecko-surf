# context-hub adoption — corpus storage & feedback-capture slice (2026-07-09)

**Lane:** `data-engineer` (the correctness corpus is stored and retrievable).
**Verdict from the eval:** COMPLEMENT, not competitor. Andrew Ng's `context-hub`
(`chub`) is a docs-distribution rail; Gecko is a call-*correctness* rail. Two of
chub's loops rhyme with our V2 moat and are worth adopting **in shape** — but every
one of them ships user text or phones an author, so each must be re-cut to fit
invariant #1 (control plane, never data plane) before a single line lands.

This spec says exactly what we take, what we reject, and what the schema/cache/
retrieval looks like. **Design only — no implementation in this doc.**

## Scope & non-goals

- **In:** the feedback-capture path (how the corpus *learns* a call outcome), the
  corpus persistence/distribution model (how it is *stored, versioned, retrieved*
  locally), and the telemetry hygiene divergence from chub.
- **Out (named, other lanes):**
  - *What signal is worth capturing to make the next call correct* → `ai-ml-engineer`
    retrieval spec. This doc fixes the **storage contract + closed vocabulary**; it
    does not decide ranking, embeddings, or which classes predict FCC. Seam named in
    §5.
  - *How corpus rows become durable agent memory / just-in-time context* →
    `context-engineer` memory spec. This doc produces the control-plane-safe rows;
    that spec decides how they are surfaced into an agent's working context. Seam
    named in §5.
  - *The direct-call-vs-capture architecture call* → `staff-engineer`. This doc
    proposes the reconciliation (§1) but does not unilaterally settle it.

## Grounding (read, not re-derived)

- **chub loops** — `cli/src/lib/annotations.js` (local derived notes),
  `cli/src/lib/telemetry.js` `sendFeedback` (up/down + free-text comment + agent/model
  → `api.aichub.org/feedback`), `cli/src/lib/analytics.js` `trackEvent` (raw query
  `≤1000` chars + result ids → PostHog cloud), and the
  `chub build → registry.json → CDN → ~/.chub/sources/<src>/{registry.json,meta.json,data/}`
  cache flow (`docs/design.md`).
- **Gecko ground truth** — `gecko/corpus.py` (`CallOutcome`, closed `ERROR_CLASSES`,
  `OutcomeSource=observed|reported|synthetic` derived from mode, `Tenancy=local|contributed`
  fail-closed-local, synthetic segregation by path), `gecko/preflight_corpus.py`
  (`PreflightRun`, closed `KNOWN_CLASSES`, `surface_rev` fingerprint,
  `known_classes_from_corpus()` = the cross-API flywheel read-path), `gecko/events.py`
  (allowlist writer, ships-silent, `hit_rank`/`source` FCC signal).
- **Just-built consumer-side capture point** — `docs/specs/2026-07-09-local-credential-resolver-design.md`
  and `docs/specs/2026-07-07-registry-local-execution-design.md`: the **local runner
  (`gecko serve`)** injects auth and fires the request at the provider directly, and
  already caches surfaces under `~/.gecko/surfaces/{name}@{rev}.json` with an opt-in
  `--report-failures` classes-only upload path.

The load-bearing observation: **we already have the corpus schema, the closed
vocabulary, and the flywheel read-path built.** context-hub does not teach us *new
storage*; it teaches us three product *shapes* and one *anti-pattern*.

---

## 1. Feedback / annotation capture → the V2 correctness corpus

### What chub does
Two distinct loops, deliberately separated in chub:
- **annotation** — `chub annotate <id> --note "..."`: a *local, derived* note the user
  writes about an entry. Stored at `~/.chub/annotations/<id>.json` as
  `{id, note, updatedAt}`. **Off by default** (nothing until an explicit write),
  **path-validated** (`entryId.replace(/\//g,'--')`, MCP id regex-gated), **never
  leaves the machine**.
- **feedback** — `chub feedback <id> up|down`: an up/down rating + optional free-text
  `comment` + `agent`/`model` → POSTed to the author's telemetry endpoint. This is the
  *author-facing quality signal*.

### What we adopt — the shape, not the payload
The two-loop split maps cleanly onto Gecko, and we already have the primitives:

| chub loop | Gecko analog (already built) | Adopt? |
|---|---|---|
| annotation (local derived note, off-by-default, path-safe) | a **local, opt-in, derived** corpus row — `corpus.record()` only writes when a `corpus_path` is passed; append-only JSONL; auditable by `grep` | **Adopt the discipline; reject free text** — see below |
| feedback (up/down → author) | the **FCC outcome** — `CallOutcome.first_call_correct` + `error_class` is the up/down, `hit_rank`/`source` is the "was the retrieved answer right" signal | **Adopt the signal; reject the author telemetry** — §3 |

**The one hard divergence: no free text.** chub's `note`/`comment` are unbounded
user strings — the exact thing invariant #1 forbids (a comment can carry a payload, an
arg value, a secret, a customer's PII). Our "annotation" is **not a string field**; it
is a **closed-vocabulary classification**. Where chub stores *what the human typed*, we
store *which class the run fell into*. That is already the `preflight_corpus`
discipline (`KNOWN_CLASSES`, fail-closed on stray text) and the `corpus`
discipline (`ERROR_CLASSES`, allowlist writer). We extend it, we do not loosen it.

So the "annotation loop" for Gecko is: **an agent- or runner-derived correction is
recorded only as a closed-set class + `surface_rev`, never as prose.** If a signal
cannot be expressed as a member of a closed vocabulary, it does not get a free-text
escape hatch — it gets a *new vocabulary constant* (an append to the frozen set,
reviewed as a code change) or it is dropped. This is the structural guarantee that a
"note" can never smuggle a value out.

### The unresolved tension — "does Gecko see call outcomes?" — reconciled
V1's clean design has the agent calling the API directly, so Gecko may never see the
outcome. The just-built **local runner is the answer** and this spec commits to it as
the **primary capture point**:

- **The runner is on-path by construction.** `gecko serve` builds the `PreparedRequest`
  from surface metadata, injects auth via `ResolvedSession.auth_headers()`, and
  `caller.execute()` fires at the provider. The **HTTP status comes back through the
  runner** before the agent ever sees the body. The runner can therefore call
  `corpus.outcome_from(status=..., ...)` with `mode="live"` → `source="observed"` — a
  real, published-eligible FCC row — **without ever touching the response body** (recall
  `outcome_from` takes `status: int | None`, never the result dict; the body physically
  cannot enter).
- **This is the capture point, and it is control-plane-clean by architecture, not
  policy.** Same argument the credential-resolver spec makes for the key: the value
  (there, the secret; here, the payload) lives in runner process memory for the call and
  is never persisted, because the *only* function that writes the corpus cannot receive
  it.
- **Three capture modes, already modeled by `OutcomeSource`** — this is the reconciliation
  of the whole feedback-capture decision:
  1. **observed** (`mode="live"` through the runner) — the strong signal; the runner saw
     the wire status. *This is what the local-runner architecture unlocks and why it is
     the right V2 bet.*
  2. **reported** (`mode="reported"`) — the agent calls the API itself (not through the
     runner) and *reports* the outcome class back. Weaker (self-reported), so it is a
     **distinct `source`** and is **excluded from the published FCC rate** by the same
     routing `corpus.record()` already does. This is the escape valve for the
     direct-call design without corrupting the metric.
  3. **synthetic** (`mode="recorded"`, validator) — fabricated 200; **segregated to
     `synthetic.jsonl`** by path so a naive reader fails closed. Already built.

  We do **not** need a proxy/observe mode that reads bodies. The runner-as-capture-point
  gives us `observed` rows for free, and `reported` covers the direct-call remainder —
  both payload-free. **Escalate to `staff-engineer` only the choice of whether V2
  *requires* the runner on-path (observed-first) or treats `reported` as co-equal.**

### The schema — reuse, do not fork
No new record type. The V2 corpus **is** `corpus.CallOutcome`, already control-plane
proven:

- keys: `ts, surface_id, surface_rev, operation_id, method, path_template (templated,
  never filled), params_present (NAMES), arg_shape (name→JSON-type), body_present (bool),
  status, ok, error_class (closed set), first_call_correct, attempt, latency_ms, mode,
  auth_injected (bool), source (derived), tenancy (fail-closed local)`.
- writer guarantees: `ALLOWED_KEYS` allowlist (fail closed on any new key),
  `ERROR_CLASSES` closed set (fail closed on stray class), append-only JSONL, `source`
  derived from `mode` (a caller can never mislabel a synthetic 200 as observed).

**The single additive change this spec proposes** (design, not code): the corpus today
lives wherever a caller passes `corpus_path`. To make it a *persistent, retrievable,
cross-request* store (§2), give it a **canonical default home and a stable layout** —
that is the only gap. Everything else is already correct.

### How it compounds across APIs
The flywheel read-path already exists and is the thing to lean on:
`preflight_corpus.known_classes_from_corpus()` returns the distinct failure classes
**every prior run on every surface** produced, and Preflight *unions* it with the seed
so a class first seen on API #1 is watched on API #2. The V2 corpus extends this from
Preflight classes to **call-outcome classes**: an `error_class` pattern that recurs on
surface A (e.g. `unprocessable_422` correlated with a given `arg_shape`) becomes a
cross-API prior. **Crucially, the compounding key is the CLASS, never the value** — the
same reason `known_classes_from_corpus` only ever lets class names leave the file. The
*ranking/prediction* on top of these priors is the `ai-ml-engineer` seam (§5).

---

## 2. build → registry → local-cache as the corpus persistence & distribution model

### What chub does
`chub build` walks a content tree → emits `registry.json` (a versioned index:
`{version, base_url, generated, docs[], skills[]}`, each entry carrying
`path/files/size/lastUpdated` and per-version nesting) + copies the content tree → the
whole `dist/` is uploaded to a **public CDN**. The CLI fetches `registry.json` first
(cheap index), caches per-source at `~/.chub/sources/<src>/{registry.json, meta.json
(lastUpdated+hash), data/}`, and fetches entry bodies on demand. Multi-source merges a
public CDN with local folders; a `source` trust field (`official|maintainer|community`)
plus `~/.chub/config.yaml` filtering gates what an agent sees.

### What we TAKE
1. **Index-first, versioned-index shape.** A cheap top-level index that is fetched/read
   before any body, and that is **explicitly versioned**. chub versions by
   `generated` + per-entry `lastUpdated`; **we version by `surface_rev`** — which we
   already stamp on every `CallOutcome` and `PreflightRun`. The corpus index is keyed
   `(surface_id, surface_rev)` so a corpus built against surface rev N is never silently
   read as valid for rev N+1 (a drifted surface must not reuse stale correctness priors).
   This is the direct analog of chub's `registry.json` version field, cut to our unit.
2. **Local per-source cache layout, reusing the home the runner already owns.** The
   registry-local-execution spec already caches surfaces at
   `~/.gecko/surfaces/{name}@{rev}.json`. The corpus gets a sibling:

   ```
   ~/.gecko/
     surfaces/{name}@{rev}.json         # already exists — the surface (control plane)
     corpus/
       {surface_id}/
         outcomes.jsonl                 # observed + reported rows (published-eligible)
         synthetic.jsonl                # segregated synthetic rows (already the pattern)
         index.json                     # cheap index: per (surface_rev) → {counts by
                                         #   error_class, first_seen, last_seen, n_observed}
       _classes.json                    # cross-API known-classes rollup (flywheel cache;
                                         #   the persisted form of known_classes_from_corpus)
   ```

   `outcomes.jsonl`/`synthetic.jsonl` are exactly today's `corpus.record()` targets,
   relocated to a canonical home. `index.json` is the **built artifact** — the analog of
   `chub build`'s `registry.json`: a periodically-regenerated rollup so retrieval reads a
   small index, not a growing JSONL, at query time. `_classes.json` persists the
   flywheel rollup so a cold start does not have to re-scan every surface's JSONL.
3. **A `gecko corpus build` step (design-named, not built here).** The analog of
   `chub build`: fold the append-only `outcomes.jsonl` into the small `index.json` +
   refresh `_classes.json`. Append-only stays the source of truth (structurally
   payload-safe, no UPDATE path); the index is a derived, disposable projection. This
   keeps the fast read-path off the raw log.

### What we REJECT (hard invariants — do not soften)
1. **No public catalog. Ever.** chub's whole point is a public CDN of shared content.
   Our corpus is **LOCAL / BYOD only.** This matches memory `repo-public-structure`
   ("no public catalog — a discipline") and `context-two-products-who-pays`. The corpus
   index is **not** uploaded, **not** merged from a community CDN, **not** discoverable.
   `Tenancy` stays fail-closed `local`; the `contributed` egress path is *named but not
   built* (as `corpus.py` already does), and if it is ever built it is a **separate,
   consented, classes-only** upload — never chub's "publish the tree" model.
2. **No author-feedback telemetry.** chub's `sendFeedback` phones the *content author*
   with an up/down + free-text comment. Gecko has **no author to phone** (we ingest
   APIs unilaterally; the provider is not in our loop) and **no free-text channel**. The
   FCC outcome stays a local corpus row. The only thing that may ever leave the machine
   is the existing **classes-only, opt-in `--report-failures`** upload
   (`preflight_corpus` vocabulary, signed by the Gecko key) — and even that is metadata
   to *us*, never to the provider.
3. **No content-body distribution.** chub distributes doc *bodies* on demand. Our corpus
   distributes **nothing**; retrieval is local-only. (Surface *definitions* are
   distributed by the registry spec — that is control-plane metadata and a separate
   concern; the *corpus* of outcomes never is.)

### The per-request-scoping trap (operating principle #2) — designed against
A static, reused corpus **must not be gated behind a request/session id.** The
`~/.gecko/corpus/{surface_id}/` layout is keyed by **`surface_id` + `surface_rev`,
never by `session_id`.** `session_id` exists in `events.py` purely as a connect↔call
*correlation* token for telemetry; it is **not** a corpus partition key and must never
become one. Retrieval (below) reads the whole per-surface store regardless of which
request is asking — cross-request reuse is the default, exactly as the principle
demands. (The trap is real precisely because `events.py` *does* carry a `session_id`;
this spec states explicitly that the corpus store does not inherit it.)

### Retrieval (storage contract only; ranking is the ai-ml seam)
- **Read-path is cross-request and rev-scoped:** given `(surface_id, surface_rev)`, load
  `index.json` for the fast priors (per-class counts, FCC rate); fall back to scanning
  `outcomes.jsonl` if the index is stale/absent. `synthetic.jsonl` is only read by a
  caller that *asks* for it (segregation-by-path = fail-closed for naive readers).
- **Cross-API priors:** `_classes.json` (the persisted `known_classes_from_corpus`
  rollup) is read **across all `surface_id`s** — this is the flywheel and it is
  deliberately *not* rev-scoped (a class learned anywhere is watchable everywhere).
- **What retrieval returns:** closed-set classes + counts + `surface_rev` provenance.
  **How those become a ranked "call it like this" hint, and whether we ever need vectors
  over this store, is the `ai-ml-engineer` retrieval seam** (§5). Per operating principle
  #3 and memory `agent-native-surface-design`, **no vectors until evidence gates it** —
  JSONL + a built `index.json` is correct at current scale.

---

## 3. Telemetry watch-out — how our capture diverges from chub's

chub ships two things Gecko must never ship:

| chub emits | to | our divergence |
|---|---|---|
| `trackEvent('search', {query: query.slice(0,1000), results:[ids], query_length, ...})` — **raw user query text (≤1000 chars)** + platform + persistent `clientId` → **PostHog cloud** | third-party analytics | **We never emit query text.** `events.py` emits `surf.search` with `k` (breadth) and `hit_rank` — **counts and ranks, never the query string.** The agent's intent text is exactly the free-text/payload channel invariant #1 closes. There is no `query` field in `ALLOWED_FIELDS` and there must never be one. |
| `sendFeedback(..., {comment, agent, model})` — **free-text comment + agent name + model** → author API | provider/author telemetry | **We never emit free text, and never phone a provider.** The FCC signal is a **closed `error_class`** on a local row. `client`/`user_agent` in `events.py` are the only externally-derived strings and they are **sanitized + capped + neutralized** (`_safe_client`/`_safe_user_agent`: strip control chars, cap, redact secret-shaped/injection to `"redacted"`), never fail-open verbatim like chub's `agent`/`model`. |
| persistent `clientId` (PostHog `distinctId`) correlating every command | cloud | our `surface_id` is reduced to a **cred-free opaque token** (`_safe_surface_id`: host-only, secret-shaped → hash); `session_id` is a **per-connect correlation token**, hashed if secret-shaped, and — per §2 — **never a corpus key.** |

**The structural guarantees that make our divergence enforced, not promised** (already
built, restated so this spec is self-contained):
- **Ships silent** — `events.py` is a no-op unless `MONGODB_URI` is set (our hosted
  surface only); a third-party OSS install never phones home. `GECKO_TELEMETRY=off`
  hard-disables. chub defaults telemetry *on* (opt-out); we default *off* (opt-in by
  configured sink). This is the correct default for a control-plane-clean tool.
- **Allowlist writer** — `emit_surf_event` accepts only `ALLOWED_FIELDS`; a stray
  `query`/`comment`/`args`/`body` key fails closed (`TelemetryError`) in CI, not
  in prod.
- **Closed value channels** — every value-bearing field is either a closed-set member
  (`error_class`, `source`, `client_kind`) or a short non-secret-shaped label
  (`tool_name`, `mode`, `tier`), length- and secret-shape-checked (`_is_safe_label`).
- **Redact-before-raise** — an emit error is logged as `"(redacted)"`; the record is
  never echoed. Same discipline the credential-resolver leak suite enforces with a
  sentinel secret — **extend that sentinel leak test to assert no query text and no
  free-text comment ever appears in any emitted event or corpus row.**

**Concrete requirement for whoever wires the search feedback signal:** capturing
"was the retrieved endpoint the right one" must be done via `hit_rank` (an int) +
`ok`/`error_class` on the *subsequent* call — **never** by storing the query or the
candidate list text. That keeps the retrieval-quality signal control-plane-clean while
still feeding the `ai-ml-engineer` ranking loop.

---

## 4. Seams to the parallel specs (named, not duplicated)

- **`ai-ml-engineer` — retrieval spec.** This doc fixes the **storage contract**: the
  closed vocabularies (`ERROR_CLASSES`, `KNOWN_CLASSES`), the `(surface_id, surface_rev)`
  key, the `index.json` projection, and the `_classes.json` cross-API rollup. It does
  **not** decide *ranking, which classes predict FCC, or whether vectors are ever
  warranted over this store*. Those consume the rows this doc guarantees are clean.
  Contract between us: the ai-ml layer may read any field on `CallOutcome` and any
  closed class, and may propose **new closed-set constants** (a reviewed append), but
  may **not** ask for a free-text field or a raw-query/candidate-text column.
- **`context-engineer` — memory spec.** This doc produces control-plane-safe corpus
  rows + the local cache. That spec decides how those rows become **durable, just-in-time
  agent memory** (per memory `context-engineering-anthropic`: retrieval-default, "patterns
  not payloads"). Contract: the memory layer surfaces **classes + priors**, never the
  raw JSONL and never a body — the "patterns not payloads" rule is satisfied *because*
  the corpus physically contains no payloads.
- **`staff-engineer` — architecture.** Owns the observed-first vs reported-co-equal call
  in §1 (does V2 *require* the runner on-path?). This doc recommends observed-first with
  reported as the direct-call escape valve, both payload-free.

## 5. Summary (for the founder)

context-hub is a **docs-distribution** tool; Gecko is a **call-correctness** tool. Its
loops are worth copying *in shape* but every one ships user text or phones an author,
so each is re-cut to our control-plane invariant before it lands. The good news: we
already built the hard parts — the corpus schema (`corpus.py`), the closed vocabularies,
and the cross-API flywheel read-path (`preflight_corpus.known_classes_from_corpus`). The
gaps context-hub exposes are small and mechanical: a **canonical local home** for the
corpus, a **built index keyed by `surface_rev`**, and a **persisted flywheel rollup**.
Critically, the just-built **local runner (`gecko serve`) is the feedback-capture point
we were missing** — it sees the wire *status* (never the body), which turns the "does
Gecko even see outcomes?" tension into a solved, payload-free `observed` capture path.
Nothing here requires a DB or vectors yet; JSONL + a built index is right at this scale.

### Top 3 decisions for the founder
1. **Commit the local runner as the primary feedback-capture point (observed-first).**
   It gives payload-free `observed` FCC rows by architecture; `reported` covers the
   direct-call remainder without polluting the published metric. → confirm with
   `staff-engineer` whether V2 *requires* the runner on-path or treats `reported` as
   co-equal.
2. **Adopt chub's index+cache rail, reject its public CDN.** Take the versioned-index +
   local per-source cache (keyed by `surface_rev`, homed at `~/.gecko/corpus/`); reject
   the public catalog and the author-feedback telemetry outright. Corpus stays LOCAL/BYOD,
   `Tenancy` fail-closed `local`.
3. **Hold the line on no-free-text, no-query-text telemetry.** chub ships raw queries to
   PostHog and free-text comments to authors; we ship **closed classes + counts + ranks
   only**, ship-silent by default. Extend the sentinel leak test to assert no query/comment
   text ever reaches an event or corpus row.

## Relevant paths
- This spec: `/home/nan/PycharmProjects/Gecko/surfcall/docs/specs/2026-07-09-context-hub-adoption-data.md`
- Corpus schema + vocab: `/home/nan/PycharmProjects/Gecko/surfcall/gecko/corpus.py`
- Flywheel read-path: `/home/nan/PycharmProjects/Gecko/surfcall/gecko/preflight_corpus.py`
- Telemetry allowlist / ships-silent: `/home/nan/PycharmProjects/Gecko/surfcall/gecko/events.py`
- Capture point (local runner + resolver): `/home/nan/PycharmProjects/Gecko/surfcall/docs/specs/2026-07-09-local-credential-resolver-design.md`, `/home/nan/PycharmProjects/Gecko/surfcall/docs/specs/2026-07-07-registry-local-execution-design.md`
