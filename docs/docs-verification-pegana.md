# What a document cannot tell you

*Draft — measured 2026-08-07 against the live Pegana API, published with Pegana's
permission. Numbers below are from that run; re-run the commands at the bottom and you
will get today's.*

A spec is a **claim**. An indexed copy of that spec, or of the prose around it, is a
faithful copy of the same claim. Neither one has ever called the endpoint.

That is not a criticism of documentation — it is what documentation *is*. A document
describes intent at the moment it was written. An agent needs something else: whether the
call it is about to make **answers right now**. Between those two things sit all the
ordinary facts of a running service — an endpoint renamed last sprint, a parameter that
needs a real identifier rather than a plausible-looking one, a route that is documented
but gated. No amount of indexing prose closes that gap, because the evidence isn't in the
prose.

`gecko verify-docs --live` closes it the only way it can be closed: by making the call
and reporting what came back.

```bash
gecko verify-docs https://api.pegana.xyz/openapi.json --live
```

## Three outcomes, and the third one is not a failure

| Verdict | What was observed | What it licenses you to say |
|---|---|---|
| **VERIFIED** | The documented call was made and the API answered `2xx`. | This operation works, as documented, from here, now. |
| **REFUTED** | The call was made with nothing invented, and the API said the route does not exist (`404`/`405`/`410`). | The document claims something the API does not serve. |
| **UNVERIFIED** | No evidence either way was obtained. | Nothing. Explicitly nothing. |

UNVERIFIED is reported as its own outcome, never folded into either side, and it always
carries **why**. That rule is load-bearing. The most common reason an endpoint refuses a
probe is that the probe had to invent an argument — a made-up token mint is not a real
token mint, and "my invented id returned 404" is emphatically **not** "your endpoint does
not exist". Reporting the second when we only observed the first would be an accusation,
not a measurement. So any error status on a call that contained a synthesized argument is
downgraded to UNVERIFIED and labelled `no-real-argument:<param>`.

## The subject: a well-run API

Pegana is a design partner. Its OpenAPI scores **A (100/100)** on Gecko's
agent-readiness scorecard — 100 across first-call-correct, hygiene, agent-friendliness
and security, with **0 blocking findings and 0 warnings**. That is the point of choosing
it. This page is not "here is what is wrong with an API". It is "here is the class of
fact no document can carry", shown on a document that is already excellent.

Scope of the run: the **28 keyless operations** on the surface. Fifteen further
operations (fourteen under `/v1/me/...` — subscriptions, webhooks, preferences — plus
`POST /v1/auth/logout`) require a credential a public probe does not hold; they are out
of scope for this run rather than judged by it.

And the probe is **do-no-harm**: only `GET`/`HEAD` reach the wire, so no `POST`, `PATCH`
or `DELETE` was ever fired at a partner's live service. The three `POST /v1/auth/...`
operations are therefore reported `no-access:recorded-only` — deliberately unprobed.

## Result

```
$ gecko verify-docs https://api.pegana.xyz/openapi.json --live
"summary": {"verified": 13, "refuted": 0, "unverified": 15}
```

```
$ gecko verify-docs https://api.pegana.xyz/openapi.json --live \
    --confirm mint=solana-token-mint --confirm symbol=solana-token-symbol
"summary": {"verified": 19, "refuted": 0, "unverified": 9}
```

### Zero REFUTED is the expected, correct outcome here

Say that plainly, because a zero can be misread as a null result. **REFUTED is a finding
about a broken claim.** On an API whose spec is generated from its own routes and kept in
step with them, there is nothing for it to find, and finding nothing is the answer. A
verification tool that only produces value when it catches something is a tool with an
incentive to catch something.

What the run *does* produce on a healthy API is the positive half: **19 operations an
agent can now be told are live, with evidence, rather than believed to be live because a
document said so.** That is the deliverable.

### The default run — 13 VERIFIED / 0 REFUTED / 15 UNVERIFIED

