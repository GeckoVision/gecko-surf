# Monitoring — free defense, paid observability

> The single most important line in this skill: **the defense is free, in the
> open-source engine, forever. Safety is never gated.** What is a paid tier is the
> hosted *observability* on top — the logs, trend analytics, fleet triangulation, and
> regression alerts. This file draws that line precisely and honestly.

---

## The split

| | **Protection (the defense)** | **Observability (the monitoring)** |
|---|---|---|
| **What** | Trust anchor, sanitizer, auth-host firewall, quarantine | Hosted logs + analytics of what was blocked, over time, across surfaces |
| **Where** | In `gecko-surf` (MIT), on every call, locally | GeckoVision Cloud Pro (a hosted control-plane service) |
| **Price** | **Free forever. Never gated.** | Paid tier |
| **Status** | **Live** — shipped, tested, falsifiable ($0 offline) | **Building** |

### Why safety is never behind a paywall

Poisoning is a safety property, not a feature. Gating it would mean a paying customer
is protected and a free user is not — an unacceptable posture for infrastructure that
asks you to point an agent at untrusted APIs. So the rule is absolute: **every defense
in [how-it-works.md](how-it-works.md) runs for everyone, free, forever.** If a plan
ever proposes gating a defense, stop — that violates the model.

---

## What Cloud Pro adds (Building)

The engine already emits a **control-plane-safe** record for each graded outcome —
categorical / boolean fields only. It **never** stores a canary, host, address, amount,
response payload, or any arg value; a leak channel is recorded as a **name** (`url`,
`body`, `header:X-Api-Token`), never the value. That discipline (an allowlist writer
that fails closed on any non-allowlisted key) is what makes it safe to aggregate a
customer's poisoning telemetry at all — and it is the same invariant #1 that governs
the rest of Gecko.

Cloud Pro turns that stream into observability:

- **Blocked-attempt logs** — a queryable, control-plane-safe feed of what was
  sanitized, quarantined, or auth-host-blocked, and which defense fired.
- **Poisoning-attempt trends per `surface_rev`** — a spec is content-hashed to a
  stable revision, so you can see *"this surface started shipping injected `default`s
  at rev `a1b2c3`"* — attribute an attack to a specific spec version.
- **ASR / FRR over time** — track attack-success-rate and false-refusal-rate as your
  API set and the engine's rules evolve; catch a regression before it ships.
- **Fleet triangulation** — the same poison shape seen across many customers' surfaces
  is a signal no single customer can see alone: an emerging campaign against a shared
  upstream API.
- **Regression alerts** — get notified when a `surface_rev` bump flips a
  previously-clean surface into tripping a defense, or when FRR creeps up on a benign
  op you rely on.

None of these are required to be *protected*. They tell you *what your protection
caught* and *how the threat is trending* — the operational layer on top of a defense
that already ran for free.

---

## The "first 500" offer (read this precisely)

**"First 500" is a launch offer for the paid analytics — never a gate on
protection.** Concretely:

- The first 500 teams may get Cloud Pro **monitoring** (the logs / analytics / alerts
  above) on favorable launch terms.
- It does **not** mean the first 500 get protection and the 501st does not. The
  **defense is free for everyone**, unconditionally, before and after any offer.

If you see "first 500" framed as early access to *safety*, that's wrong — it's early
access to *observability*.

---

## The boundary this preserves

Monitoring stays inside the **comprehension / consumption** lane and inside invariant
#1 (control-plane only): it aggregates **safety metadata**, never payloads, user data,
or secrets. It is **not** a payment rail and **not** a marketplace — selling
observability on your own agents' safety telemetry is neither. If a "monitoring"
feature ever wants to store a response body, a token, or a per-surface catalog other
customers can browse, it has drifted out of lane — stop and re-scope.
