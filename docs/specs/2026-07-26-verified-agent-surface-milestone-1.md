# Milestone 1 — the Verified Agent Surface (VAS)

**Status:** canonical roadmap (2026-07-26), from a research + planning panel on the founder's
Gecko + Context7 integration brief. Pairs with `docs/positioning.md` ("stop guessing"),
`docs/icp.md`, `docs/provider-delivery.md`. GTM specifics stay in `private/`.

## The one-line

> **Context7 tells the agent *what* to call. Gecko proves it's *right* — before the call
> fires — and chains the calls Context7-fed agents get wrong.**

Context7 is a **distribution channel and a docs *input*, never a dependency or ground truth.**
Gecko is the verification + execution + correlation layer *below* it.

## What the evidence says (dev-side strong, provider-side thin — that's the finding)

- **Biggest developer pain = the multi-step chain.** Single well-documented calls are nearly
  solved; the *chain* is the death zone — 41–86.7% production failure (a 3-step chain at
  70%/step ≈ 34% end-to-end). Docs-only tools don't touch it. This is our correlation frontier.
  The wedge is no longer "first call correct" (table stakes) — it's **the chain that holds.**
- **Context7's docs are provably not ground truth.** Noma Security's "ContextCrush" shows its
  registry is *poisonable*; its own disclaimer admits content isn't guaranteed accurate; silent
  quota throttling makes agents "hallucinate again mid-session." (Corroborates our Privy
  fabrication finding and validates Skill Guard in the Context7 ecosystem.)
- **Provider "agent-readiness" is a *future* pain, not a bleeding one.** Almost all provider
  evidence is vendor thought-leadership (DX → AX, "agents are the new users of your API"), not
  practitioner testimony. **The internet cannot confirm provider WTP** — that is discovery-
  interview work. Provider curiosity hook: *"call my API correctly without me building an MCP
  server, and show me how agents are misusing it (the 400s I can't see)."*

## The milestone: the Verified Agent Surface (VAS) v1

One `gecko` invocation turns a painful API into a single content-addressed object
(`surface_rev`) carrying four layers, each with provenance:

| Layer | Shipped primitive | Provenance |
|---|---|---|
| Comprehension (first-call-correct tools + call graph) | `inspect`/`from-docs`/`graph`/`caller`/`tools` | EXTRACTED |
| Correlation (intra + cross-API chains) | `correlate`, `compose.cross_plan`, `resolve` + value-domain `index` | DECLARED / customer-CONFIRMED |
| Security (surface not poisoned) | Skill Guard (`sanitize`/`imagescan`/`encdetect`) | quarantine |
| **Verified-against-reality** ← the missing tier | `validator` replay + JSONL outcome (control-plane, no payloads) | **VERIFIED / REFUTED / UNVERIFIED** |

**DONE (milestone 1):** `gecko report <api>` emits one HTML Scorecard where every tool and
correlation edge shows a provenance badge, and any replayed tool shows VERIFIED or REFUTED.
The *same* `surface_rev` renders dev-side (`connect` → MCP tools) and provider-side (Scorecard).

**Last-mile gaps to close (the milestone's remaining work):**
1. No verdict tier that says "this doc claim was checked against the live API" — `validator`
   replays, but its outcome isn't lifted into surface provenance and rendered.
2. No ingest path that treats a *docs source* (Context7 or `from-docs`) as an untrusted
   **CLAIM** to be verified rather than trusted.

## The gap-fix — the Privy problem, generalized (`gecko verify-docs`)

A docs source yields *doc-claimed operations* `{method, path, params, source}` tagged
**CLAIMED**. Then:
1. `caller.prepare` builds the request from the claim.
2. `validator._capture` replays it (recorded first per Pattern B; live smoke last) — **status /
   shape only, no payload stored** (invariant #1).
3. Emit a verdict: **VERIFIED** (exists, shape matches) · **REFUTED** (404 / contract mismatch —
   the fabricated Privy endpoint) · **UNVERIFIED** (no access / recorded-only).

Built: replay, capture, prepare, JSONL log. Net-new: (a) a `CLAIMED` provenance tier in the
surface model, (b) `gecko verify-docs <api> --source context7|from-docs`, (c) the Scorecard
rendering the verdict badges. Thin transport around shipped primitives — keep it in the package.

## The Context7 integration — minimal, no hosting

- **Arch 2 (parallel MCP) — Tier 0, ~0 build:** an `mcp.json` snippet + doc; a dev adds Context7
  (docs) *and* `gecko connect`/`serve` (deterministic surface) side by side. Packaging only.
- **Arch 3 (Context7 library listing) — Tier 1, ~½ day:** publish Gecko as a Context7 library
  entry pointing at the OSS + `gecko add`. Distribution only, reversible, no `gecko/` code.
- **The differentiator — `gecko verify-docs` (Tier 2, ~2–3 days):** the only engine work, and the
  whole reason to bother — Gecko catches Context7's fabrications instead of trusting them.

**Reject** the brief's "hosted Gecko API" (`POST /api/v1/context7/infer`, 99.9% uptime). OSS /
on-device until WTP is proven. **Compose Context7, do not replace it.**

## The flagship demo — one artifact, two audiences

Point at **Privy** → Gecko comprehends → ingests Context7's docs as CLAIMS → `verify-docs` flags
the **fabricated endpoint REFUTED** (red badge) while the real ones show VERIFIED → an agent runs
a **first-call-correct chained call** → the *same* `surface_rev` renders as the provider
Scorecard with the REFUTED badge visible.

The single frame both audiences stare at: **the red REFUTED badge on a claim the popular docs
source got wrong.** Dev value: *your agent won't call the hallucinated endpoint, and the chain
holds.* Provider value: *your docs drifted — here's the receipt.* One surface, one screenshot.
Don't add a second artifact.

## Next deliveries (sequenced)

1. **`CLAIMED` → verdict provenance in the surface model** — net-new, ~2d, **one-way** (public
   surface shape). Unblocks everything below. Dev + provider.
2. **`gecko verify-docs`** wired to `validator` — net-new, ~2d. Dev pain: hallucinated endpoints;
   provider pain: drift.
3. **Scorecard renders CLAIMED / VERIFIED / REFUTED badges** — extends shipped `report`, ~1d.
   Provider-visualizable.
4. **Context7 Arch 2 snippet + Arch 3 listing** — ~1d, reversible. Distribution.
5. **Recorded-mode Privy verify fixture** (Pattern B) — ~1d, so the demo falsifies offline before
   any live smoke.

## Guardrails / do NOT build

- **No hosted Gecko API.** No Context7 *replacement* (compose, don't compete). No storing a
  verified response payload (invariant #1). No vectors/DB (unchanged V2-scale call).
- **Provider-facing build gates behind discovery.** Provider WTP is unknowable from desk
  research; build dev-side chaining + verification first, hold a dedicated provider *panel* until
  the founder's conversations earn the truth. The Scorecard reuses the same surface, so no
  provider-specific bet is required for the demo.

## Honest flags (founder decisions)

- **Paywalled-API verify** needs access — recorded verdicts MUST be labelled `UNVERIFIED`, never
  overclaimed as VERIFIED.
- **REFUTED-badging a third party's docs (Context7) publicly is a one-way reputational call** —
  founder + positioning sign-off before the demo goes public. (Frame it as "docs drift, not an
  accusation"; verify against reality, show the receipt, name no villain.)
