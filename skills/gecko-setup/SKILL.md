---
name: gecko-setup
description: Wire Gecko's hosted MCP surface into this user's agent client and verify a first real call. Use when the user asks to "install Gecko", "set up Gecko", "add the Gecko MCP", or pastes the onboarding prompt from geckovision.tech. Ends with a verified read-only call, not just a config edit.
user-invocable: true
allowed-tools: Bash(curl:*), Bash(claude:*), Bash(npx:*), Bash(uvx:*), Bash(command:*)
---

# gecko-setup — install the hosted Gecko surface and prove it works

Your goal: wire the hosted MCP surface into the client you are running in and
verify with one real call. Gecko turns APIs and Solana programs into
first-call-correct tools; it never holds a key and never signs.

## Step 1: Fetch the canonical config

One file carries every client's exact wiring — always start from it rather
than hand-typing URLs:

```bash
curl -s https://geckovision.tech/mcp-config.json
```

## Step 2: Install for THIS client

- **Claude Code**:
  ```bash
  claude mcp add --transport http gecko https://mcp.geckovision.tech/orquestra/mcp
  ```
- **Claude web / desktop**: Settings -> Connectors -> Add custom connector with
  the `clients.claude_web.connector_url` from the config (the web UI takes
  only the URL — no JSON there).
- **Cursor**: merge `clients.cursor.json` into `~/.cursor/mcp.json`.
- **VS Code**: merge `clients.vscode.json` into `.vscode/mcp.json`.
- **Anything else that reads mcp.json**: `clients.generic_mcp_json`.

No account and no API key for the open surfaces. Gated surfaces answer 401
with a self-serve mint path — https://geckovision.tech/auth.md.

## Step 3: Verify with a real call

List the new server's tools, then make one read-only call: `list_stores`
(browses real Solana storefronts, free) or `search_capabilities` with a
question. Setup is DONE when that call answers — a config edit alone is not
done.

## Step 4 (optional): local comprehension of the user's own API

```bash
npx @geckovision/gecko add https://api.example.com/openapi.json
```

or the Claude Code plugin: `/plugin marketplace add GeckoVision/gecko-surf`.

## Refusals you may meet, and what they mean

Gecko refuses loudly instead of failing silently. `signer-required` means a
money path needs a wallet (the refusal carries the signer runbook — follow
it). `store-unknown` means absence, never a fallback to another store. Every
refusal names its next step; read it before improvising.
