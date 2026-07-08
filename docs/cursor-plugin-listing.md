# Cursor marketplace listing — runbook

Internal checklist for publishing **gecko-surf** on the Cursor plugin marketplace.

## Prerequisites

- Public repo: `https://github.com/GeckoVision/gecko-surf`
- Plugin root: [`skills/`](../skills/) (reused for Claude Code + Cursor)
- Cursor manifest: [`skills/.cursor-plugin/plugin.json`](../skills/.cursor-plugin/plugin.json)
- Marketplace index: [`.cursor-plugin/marketplace.json`](../.cursor-plugin/marketplace.json)
- Bundled MCP: [`skills/mcp.json`](../skills/mcp.json) → `gecko-txline` (recorded demo, no secrets)

## Local test (before submit)

```bash
git checkout feat/cursor-plugin-listing   # or main after merge

mkdir -p ~/.cursor/plugins/local
rm -rf ~/.cursor/plugins/local/gecko-surf
cp -r skills ~/.cursor/plugins/local/gecko-surf

# Reload Cursor window (Cmd/Ctrl+Shift+P → "Developer: Reload Window")
# Settings → MCP → confirm gecko-txline appears
# Agent chat → verify skills/commands/agents load
```

**Smoke MCP call** (optional):

```bash
uvx --with mcp python -c "
import asyncio, json
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async def main():
    async with streamablehttp_client('https://mcp.geckovision.tech/txline/mcp') as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = await s.call_tool('getStatus', {})
            print(json.loads(out.content[0].text))

asyncio.run(main())
"
```

## Submission checklist

- [ ] Valid `skills/.cursor-plugin/plugin.json` (`name`: `gecko-surf`, kebab-case)
- [ ] All skills/agents/commands have `name` + `description` frontmatter
- [ ] `skills/assets/logo.svg` committed; `logo` field is a relative path
- [ ] `skills/README.md` documents install + bundled MCP
- [ ] No secrets in `mcp.json` (hosted txline only; colosseum is user-run)
- [ ] Local install verified under `~/.cursor/plugins/local/gecko-surf`
- [ ] Version aligned across manifests (`0.2.3`)

## Submit

1. Merge `feat/cursor-plugin-listing` → `main` and push.
2. Open [cursor.com/marketplace/publish](https://cursor.com/marketplace/publish) (signed in).
3. Submit repo URL: `https://github.com/GeckoVision/gecko-surf`
4. Pitch: *Install skills + 18 live TxLINE demo tools in one click — see Gecko comprehension before you self-host.*

Review is manual; each update is re-reviewed.

## Not bundled (document only)

| Surface | Why |
|---|---|
| `colosseum` MCP | Requires `COLOSSEUM_COPILOT_PAT` — user runs `uvx --from "gecko-surf[serve]" colosseum-mcp` |
| Live TxLINE data | Hosted demo is **recorded** ($0); live needs user's own subscription |

## Dual marketplace sync

Keep these in sync when bumping version or skills list:

- [`skills/.claude-plugin/plugin.json`](../skills/.claude-plugin/plugin.json)
- [`skills/.cursor-plugin/plugin.json`](../skills/.cursor-plugin/plugin.json)
- [`.claude-plugin/marketplace.json`](../.claude-plugin/marketplace.json)
- [`.cursor-plugin/marketplace.json`](../.cursor-plugin/marketplace.json)
- [`skills/mcp.json`](../skills/mcp.json) and [`skills/.mcp.json`](../skills/.mcp.json) (identical)
