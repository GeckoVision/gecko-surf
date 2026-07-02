# CLAUDE.md — gecko-api-kit

Context for any agent working *in this repo*. (The skills themselves, for end
users, are `skills/api-agent-ready/SKILL.md` and `skills/x402-payai-setup/SKILL.md`.)

## What this is

A standalone, MIT-licensed **provider-onboarding** kit built on
[`gecko-surf`](https://github.com/GeckoVision/gecko-surf) (MIT, on PyPI), the
API-comprehension engine. Where `solana-api-skill` teaches an agent to *call* an
unfamiliar API, this kit teaches you to make an API *you provide* usable by agents —
its **whole** surface, first-call-correct, over MCP, **alongside** whatever MCP the
provider already ships.

Two skills:
- **`api-agent-ready`** — comprehend → emit breadcrumbs → serve MCP → discoverable,
  aggregate-not-replace.
- **`x402-payai-setup`** — wire pay-per-call via PayAI; provider keeps 100%; Gecko
  never the rail, no cut.

It ships **no executable logic of its own** — it's a Claude Code plugin (skills +
commands + agents), installed from the **Marketplace**
(`/plugin install gecko-surf@geckovision`). The content is markdown the agent reads;
the engine is a separate `gecko-surf` install the plugin drives via `uvx`/`pip`.

## Layout

```
skills/
  api-agent-ready/     SKILL.md + the five-step spine:
                       comprehend · artifacts · serve-mcp · discoverable ·
                       aggregate-not-replace
  x402-payai-setup/    SKILL.md + wire-x402-payai · verify-paid-call
agents/                api-onboarding-engineer · x402-payments-engineer
commands/              /make-agent-ready <url> · /setup-x402 <api>
rules/                 aggregate-not-rail (aggregate/compose boundary)
.claude-plugin/        plugin.json — the Marketplace plugin manifest
CLAUDE.md · LICENSE (MIT) · README.md · VERSION · CHANGELOG.md
```

## Rules for editing

- **Honest status, per file.** Label **Live** vs **Building** and never blur them.
  Live today (in `gecko-surf`): comprehend, `search_capabilities`, Streamable-HTTP
  MCP + one-click add, SSRF guard, `gecko test`, `gecko from-docs`. Building: agent-
  native artifact **auto-emission** (`llms.txt` / `x-gecko` / `gecko.json` are a
  hand-authored pattern today), breadcrumb discoverability, drift re-ingest, the
  correctness corpus, and live x402/PayAI settlement.
- **Don't invent PayAI specifics.** Facilitator URLs, SDK call names, exact endpoint
  shapes get a `<!-- VERIFY -->` marker, not an invented value. Confirm against
  PayAI's live docs before publishing.
- **Stay in lane — the whole point of `aggregate-not-rail`.** This is the
  **comprehension / consumption** layer. It is NOT a payment rail (we compose
  PayAI/Metera/pay.sh and take no cut) and NOT a marketplace (no public catalog of
  providers' APIs). If an edit drifts toward either, stop.
- **Aggregate, not replace.** Never suggest touching, proxying, or replacing a
  provider's own MCP. Gecko is additive — the full surface *alongside* what exists.
- **Control-plane only.** Model storing the API surface + correctness metadata —
  never response payloads, user data, or secrets.
- **Never sign or broadcast.** Live settlement is founder-run only; default
  `X402_MODE=stub`. Examples must model that, never a real broadcast.
- **Security in examples.** Never a real key or `payTo` — placeholders only; teach
  hiding auth from the tool def and redacting it from logs.
- **Keep it progressive.** SKILL.md routes; focused files load on demand. Don't
  inline everything.
- **Ground the numbers.** The Pegana figures (41 / 26 / 15, 6/6, ~6 MCP tools) come
  from the committed demo in `surfcall/examples/pegana_demo/`. Don't drift them.

## Provider

Built by [GeckoVision](https://geckovision.tech) — the API-comprehension company.
Engine: [`gecko-surf`](https://github.com/GeckoVision/gecko-surf) ·
https://pypi.org/project/gecko-surf/. Sibling skill:
[`solana-api-skill`](https://github.com/GeckoVision/solana-api-skill) (the consumer
side — make any API *callable* first-call-correct).
