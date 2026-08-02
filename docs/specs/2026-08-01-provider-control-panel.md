# Provider Control Panel — spec + strategy

**Date:** 2026-08-01
**Status:** Design (spec + strategy). Supersedes the ad-hoc "provider delivery"
direction in `docs/provider-delivery.md` by giving it a config-driven backbone.
**Owners:** staff-engineer (product spec), business-manager (positioning/GTM —
pricing + named-competitor detail live in `private/`, not here).

> **Scope note.** This is the provider-facing product. It does **not** change the
> consumer/dev path (`gecko add/serve/connect`). It defines what a *provider*
> understands, configures, controls, and measures — and the config that makes
> those four things one object instead of four features.

---

## 0. The one-line promise to a provider

> **Agents are already calling your API and silently misfiring. Gecko makes them
> first-call-correct, proves it, and shows you what that's worth — without ever
> touching your data.**

Everything below is in service of a provider believing that sentence and being
able to act on it.

---

# Part A — Strategy: what a provider must understand

## A1. Why the terminal isn't enough (the buyer-legibility gap)

Our proof lives in a terminal: `gecko report`, a scorecard, a call graph, a
red-team pass. That is the right proof surface for the *engineer* who evaluates
us. It is the **wrong** surface for the *person who signs* — the DX owner, the
product lead, the exec — who is visual and does not read a JSON sidecar.

**The Control Panel is not a nicety; it is the legibility layer.** Same facts we
already compute, rendered so a non-terminal buyer can see the advantage.

- Terminal = **proof for the builder**.
- Control Panel = **comprehension for the buyer**.

This is the same "better envelope" lesson from the Agent-Surface Report work:
the compression/correctness numbers are the differentiator; the envelope is what
lets a buyer feel them.

## A2. The three things only Gecko shows

Grounded in the competitive research (2026-08-01). Comparable products fall into
two camps, and neither does what we do:

1. **Protocol-presence readiness scores** (CDN/edge vendors): "do you publish an
   llms.txt / MCP card / API catalog?" — a *discoverability checklist*, not
   behavior.
2. **Raw-traffic MCP analytics** (observability vendors): "are your tool calls
   erroring / how slow?" — *traffic health*, blind to whether the call was
   *correct against your contract*.

**Nobody scores correctness against the API's real semantics, nobody pairs it
with threats-neutralized, and nobody translates wrong agent calls into a dollar
figure.** That triad is Gecko's white space:

- **Correctness** — did the agent's call match *your* contract, first try
  (not "did it 200")? This is the comprehension layer as a measurable output.
- **Trust boundary** — how many poisoned/injection vectors on your surface we
  neutralized *before* an agent ever saw a tool.
- **Revenue frame** — what your own failure rate, at your own price, plausibly
  costs you.

## A3. The trust promise *is* a feature, not a disclaimer

