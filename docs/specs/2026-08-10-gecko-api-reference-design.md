# The Gecko API Reference — design

**Date:** 2026-08-10
**Status:** design, approved in outline (approach A), pending review
**One line:** Graphify for APIs, self-hosted — the provider publishes their API as the graph an agent actually traverses, with the derived plan beside it and measured proof that the plan lands.

---

## 1. Why this exists

A provider's OpenAPI page answers *"what endpoints exist."* It does not answer the
question an agent has to solve: **given an intent, which call comes first, what does it
feed, and will it work the first time.** Gecko already derives that graph. It is invisible
outside our process.

Arazzo's "see it in action" is the shape of the answer — a workflow document beside a
rendered, step-by-step diagram. We can render something strictly richer, because our graph
is *derived from the spec* rather than hand-authored, carries *provenance* per edge, and
can carry *measured proof* that the plan executes.

**Distribution is the point.** The provider generates and hosts it. If we host it, it
reads as us holding something; if they can run one command and publish it, adoption is
self-serve and every published page is a Gecko artifact in the wild with their name on it.

---

## 2. What testing found (this design is shaped by it, not by hope)

Run against real surfaces before writing this. Findings, in order of impact:

### F1 — Arazzo 1.0 cannot express our most common chain. **(load-bearing)**

Of 7 multi-step chains derived from `pegana_openapi.json` starting from `{symbol}`, only
**2 export as executable Arazzo**. The other 5 refuse, all with the same cause:

> `unresolved-output-arity` — "the value lives inside a collection and Arazzo 1.0 has no
> `for-each` and no which-one expression, so no correct pointer exists; emitting `/0/`
> would bind whichever element sorted first and call it verified"

The refusal is **correct** and is ours by design (R3/R5, `e82bd8b`). But the pattern it
refuses — *list things, then act on one of them* — is the most common real chain there is.

The sharp part: **the canonical Arazzo pet-store example does exactly what we refuse.** It
binds `petId: $response.body.data.pets[0].id` — takes the first element and proceeds. So
the honest framing of our own reference page is not "we do Arazzo too." It is:

> Our graph knows this chain. Arazzo 1.0 cannot express it correctly, so we refuse to
> emit a pointer that would bind an arbitrary element and call it verified — and we show
> you the chain anyway.

**Design consequence:** Arazzo is a *pane*, not the model. The page renders from our own
view model; the Arazzo document is an export shown beside it, with refusals rendered as
first-class content rather than an empty box.

### F2 — `to_arazzo()` refuses silently without `graphs=`

Called without the graphs it derived over, it returns a document with **zero workflows**
and no exception. A naive integration renders an empty plan pane and looks broken. Callers
must check `is_executable()`. The renderer will treat "not executable" as a content state
with its refusal reasons displayed, never as an empty pane.

### F3 — `surfaceviz.graph_data()` is not the contract a renderer wants

It returns keys `['edges', 'operations', 'summary']` — `operations`, not `nodes`. In one
probe it returned 0 operations alongside 30 edges. It is shaped for the SVG it feeds, not
for reuse. The new view model will not consume it directly.

### F4 — `tests/fixtures/petstore_openapi.json` is invalid JSON and nothing loads it

Trailing comma after `info.description` (line 6). `test_arazzo.py` uses *inline* petstore
specs, so nothing ever parses the file and nothing caught it. This is the canonical Arazzo
example surface — precisely the one we want for an Arazzo-style comparison. **Fix it and
give it a test that loads it.**

### F5 — the program surface is the stronger demo, and R7 is visible in it

`find_start("swap out of hyUSD")` returns 5 ranked starts; the top one carries a 6-step
derive plan with per-account provenance:

```
lb_pair          recovered   the helper-seeded ROOT the IDL drops (#4057)
reserve          recovered   origin (packaged config): derived from caller-supplied source
oracle           extracted
bin_array        recovered   remaining_accounts the IDL never names
event_authority  extracted
user_token       recovered   an ATA of the user under the SPL Associated Token program
```

That is a better story than any REST chain we have: the gap is concrete, the recovery is
attributable, and R7's config-origin note now surfaces in the plan.

### F6 — the program surface needs the `solana` extra

`find_start` raises `ConfigError: constant pubkey seed needs the 'solana' extra`. The
error is good. The renderer must degrade honestly when the extra is absent rather than
render a program page with holes.

### F7 — `mypy gecko` is not clean on a bare `uv sync`

4 `import-not-found` errors (`jwt`, `anyio`). CI is green only because it runs
`uv sync --extra serve` (`ci.yml:52`). Unrelated to this feature but it will bite the
first external contributor who tries to build the reference.

