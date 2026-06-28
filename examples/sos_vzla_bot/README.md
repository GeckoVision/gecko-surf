# SOS Venezuela 2026 — Telegram bot (powered by surfcall)

A Spanish-first Telegram bot that lets **anyone** — no coding needed — query the
[SOS Venezuela 2026](https://sosvenezuela2026.com) humanitarian data by chatting:
search missing/found people, the hazard map, aggregate stats, structural-damage
validations, and earthquake news.

It is an **LLM agent that USES the API _through_ surfcall** — surfcall's first
humanitarian dogfood and API #3. Built for Build4Venezuela.

```
schema.sql + live API ──▶ hand-authored OpenAPI 3.1 stub (spec/)
                               │
                               ▼
                 surfcall ingest → McpSurface tools   (surfcall_tools.py)
                               │
                               ▼
         Claude tool-use loop  (agent.py) ──▶ Telegram long-polling  (bot.py) ──▶ citizen
```

Architecture is four small, independently testable units:

| File | Purpose |
|---|---|
| `spec/sosvenezuela_openapi.json` | The hand-authored OpenAPI 3.1 stub (5 public GETs, canonical enums baked in). Also the artifact we offer upstream. |
| `surfcall_tools.py` | The surfcall⇄LLM seam: builds the engine, exposes the **5 public reads only** (allow-listed), sanitizes + caps output. |
| `agent.py` | The Claude tool-use loop. `llm` is injected, so it tests offline. |
| `bot.py` | Telegram long-polling transport + per-user rate limit. Pure `handle_message` for tests. |

## Run the tests (offline, $0, no deps)

The whole recorded path needs only the surfcall engine — **no Anthropic, no Telegram**:

```bash
uv run pytest examples/sos_vzla_bot/tests/ -q
```

## Choose your LLM provider (free or paid)

The provider is pluggable via env vars — the agent loop is identical either way:

| `SOSBOT_PROVIDER` | Key needed | Default model | Cost | Notes |
|---|---|---|---|---|
| `openrouter` (default) | `OPENROUTER_API_KEY` | `meta-llama/llama-3.3-70b-instruct:free` | **$0** | Free, multilingual; tool-calling slightly less reliable |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-4-5` | ~$0.005/chat | Most reliable first-call-correct tool use |

Override the model with `SOSBOT_MODEL=...` (e.g. a free `openai/gpt-oss-120b:free`,
or a cheap paid `openai/gpt-oss-120b` at $0.03/$0.15 per Mtok). If a free model flubs
tool calls, flip to `SOSBOT_PROVIDER=anthropic`.

## Run the bot live (founder-run)

Long-polling — no public URL, $0 hosting. Hits the real public SOS Venezuela API.

1. **Get a Telegram bot token** from [@BotFather](https://t.me/BotFather) (`/newbot`).
2. **Set secrets** (never commit them) — free path shown:
   ```bash
   export TELEGRAM_BOT_TOKEN="...from BotFather..."
   export OPENROUTER_API_KEY="...your key..."   # free models, $0
   # paid/reliable alternative:
   #   export SOSBOT_PROVIDER=anthropic ANTHROPIC_API_KEY="..."
   # dry-run the wiring at $0 (synthesize answers, no live API):
   #   export SOSBOT_MODE=recorded
   ```
3. **Install the bot extra and run:**
   ```bash
   uv sync --extra sosbot
   uv run python -m examples.sos_vzla_bot
   ```
4. DM your bot, e.g. *«¿está reportada María Pérez?»*, *«¿cuántos desaparecidos hay?»*,
   *«últimas noticias»*.

## Honest notes

- **Free for users; we pay the LLM** (cents per chat on Haiku 4.5).
- It validates **comprehension / dogfood**, not surfcall's WTP thesis — a free
  humanitarian API gives zero willingness-to-pay signal.
- It mirrors the platform's **privacy** stance: cédulas stay masked, coordinates
  approximate, minors protected. The bot never de-masks anything.
- Data attribution: **«SOS Venezuela 2026»**. Emergencies in Venezuela: **171**.