The control-plane invariant ("Gecko stores the API surface + tool defs +
correctness metadata, **never** response payloads, user data, or secrets") is
usually stated defensively. Reframe it as the **reason a provider can turn us on
unilaterally**:

> "We never see your data, so onboarding is a spec URL, not a security review."

This is a differentiator against anything that proxies traffic or ingests logs.
It must survive contact with every panel in Part B (see §B6, the one-way door).

## A4. Paid-model framing (detail in `private/`)

- Developers **never** pay — they are the wedge; their measured usage is the
  asset a provider pays to see and protect.
- Revenue is **provider-side, flat per-API, never a take-rate.** We are not a
  payment rail and not a marketplace.
- **Willingness-to-pay is still the unvalidated thesis decider.** Nothing in this
  spec assumes it is answered; §C3 defines the cheapest probe.
- Concrete pricing hypotheses, GTM sequencing, and named-competitor positioning
  live in `private/` (business-manager), never in this committed doc.

---

# Part B — Product spec: configure · control · follow

## B0. The backbone: everything is one config object

The current provider surface is **hand-built per API** (e.g.
`gecko/providers/meteora.py` hardcodes recipes; `gecko/providers/cli.py`
hardcodes a `PROGRAMS` registry). Two config files already exist as *dead
config nothing reads* — `providers/orquestra/provider.json` and
`providers/orquestra/meteora/program.json`. **This spec resurrects and
generalizes them into the product backbone.**

The Control Panel is a **thin editor over a declarative config**. "Configure your
API," "control what you already have," and "set what gets measured" are all the
same act: editing this object. Nothing in the panel is imperative; the panel
writes config, the engine reads it.

### B0.1 Config schema (normative shape, not final field list)

Two tiers — a provider (tenant) and its APIs. A Solana program is just an API of
`kind: "program"` carrying PDA recipes, so the program path and the REST path
share one loader.

```jsonc
// provider.json  — one per provider/tenant
{
  "provider_id": "acme",
  "display_name": "Acme Data",
  "apis": ["acme-core", "acme-payments"]
  // billing tier is informational only; no secrets, no keys
}
```

```jsonc
// <api_id>.json  — "control what you already have"
{
  "api_id": "acme-core",
  "kind": "openapi",                 // openapi | docs | program
  "spec_source": { "type": "url", "value": "https://acme.dev/openapi.json" },
  "spec_rev": "2026-08-01T…",        // set by ingest; drives drift

  "visibility": {                    // which ops agents may see
    "default": "exposed",
    "operations": { "deleteAccount": "hidden" }
  },

  "auth": {                          // injected at call time, hidden from agent
    "scheme": "bearer",
    "account_ref": "keyring:acme-core",   // a POINTER, never the secret
    "injected": true
  },

  "pricing_hints": {                 // provider-supplied → feeds ESTIMATED only
    "default": { "price": 0.02, "currency": "USD", "unit": "call" },
    "operations": { "bulkExport": { "price": 0.50 } }
  },

  "quarantine": {                    // provider-managed anti-poison allowlist
    "allow": ["createTransfer"]      // approve an op we flagged as a false positive
  },

  "drift_watch": {
    "enabled": true,
    "cadence": "on_spec_change",     // on_spec_change | daily | weekly
    "notify": { "type": "webhook", "target_ref": "keyring:acme-drift-hook" }
  },

  "metrics": {                       // the governance seam, §B6
    "measured": "local_aggregator",  // local_aggregator | off
    "consent": false                 // must be explicit to leave `local`
  }
}

// program APIs additionally carry the recovered on-chain graph:
// "program": { "program_id": "LBUZ…", "pdas": { "lb_pair": [ …seed nodes… ] } }
```

**Invariants the schema enforces by shape:**
- `auth.account_ref` and `notify.target_ref` are **pointers into the credential
  store**, never inline secrets. A config file is safe to commit/share.
- `pricing_hints` are the provider's own numbers; they only ever feed an
  **ESTIMATED** figure and are visually segregated from measured counts.
- `metrics.measured` defaults conservatively; leaving `local` requires
  `consent: true` (see §B6).

### B0.2 What must be built (engine)

- A `ProviderConfig` / `ApiConfig` loader (pydantic; single-source-of-truth
  types) that reads the JSON above. *Today no code reads these files — this is
  the first build.*
- The existing hand-built surfaces (`providers/meteora.py`, the `PROGRAMS`
  registry) become **config instances**, not code. Adding an API becomes
  "write/POST a config," not "edit `gecko/providers/`."
- The loader feeds the already-shipped comprehension path (`ingest → tools →
  caller`) and the already-shipped `report.py` scorecard. **We are wiring
  existing engines to a config front door, not building new comprehension.**

## B1. Onboard — register an API

Provider gives a `spec_source` (OpenAPI URL, docs URL, or inline). Engine
comprehends it (existing path) and writes the initial `<api_id>.json` with sane
defaults (all ops exposed, no pricing, drift on-change, metrics off). This is the
day-zero state; the scorecard renders immediately from the spec alone.

## B2. Configure — the levers a provider controls

Each maps to a config field in B0.1 and to an existing engine capability:

| Lever | Config field | Backed by |
|---|---|---|
| Expose/hide operations to agents | `visibility` | tool projection |
| Wire auth (injected, hidden) | `auth` | `gecko/access.py` seam |
| Pricing hints (for the estimate) | `pricing_hints` | new; provider input only |
| **Approve a false-positive quarantine** | `quarantine.allow` | `gecko/sanitize.py` |
| Drift cadence + notify target | `drift_watch` | `report_diff` (built) |
| Opt into measured metrics | `metrics` | `telemetry.aggregate` (built, disabled) |

The `quarantine.allow` lever is doubly important: it is *both* a control and the
**fix for the anti-poison false-positive pain** (a provider whose real op was
over-quarantined approves it here instead of filing a bug).

## B3. Control what they already have — surface management

Read/manage the comprehended surface:
- View the generated tools + the call graph (existing `report.py` /
  `surfaceviz.py`).
- See revisions (`spec_rev` history) and what changed between them
  (`report_diff` / `render_diff`).
- Re-ingest on demand.
- See which ops are exposed/hidden/quarantined at a glance.

No new capture — this is a view over surface + correctness metadata we already
hold.

## B4. Follow the metrics — the console panels

Every number wears a **provenance chip**; the chip is load-bearing, not
decoration:

- **PREDICTED** — from the spec alone. Always present (day-zero).
- **MEASURED** — from real corpus calls. May be empty → show "connect the
  add-command," **never "$0 lost."**
- **ESTIMATED** — MEASURED counts × provider `pricing_hints`. Always a **range**,
  formula exposed on hover.

Panels, priority order (from the panel synthesis):

1. **Agent-Readiness verdict (HERO).** Grade A–F + score, first-call-correct rate
   beside it. *PREDICTED, always; MEASURED overlay when calls exist.* Hero
   because it is the only number true on day zero and it *sharpens* (not changes
   shape) as data arrives. **Never lead with the dollar.**
2. **Revenue at risk.** Failed first-calls (bold, MEASURED) over an estimated
   $/mo **range** (light, ESTIMATED). No pricing hint → show the count only, no
   dollar.
3. **Where agents win vs stumble.** Ranked endpoints + the one-line fix per
   failure. *PREDICTED findings + MEASURED overlay.*
4. **Trust boundary.** Poisoned-surface events **neutralized at comprehension**
   + battle-test posture (e.g. "8/8 attack scenarios caught, 0 benign
   false-flags"). *See §B5 — the framing is non-negotiable.*
5. **Drift-watch.** Timeline of revisions + score delta ("v4 broke 3 agent
   call-paths"). The recurring value.
6. **Playground (persistent tab).** The deterministic intent → first-call-correct
   replay + provenance-colored graph. Doubles as the **shareable, dev-attracting**
   link.

## B5. The anti-poisoning panel must be *true*

"Malicious calls avoided" is **false** — it implies a live in-path firewall. Our
defense acts at **comprehension time**: we neutralize the poisoned surface before
an agent ever sees a tool. Truthful framings only:

- "Poisoned-surface events **neutralized at comprehension**" (N injection /
  fund-routing / secret-exfil vectors dropped or quarantined).
- "Battle-test posture: N/N attack scenarios caught, 0 benign false-flags."

Distinguish **HARD-guarantee** drops (attacker values in `const`/`default`/
`example`/`enum` — secret shapes, address-routing targets) from **BEST-EFFORT**
prose matches on the panel itself. Never count benign twins as blocks. The
provider value line: *"your spec/docs mirror cannot be weaponized to steer your
users' agents into leaking a key or routing funds."*

## B6. The governance seam — the one-way door

**Decision (ratified): Gecko never ingests a provider's raw request logs into
Gecko-controlled storage.** Accepting raw traffic dissolves the control-plane
promise (A3) that lets us ingest any API unilaterally — it is a one-way door.

If a provider wants **MEASURED** (real, not predicted) numbers:
- They run our aggregator **locally** (`corpus → telemetry.aggregate`, already
  built, fails closed, closed error-class set).
- It sends back **only allowlisted aggregate counts** — never payloads, never
  per-session rows, never free-text.
- `metrics.consent` must be explicitly `true` for any aggregate to leave the
  provider's `local` tenancy.

Rejected alternatives (do **not** build): a live proxy/observe mode that buffers
response bodies; 2xx-but-semantically-wrong detection via body storage; keying
the corpus on `session_id`.

---

# Part C — Sequencing, honesty, open decisions

## C1. The V1/V2 line

**The line: spec-derivable = V1 (ship now, zero invariant risk). Needs real
traffic = V2 (gated on the feedback path).**

**V1 — build now** (config backbone + existing engines, no new capture):
- The `ProviderConfig`/`ApiConfig` loader + migrate Meteora/programs to config.
- Onboard, Configure (B2), Control (B3).
- Metrics panels in **PREDICTED** tier + the **ESTIMATED** *frame* (labeled).
- Drift-watch (built) + notify.
- Trust-boundary panel (B5) from existing sanitize/red-team metadata.

**V2 — do not ship as "measured" yet:**
- MEASURED tier (real production FCC, real error distribution) via the local
  aggregator seam (B6). The seam is designed here; turning it on is V2.
- Any real-traffic revenue attribution.

## C2. Prerequisite fix-list (before this is demo-clean)

1. **Anti-poison false-positives** — real ops over-quarantined (e.g. a base58
   false-positive class dropped a large fraction of a real spec). Fix, or the
   Configure/Control view looks worse than reality. (`gecko/sanitize.py`.)
2. **`servers[].url` auth-exfil** — security review reports the auth-host
   allowlist closes it (`gecko/client.py:180-208`); the `gecko-known-bugs` note
   still flags it live. **Reconcile with a direct check before any security
   demo.**
3. **Dead config** — `providers/orquestra/provider.json` +
   `meteora/program.json` imply a config engine that does not exist. This spec's
   B0 build closes that gap; until then, do not represent the surface as
   config-driven.
4. **Placeholder demo data** — recorded-mode surfaces return zeroed placeholders
   (`gecko/client.py:653-656`); surface the "placeholder, not live" note in any
   provider-facing view.

## C3. The fastest WTP probe (do this in parallel with the build)

The cheapest test of the actual decider needs **no new infra** — the scorecard +
drift-watch already exist:

1. Point `gecko report` at a real design-partner spec → scorecard.
2. Simulate/observe a spec change → `report --since` drift delta ("v4 regressed
   N points, broke M call-paths").
3. Show 3 provider DX owners the result and the mocked revenue-at-risk **ledger**
   against their real spec; mom-test whether they reach for it unprompted.

If they say "prove the number is real first," the modeled loss doesn't sell yet
and we need the MEASURED tier (V2) before the money frame lands. That is the
signal to watch. Discovery script + partner list live in `private/`.

## C4. Open decisions to ratify

- **D1 (ratified in B6):** never ingest raw provider logs. Provider computes,
  we receive counts.
- **D2:** does `pricing_hints` ever get *validated* against a provider's real
  price list, or stay self-declared? (Recommendation: self-declared in V1 —
  validating it would require billing data we refuse to hold.)
- **D3:** where does the config live for a hosted provider — a Gecko-hosted
  tenant store, or the provider's own repo (config-as-code, PR-reviewed)?
  Config-as-code preserves A3 best; hosted is easier for non-technical buyers.
  Decide before the hosted panel is built.
- **D4:** do program APIs and REST APIs truly share one loader, or does `kind:
  "program"` fork early? (Recommendation: one loader, `kind` selects the
  seed-recovery step only.)

---

## Appendix — grounding (what exists today)

| Capability | Module | State |
|---|---|---|
| Scorecard + Playground + diff | `gecko/report.py` | shipped |
| Grading + `--min-grade` gate | `gecko/inspect.py` | shipped |
| Agent-Surface metrics | `gecko/metrics.py` | shipped |
| Correctness validator + corpus log | `gecko/validator.py` | shipped, control-plane-clean |
| Aggregate telemetry (the seam) | `gecko/telemetry.py` | shipped, **ships disabled** |
| Anti-poison + red-team | `gecko/sanitize.py`, `gecko/redteam/` | shipped (8/8) |
| Program surface (config-driven, program path) | `gecko/providers/*` + `gecko/providers/configs/` | **shipped (PR1)** — PDAs + identity are packaged data; only the multi-step `plan` is still code |
| Provider config loader | `gecko/provider_config.py` | **shipped (PR1)** — dataclass models, seed-spec deserialization, `importlib.resources` loader, secrets-are-pointers guard. REST-path fields (visibility/pricing/quarantine applied to `report`) + declarative plans are follow-on PRs |