---

## 3. Architecture

```
SurfaceGraph + Plan ──────project_http()──────┐
                                               ├──▶ ReferenceModel ──▶ render_html()
ProgramGraph + StartPoint + Receipt ───────────┘      (frozen)
        └─────────────project_program()
```

A new `gecko/reference.py` owns a **surface-agnostic view model**. Two thin projectors
feed it. The renderer knows only the model — never `SurfaceGraph`, never `ProgramGraph`.
That boundary is what stops one renderer becoming two the first time the program surface
needs something HTTP does not have.

### The model

| Field | Holds |
|---|---|
| `surface` | id, title, kind (`http` \| `program`), generated-at, gecko version |
| `nodes` | id, label, kind, badges |
| `edges` | from, to, join label (field name / seed name), provenance, confidence |
| `flows` | ordered steps: node ref, inputs, outputs, per-step provenance, gaps |
| `plan_export` | the Arazzo document **and** its refusals — both rendered |
| `proof` | optional measured receipt per flow (status, units, network, date) |
| `gaps` | honest absences, never omitted |

Projectors are independently testable: given a graph, assert the model. That is where all
surface-specific knowledge lives.

### Reuse

`surfaceviz.render_svg` for the graph image, `arazzo.to_arazzo` (with `graphs=` — see F2)
for the plan pane, `surfacereport.build_report` for the spine. `graph_data()` is **not**
reused (F3).

---

## 4. The page

Three panes.

**Graph.** Nodes and edges, arrows coloured by provenance so the trust ladder is visible
at a glance. Clicking a node reveals its derived call and per-field provenance.

**Plan.** The Arazzo-style step list — `stepId`, `operationId`, parameters, outputs. When
Arazzo cannot express the chain (F1), the pane says so in the refusal's own words and
still renders our steps. This is a feature of the page, not an error state.

**Proof.** The before/after: naive path fails, Gecko's plan passes, with CU, network and
date, stamped `MEASURED`. A surface with no measured run renders *"not yet measured"* and
the command to produce one — never a plausible number.

---

## 5. Honesty rules, enforced structurally

- Every edge carries its provenance class; every number carries `MEASURED`, its network,
  and its date.
- Gaps render. A flagged account or an unsatisfiable input appears as a gap — a reference
  that hides what it does not know is the failure mode this repo has spent the week fixing.
- Arazzo refusals render, with their reason.
- **Control plane only.** Operation ids, parameter *names*, join keys, provenance,
  categorical receipt fields. Never a payload, value, key, or filled URL. Enforced the way
  `corpus.outcome_from` is: the model's builders take structured inputs and expose no free
  `content` field a response body could enter through.

---

## 6. Determinism

Same surface in → byte-identical page out. Everything sorted; the only timestamp is the
explicitly-labelled measurement date. Inline CSS/JS, no CDN, no external fonts — same
class as the Scorecard, `surfaceviz`, and the Skill-Guard artifacts.

---

## 7. Serving and generation

- `gecko reference <surface> --out ref.html` — the frozen page, for any static host.
- `gecko serve <surface>` gains `/reference` — rendered from the live comprehension.

Same renderer, so the published file and the served page cannot disagree. The static file
ships **curated flows** rather than a free-text intent box; the live intent box exists
only under `serve`, where our Python router runs. We do not port the router to JavaScript,
so the page can never rank an intent differently than the engine does.

---

## 8. Scope

**In:** the view model, both projectors, the renderer, the two entry points, the
`petstore` fixture fix (F4).

**Out, to their own specs:** the measured-proof *pipeline* (how receipts are produced and
attached); the video and the Orquestra delivery. This spec ends at: the page renders, from
either surface, with proof when a receipt exists.

---

## 9. Testing

- Projector unit tests: graph in → model out, per surface kind.
- Determinism: same input twice → identical bytes.
- Control plane: a canary test asserting no value/payload/secret can reach the model —
  mirroring the existing corpus control-plane test.
- Arazzo refusal rendering: a chain that refuses (F1) must render its steps *and* its
  refusal, never an empty pane.
- Degradation: program surface without the `solana` extra renders an honest unavailable
  state (F6).
- The fixed `petstore_openapi.json` gets a test that actually loads it.

---

## 10. Open questions for the founder

1. **Naming.** "Gecko API Reference" vs something that says graph. The page's own claim is
   closer to *"the call graph an agent traverses"* than to *"reference."*
2. **Does the page name the provider or Gecko first?** Distribution argues for their brand
   with a Gecko footer; attribution argues the reverse.
