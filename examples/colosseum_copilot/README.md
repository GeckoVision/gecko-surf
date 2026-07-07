# Colosseum Copilot — ready-to-use, via Gecko

Give your coding agent **first-call-correct** access to the [Colosseum Copilot API](https://docs.colosseum.com/copilot/api-reference)
— search projects, analyze cohorts, compare, submit feedback — without wrestling the docs.

Gecko comprehended this API from its docs (Colosseum publishes no OpenAPI). Your token is
**BYOK**: injected at call time, hidden from the agent, and sent only to Colosseum.

## Use it (3 steps)
```bash
export COLOSSEUM_COPILOT_PAT=...            # get one: https://arena.colosseum.org/copilot
uvx --from "gecko-surf[serve]" python serve_colosseum.py
claude mcp add --transport http colosseum http://127.0.0.1:8000/mcp
```
Then ask your agent: *"search Colosseum for Solana data-API projects"* — it calls it right, first try.

## Why it's not just `from_openapi`
There's no OpenAPI to convert. And even the docs mislabel the routes (they show
`/colosseum_copilot/status`; the real route is `/status`). Gecko reads the docs, comprehends
the true surface, hides the auth, pins the host so your PAT can't leak, and sends a real
User-Agent so Colosseum's WAF doesn't block it. That's the difference between *wrapping a spec*
and *making the call correct*.

Built with [Gecko](https://geckovision.tech) — the API comprehension layer for agents.
