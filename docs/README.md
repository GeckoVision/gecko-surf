# Gecko docs

**Gecko makes any external API agent-usable — no integration code.** Point an agent at
an API (even a messy, paywalled one behind human-shaped docs) and it finds the right
call, makes it correctly the first time, and runs.

Install: `pip install gecko-surf` · CLI: `gecko <openapi-url>` · SDK: `from gecko import AgentApiClient`

## Product docs
| Page | What |
|---|---|
| [Overview](index.md) | The one-liner, the before/after, the hero demo, where we are. |
| [Quickstart](quickstart.md) | Serve any API over MCP, or embed the SDK — in minutes. |
| [Why Gecko](why.md) | Using vs. not using; the before/after table; who it's for. |
| [How it works](how-it-works.md) | The five-stage comprehension pipeline. |
| [Stay correct](stay-correct.md) | What happens when the upstream API changes. |
| [FAQ & data governance](faq.md) | BYOK, control-plane-only, SSRF, vs OpenAPI→MCP generators. |

## Where we are
V1 — the comprehension path — is **live**: ingest → first-call-correct tools → access →
direct call, proven end-to-end against the real, paywalled TxODDS API, with a **`$0`
recorded mode** that runs the whole path offline. The **vectorized semantic index**, the
**docs→OpenAPI on-ramp**, and the **auto-update "stay-correct" job** are on the V2
roadmap, labeled as such throughout.

---

## Internal planning *(not product docs)*
Operational planning lives in this folder; the canonical strategy set is in Notion.

- [`discovery/…interviews.md`](discovery/2026-06-26-agent-api-consumption-interviews.md)
  — the Mom-Test discovery script. **The WTP decider.**
- [`strategy/…v1-spine-plan.md`](strategy/2026-06-26-txodds-v1-spine-plan.md)
  — the V1 implementation spine.
- [`strategy/enablement-thesis-backlog.md`](strategy/enablement-thesis-backlog.md)
  — deferred ideas / the 6→8 work.
- `decisions/` — architecture decision records.

**Canonical strategy (Notion):**
[Gecko home](https://app.notion.com/p/38c1585f5e41810fb9e6d05612da6c64) ·
[PRD](https://app.notion.com/p/38b1585f5e41817ea04fc924ec2add42) ·
[Business Hypothesis](https://app.notion.com/p/38c1585f5e4181a58d94c165cf265507) ·
[Roadmap](https://app.notion.com/p/38c1585f5e4181408211c0a2315841b3) ·
[ICP](https://app.notion.com/p/38c1585f5e4181f1ab08f59bc7e74acd) ·
[Architecture](https://app.notion.com/p/38c1585f5e4181938f11cfa0bf2a458b)

> `private/` business docs are gitignored (mirrored to Notion). Don't fork the
> Notion-canonical PRD/ICP into the repo — keep one source of truth.
