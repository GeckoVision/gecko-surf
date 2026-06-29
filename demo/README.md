# Demo recording kit

Everything to shoot the Gecko demo. Two clips, ~90s total.

## Recording the terminal — pick one

| Option | How | Result |
|---|---|---|
| **Use the ready GIF** (fastest) | drop **`petstore.gif`** straight into your video | a clean terminal clip of the real `gecko serve` run — no recording at all |
| **vhs** (crispest, scripted) | `brew install vhs` → `vhs demo/gecko-demo.tape` → `demo/gecko-serve.gif` | runs the real command and records it, perfectly timed, no retakes |
| **Loom** | just screen-record with the terminal window open | works fine — and the same Loom session rolls into Claude Code + Telegram |

> Loom records your screen, so it *does* capture the terminal. The GIF / vhs options are
> only for a cleaner, embeddable terminal clip.

---

## Demo A — "Integrate any API in 60 seconds" (lead with this)

The product magic. Record terminal → Claude Code.

1. **Terminal** (or use `petstore.gif`):
   ```bash
   uvx --from "gecko-surf[serve]" gecko https://petstore3.swagger.io/api/v3/openapi.json
   ```
   → prints `comprehended 19 operations -> 10 usable (9 auth-gated hidden)` + the
   `claude mcp add …` line. *(Verified real output.)*
2. **Copy** the printed `claude mcp add …` line → **paste into Claude Code**.
3. **Ask Claude**, in plain language: *"find the available pets"* (or *"what can this API do?"*).
   Claude calls the right tool, fills the params, and answers — **first try**.
4. Punchline: *"No client written, no docs read. Any API, agent-usable in minutes."*

Swap the Petstore URL for any OpenAPI your audience knows.

## Demo B — "The bot in the wild"

The real-world app. Screen-record the Telegram chat with **@DEV_VEZbot** (run it on the
Anthropic config so it's fast on camera):

1. *"¿cuántas personas desaparecidas hay?"* → the live stats
2. *"busca a María"* → the person list
3. *"dame las últimas noticias"* → recent news *(verify it answers cleanly first)*

Open on the landing page (`ayuda-venezuela-wine.vercel.app`) as the title card, then cut
to the chat.

---

## Assets in this kit
- `petstore.gif` — the real `gecko serve` terminal clip (drop-in).
- `gecko-demo.tape` — a vhs script to record your own crisp terminal GIF.
- More GIFs for B-roll/cutaways live in [`../docs/assets/`](../docs/assets/) (hero,
  first-try, recorded, ssrf, rawtool, auth, surfacerev).
