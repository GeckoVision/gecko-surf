---
name: gecko-setup
description: Connect Gecko's hosted MCP surface to this user's agent client, sign in, and verify a first real call. Use when the user asks to "install Gecko", "set up Gecko", "add the Gecko MCP", or pastes the onboarding prompt from geckovision.tech. Ends with a verified read-only call, not just a config edit.
user-invocable: true
allowed-tools: Bash(curl:*), Bash(claude:*), Bash(npx:*), Bash(uvx:*), Bash(command:*)
---

# gecko-setup — install the hosted Gecko surface and prove it works

Your goal: connect the hosted MCP surface to the client you are running in,
sign in, and verify with one real call. Gecko answers questions about
companies: it picks the data sources, shows the price before anything is
spent, and returns every value with its source, date and cost. It never holds
your keys and never signs.

## Step 1: Fetch the canonical config

One file carries every client's exact wiring — always start from it rather
than hand-typing URLs:

```bash
curl -s https://geckovision.tech/mcp-config.json
```

## Step 2: Install for THIS client

- **Claude Code**:
  ```bash
  claude mcp add --transport http gecko https://mcp.geckovision.tech/anyquery/mcp
  ```
- **Claude web / desktop**: Settings -> Connectors -> Add custom connector with
  the `clients.claude_web.connector_url` from the config (the web UI takes
  only the URL — no JSON there).
- **Cursor**: merge `clients.cursor.json` into `~/.cursor/mcp.json`.
- **VS Code**: merge `clients.vscode.json` into `.vscode/mcp.json`.
- **Anything else that reads mcp.json**: `clients.generic_mcp_json`.

## Step 3: Sign in once

This surface needs a credential. A client that supports MCP sign-in (OAuth)
opens a sign-in page on first connect: the user gives an email and types the
code that arrives. Raw HTTP or a client without OAuth mints a Gecko key
self-serve and sends `Authorization: Bearer <key>`; the steps are in
https://geckovision.tech/auth.md. Keep the key in the environment or the OS
keychain, never in a chat message.

## Step 4: Verify with a real call

List the new server's tools, then call `plan_query` with a plain question. It
returns the fields, the sources it would use and a quote, and spends nothing.
Setup is DONE when that call answers. A config edit alone is not done, and a
401 means no credential reached the server.

Paid tools (`run_query`, `cnpj_lookup`, `open_dataset_download`) quote first
and charge only on a second call with the quote id and `confirm: true`. Show
the user the quote and get a yes first.

Buying or swapping on Solana uses a different surface,
`https://mcp.geckovision.tech/orquestra/mcp` (open, no key): start with
`list_stores`, and follow https://geckovision.tech/agents.md for the money
path order.

## Step 5 (optional): local comprehension of the user's own API

```bash
npx @geckovision/gecko add https://api.example.com/openapi.json
```

or the Claude Code plugin: `/plugin marketplace add GeckoVision/gecko-surf`.

## Refusals you may meet, and what they mean

Gecko refuses loudly instead of failing silently. `signer-required` means a
money path needs a wallet (the refusal carries the signer runbook — follow
it). `store-unknown` means absence, never a fallback to another store. Every
refusal names its next step; read it before improvising.
