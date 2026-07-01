# Step 3 — Serve the full surface over MCP, one-click add

**Status: Live.** Streamable-HTTP MCP + one-click add + SSRF guard shipped in
`gecko-surf`.

Comprehension (step 1) produced first-call-correct tools. This step serves them to
agents over MCP and prints the one-click add so a human can connect an external
agent in a single step.

## Serve it

```bash
gecko https://api.example.com/openapi.json          # comprehend + serve (== gecko serve <spec>)
```

`gecko serve` stands up a **Streamable-HTTP** MCP server exposing:
- one question-shaped tool per comprehended operation (auth hidden), and
- the synthetic `search_capabilities` tool (intent → ranked endpoints).

It prints the comprehension summary (operations ingested, tools surfaced, auth-gated
hidden), the MCP URL, and a **one-click add** for each supported host app.

## The one-click add strings

The serve banner prints, for a server named `example` at
`https://mcp.example.com/mcp`:

- **Claude Code** (CLI line):
  ```bash
  claude mcp add --transport http example https://mcp.example.com/mcp
  ```
- **Cursor** — a `cursor://anysphere.cursor-deeplink/mcp/install?...` deeplink
  (base64 of the server config).
- **VS Code** — a `vscode:mcp/install?...` deeplink (url-encoded
  `{name, type:"http", url}`).

Paste the breadcrumb link from [artifacts.md](artifacts.md) into your `llms.txt` and
an agent can go from "find the API" to "call the API correctly" in one hop.

## Security (enforced by the engine)

- **SSRF guard.** Every URL the engine fetches (the spec, any upstream) is validated
  first — private IP ranges, loopback, link-local, and non-http / `file://` schemes
  are blocked. Ingested spec/doc content is treated as untrusted input.
- **Auth invisible to the agent.** Tool defs never expose auth headers; credentials
  are injected at call time and redacted from logs and errors.
- **Control-plane only.** The server stores the surface + correctness metadata,
  never response payloads, user data, or secrets.

## Coexistence

`gecko serve` stands up a **new** MCP for the full surface. It does **not** modify,
proxy, or shut down the provider's existing MCP — the two run side by side. That's
the point: [aggregate-not-replace.md](aggregate-not-replace.md).

Next: [discoverable.md](discoverable.md) — make the served MCP findable without
becoming a catalog.
