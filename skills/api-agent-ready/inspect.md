# Inspect — score your API's agent-readiness (before you ship)

**Step 0 gave you the checklist. `gecko inspect` gives you the score.** Point it at your
API and it grades how ready the surface is for an agent to call correctly the first time —
with located, fixable findings. Run it before a deploy; wire it into CI. Offline, $0,
deterministic, control-plane only (it inspects the *surface*, never sends a real call).

> Available in **gecko-surf ≥ 0.4.4**.

## Run it

```bash
gecko inspect https://api.example.com/openapi.json     # or a bare domain, a docs URL, a path
gecko inspect api.example.com -o report.json           # also write the machine-readable report
gecko inspect api.example.com --min-grade B             # CI gate: exit non-zero below B
```

Input goes through the same resolver as `gecko add`, so you can point it at a **domain**
(it finds the spec), a **docs page** (it recovers one), or a **spec file** — you don't need
to hand it an `openapi.json`.

## What it scores (four dimensions, one grade)

| Dimension | Checks |
|---|---|
| **first-call-correct** | Can an agent build a valid first call for each op? (the core promise) |
| **spec hygiene** | Unique `operationId`s · summaries present · params typed with `in` · auth declared · error responses documented |
| **agent-friendliness** | **Ambiguity** — does each op rank #1 for its own intent, or does a sibling steal the routing? (the `getTipFloor` / mint-vs-symbol trap) |
| **security** | Anti-poisoning scan of the spec text (injection-shaped descriptions) |

It prints a weighted **A–F grade**, then each finding with its location and a concrete fix.
`--min-grade` (or **any blocking finding**) exits non-zero — **TDD-for-APIs**: fail the
deploy on a regression, the way you'd fail on a red test.

## Why the ambiguity check is different

Anyone can lint a spec for missing fields. The **agent-friendliness** dimension is
comprehension-native: it runs each op's own intent through Gecko's catalog and flags the
ones a *sibling* out-ranks — an agent asking for op X's job would be routed to op Y. That's
a real first-call failure a schema linter can't see, and only Gecko can, because it
comprehends the surface the way an agent does.

## Worked example — Pegana

```
pegana: agent-readiness A (96/100) — 0 blocking, 1 warning
  first-call-correct   100/100
  hygiene               84/100   (8 ops: no documented 4xx/5xx responses)
  agent-friendliness    98/100   ⚠ list_alerts loses its routing to list_my_alerts
  security             100/100
```

A near-perfect surface — and `inspect` still surfaced a genuine routing ambiguity
(`list_alerts` vs the JWT-gated `list_my_alerts`) and the undocumented error responses. Fix
those, re-run, ship.

## Where it fits

`inspect` is the measurement layer on top of [best-practices.md](best-practices.md): the
checklist is the design advice, `inspect` is the objective score against it. Run it while
building, gate CI on it, then move on to [comprehend.md](comprehend.md) and
[serve-mcp.md](serve-mcp.md) to make the graded surface agent-callable.