| Operation | Verdict | Basis |
|---|---|---|
| `GET /` | VERIFIED | `replay:200` |
| `GET /healthz` | VERIFIED | `replay:200` |
| `GET /readyz` | VERIFIED | `replay:200` |
| `GET /v1/alerts` | VERIFIED | `replay:200` |
| `GET /v1/assets` | VERIFIED | `replay:200` |
| `GET /v1/assets/{symbol}/history` | VERIFIED | `replay:200` |
| `GET /v1/audit` | VERIFIED | `replay:200` |
| `GET /v1/calibration` | VERIFIED | `replay:200` |
| `GET /v1/meta/webhook-ips` | VERIFIED | `replay:200` |
| `GET /v1/meta/webhook-keys` | VERIFIED | `replay:200` |
| `GET /v1/methodology/current` | VERIFIED | `replay:200` |
| `GET /v1/peg/feed` | VERIFIED | `replay:200` |
| `GET /v1/stats` | VERIFIED | `replay:200` |
| `GET /v1/assets/by-mint/{mint}` | UNVERIFIED | `replay:404`, `no-real-argument:mint` |
| `GET /v1/assets/by-mint/{mint}/state` | UNVERIFIED | `replay:404`, `no-real-argument:mint` |
| `GET /v1/peg/feed/by-mint/{mint}` | UNVERIFIED | `replay:404`, `no-real-argument:mint` |
| `GET /v1/assets/{symbol}` | UNVERIFIED | `replay:404`, `no-real-argument:symbol` |
| `GET /v1/assets/{symbol}/state` | UNVERIFIED | `replay:404`, `no-real-argument:symbol` |
| `GET /v1/assets/{symbol}/loop-exposure` | UNVERIFIED | `replay:404`, `no-real-argument:symbol` |
| `GET /v1/assets/{symbol}/simulate-depeg` | UNVERIFIED | `replay:400`, `no-real-argument:shock_bps`, `no-real-argument:symbol` |
| `GET /v1/audit/{alert_id}` | UNVERIFIED | `replay:400`, `no-real-argument:alert_id` |
| `GET /v1/audit/{alert_id}/onchain` | UNVERIFIED | `replay:400`, `no-real-argument:alert_id` |
| `GET /v1/audit/{alert_id}/replay-bundle` | UNVERIFIED | `replay:400`, `no-real-argument:alert_id` |
| `GET /v1/audit.csv` | UNVERIFIED | `replay:400`, `no-real-argument:from`, `no-real-argument:to` |
| `POST /v1/auth/magic/mint` | UNVERIFIED | `no-access:recorded-only` |
| `POST /v1/auth/magic/consume` | UNVERIFIED | `no-access:recorded-only` |
| `POST /v1/auth/telegram` | UNVERIFIED | `no-access:recorded-only` |
| `GET /v1/ws` | UNVERIFIED | `replay:400`, `no-evidence:endpoint-answered` |

Read the UNVERIFIED column and a shape appears immediately: **fourteen of the fifteen are
statements about our probe, not about Pegana.** Eleven say "we had to invent an
argument". Three say "we chose not to call this". The fifteenth, `GET /v1/ws`, is about
the endpoint and still isn't a fault: it is a WebSocket upgrade route, so a plain HTTP
`GET` is refused by design. The route answered — evidence of liveness, but not of the
documented behaviour — so it stays UNVERIFIED rather than being scored either way.

### Declare the value domains — 19 VERIFIED / 0 REFUTED / 9 UNVERIFIED

The invented-argument bucket is not a wall. Tell Gecko what those two parameters
*are* — a Solana token mint, a Solana token symbol — and it fills a real, canonical value
instead of a placeholder:

```bash
gecko verify-docs https://api.pegana.xyz/openapi.json --live \
  --confirm mint=solana-token-mint \
  --confirm symbol=solana-token-symbol
```

