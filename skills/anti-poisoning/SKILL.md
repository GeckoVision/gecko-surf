---
name: anti-poisoning
description: Protect your agent from a POISONED API surface. When you point an agent at a long-tail, paywalled, or third-party API you don't own, the spec's description text, defaults, examples, enums, servers[], and security schemes are all attacker-controllable — a poisoned spec can try to make your agent paste a private key, echo an API token, route funds to an attacker address, or leak the secret into a URL. This skill covers Gecko's shipped, engine-level defenses (out-of-band trust anchor, spec-text sanitizer, fail-closed auth-host firewall, quarantine) that treat every ingested spec as untrusted input. For agent builders and operators consuming untrusted OpenAPI. The defenses are free and in the open-source engine forever; the hosted poisoning-attempt logs and analytics are the paid tier. NOT a payment rail, NOT a marketplace.
user-invocable: true
---

# Anti-Poisoning Skill

> **For the agent builder / operator — the consumer side.** The other skills in
> this kit help a *provider* make an API agent-ready. This one protects *your
> agent* when it consumes an API you don't own. If you point an agent at a
> long-tail, paywalled, or registry-pulled API, **the spec is attacker-controllable
> input** — this skill is the defense.

## The threat

An OpenAPI spec is not trusted data. Its `description`/`summary` text, param
`default`/`example`/`enum` values, `servers[]` base URL, and `securityScheme`
locations are all things **someone else wrote**. A poisoned spec can't call the API
itself, but it can try to **persuade or trick your agent** into doing the damage:

- paste your **private key / seed phrase** into a request field,
- **echo your API key** back through a poisoned `default`/`example`,
- **route funds** to an attacker address baked into a schema default,
- ship your token to `evil.attacker.test` via a poisoned `servers[].url`,
- **leak the secret into the URL** via an `in: query` auth scheme (logs, proxies),
- smuggle an injected instruction through a **response** schema that recorded mode
  echoes back verbatim.

Most "OpenAPI → MCP tools" pipelines **trust the spec** — they copy the description
into the tool the agent reads, keep every default, derive the call target from
`servers[]`, and follow the security scheme wherever it points. **That trust is the
vulnerability.** Gecko treats the spec as untrusted and neutralizes each shape before
it reaches your agent.

## The four defenses (all Live, in the engine)

Pick the depth you need; load only the file you want (progressive, token-efficient):

| # | Defense | What it stops | Read | Status |
|---|---|---|---|---|
| 1 | **Out-of-band trust anchor** | Auth-host exfil via a poisoned `servers[]` — the auth allowlist is pinned from *provenance*, never the served spec | [how-it-works.md](how-it-works.md) | **Live** |
| 2 | **Spec-text + schema sanitizer** | Injected instructions in description/summary; secret- and address-shaped `default`/`example`/`enum` values that would seed a tool arg | [how-it-works.md](how-it-works.md) | **Live** |
| 3 | **Fail-closed auth-host firewall** | A drifted call target — the caller refuses to inject your secret toward an unexpected host, and degrades to no-auth when there is no pinned host | [how-it-works.md](how-it-works.md) | **Live** |
| 4 | **Quarantine** | A poisoned or from-docs surface is born recorded-only with **no auth injected** until a human clears it | [how-it-works.md](how-it-works.md) | **Live** |

- **[showcase.md](showcase.md)** — the specific exploits blocked (the 7-attack
  showcase) and the battle-test naive→defended scorecard. Honest scorecard, not a
  guarantee.
- **[monitoring.md](monitoring.md)** — the packaging: **defense is free forever**;
  the hosted logs / analytics / regression alerts are Cloud Pro (Building).

## The one guarantee boundary (read this — it's honest, not aspirational)

Gecko does **not** claim "zero poisoning." The defenses split into two tiers, and the
engine is explicit about which is which:

- **HARD guarantee — the arg-routing / auth-live class fails closed.** No
  attacker-controlled *value* can route into an agent-facing tool arg while your auth
  stays live. A secret / crypto-address / injection value in any request-side channel
  (`const`/`default`/`example`/`enum`) is **dropped** and the surface is
  **quarantined** (auth off, recorded-only) until a human clears it. Exceeding the
  scan depth **fails closed** (treated as poisoned), never assumed clean.
- **BEST-EFFORT — pure text prompt injection** in human-readable fields. The
  zero-width strip + homoglyph fold + curated rules **raise the cost** but this is
  defense-in-depth, **not** a guarantee. Known residuals we do **not** claim to catch:
  an instruction *split across sibling fields*, and a *base64/otherwise-encoded*
  payload. For those the real backstops are the other three defenses — the auth-host
  pin, the recorded-mode response scrub, and quarantine-on-detect.

Do not present this skill as "your agent is now unpoisonable." Present it as: *the
spec is untrusted, and here is what the engine provably neutralizes.*

## The boundary (what this is and isn't)

This is the **comprehension / consumption** layer — it makes an untrusted API *safe
to use*. It is **control-plane only**: it stores the API *surface* + correctness /
safety metadata — **never** response payloads, user data, or secrets. It is **not** a
payment rail and **not** a marketplace. The **defense is free**, in the open-source
engine, forever; only the hosted *monitoring* (logs, trend analytics, fleet
triangulation, regression alerts) is a paid tier — see [monitoring.md](monitoring.md).

## Provider

Built by **[GeckoVision](https://geckovision.tech)** — the API-comprehension
company. Engine: [`gecko-surf`](https://github.com/GeckoVision/gecko-surf) (MIT) ·
https://pypi.org/project/gecko-surf/. Defenses live in `gecko/sanitize.py`,
`gecko/surfaces.py`, and `gecko/caller.py`; the exploit showcase in
`examples/poisoning_showcase/`; the battle-test in `gecko/redteam/`.