Six operations move from "we couldn't tell" to "answers, `200`":

| Operation | Default | With domains declared |
|---|---|---|
| `GET /v1/assets/by-mint/{mint}` | UNVERIFIED | **VERIFIED** |
| `GET /v1/assets/by-mint/{mint}/state` | UNVERIFIED | **VERIFIED** |
| `GET /v1/peg/feed/by-mint/{mint}` | UNVERIFIED | **VERIFIED** |
| `GET /v1/assets/{symbol}` | UNVERIFIED | **VERIFIED** |
| `GET /v1/assets/{symbol}/state` | UNVERIFIED | **VERIFIED** |
| `GET /v1/assets/{symbol}/loop-exposure` | UNVERIFIED | **VERIFIED** |

Nine remain UNVERIFIED, each with its reason unchanged:

| Operation | Verdict | Basis | In plain terms |
|---|---|---|---|
| `GET /v1/assets/{symbol}/simulate-depeg` | UNVERIFIED | `replay:400`, `no-real-argument:shock_bps` | `symbol` is real now; the shock size is still a number we made up. |
| `GET /v1/audit/{alert_id}` | UNVERIFIED | `replay:400`, `no-real-argument:alert_id` | An alert id is a runtime entity — no canonical value exists to declare. |
| `GET /v1/audit/{alert_id}/onchain` | UNVERIFIED | `replay:400`, `no-real-argument:alert_id` | Same. |
| `GET /v1/audit/{alert_id}/replay-bundle` | UNVERIFIED | `replay:400`, `no-real-argument:alert_id` | Same. |
| `GET /v1/audit.csv` | UNVERIFIED | `replay:400`, `no-real-argument:from`, `no-real-argument:to` | A time window we invented, not one the spec pins. |
| `POST /v1/auth/magic/mint` | UNVERIFIED | `no-access:recorded-only` | Mutating; never fired at a live partner. |
| `POST /v1/auth/magic/consume` | UNVERIFIED | `no-access:recorded-only` | Mutating; never fired. |
| `POST /v1/auth/telegram` | UNVERIFIED | `no-access:recorded-only` | Mutating; never fired. |
| `GET /v1/ws` | UNVERIFIED | `replay:400`, `no-evidence:endpoint-answered` | A WebSocket route; a plain `GET` cannot verify it. |

Those nine are the honest residue, and they are the reason the third verdict exists. Each
one names precisely what would be needed to resolve it — a real alert id, a declared time
window, a credential, a WebSocket probe. That is a to-do list. A confidence score would
have been a guess.

## What VERIFIED does and does not mean

It means: **this exact call, built from the spec, reached this API and was answered
`2xx`.** It does not mean the response was semantically correct, complete, or fresh —
judging the *content* of an answer is a different question and is not claimed here. The
honesty boundary is deliberate and it is enforced in code: a recorded (offline) run
synthesizes responses from the schema, so it reports `0 verified / 0 refuted / 43
unverified` on this same API — a synthesized `200` can never be promoted to VERIFIED.
Only an observed status off the real wire can produce a VERIFIED or a REFUTED.

Nothing from the responses is retained. The report carries operation ids, verdicts, basis
labels and counts — no response bodies, no argument values, no filled URLs.

## Reproduce it

```bash
pip install gecko-surf

# the default run
gecko verify-docs https://api.pegana.xyz/openapi.json --live

# with the value domains declared
gecko verify-docs https://api.pegana.xyz/openapi.json --live \
  --confirm mint=solana-token-mint \
  --confirm symbol=solana-token-symbol

# the offline baseline: $0, no wire, all 43 ops honestly UNVERIFIED
gecko verify-docs https://api.pegana.xyz/openapi.json
```

Counts move as an API moves. That is the entire point: a document is a snapshot of
intent, and this is a measurement of now.

---

*With thanks to the Pegana team for permission to publish a measured run against their
live API.*
