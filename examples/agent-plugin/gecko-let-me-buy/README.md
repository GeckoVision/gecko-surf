# gecko-let-me-buy

An [Agent Plugin](https://agent-plugins.org) for the Let Me Buy storefront program on
Solana — hand-authored as the first one, so that the generator has a target it is known
to accept rather than a shape we assumed.

## What is in it

- **`skills/buy-a-product`** and **`skills/confirm-a-delivery`** — the two things a
  storefront is for, and the traps the IDL does not state: the two ATAs that differ only
  by owner, the missing authority guard on `make_purchase`, the argument-name mismatch on
  `mark_as_delivered`, and the receipts cap that loses orders silently.
- **`mcp.json`** — the servers that actually serve this program.
- **`tech.geckovision/`** — the graph and the score, **generated** from
  `gecko.program_graph`, never typed. Anything hand-written there would be the defect we
  measure in other people's surfaces.

## Why `mcp.json` points where it does

The refusal is that a plugin must **never point at us**: the provider's own server, or
nothing. This program has no MCP server of its own, and the surface that genuinely serves
it to agents today is Orquestra's public catalogue — so that is what is declared, and it is
a third party rather than the program's author. **A provider shipping their own plugin
should replace this entry with their own server.** It is stated here rather than left for a
reader to notice.

## What the extension namespace is for

Agent Plugins 1.0.0 defines reverse-domain extension namespaces for exactly this: things a
client may use and a client may ignore, without changing the portable core. So a client that
has never heard of Gecko loads two skills and an MCP server and works. A client that has
gets the graph, the provenance on every seed, and the score.

That is the whole composition pattern, and unlike FDL or A2A we did not have to propose a
field to get it.

## Status

Hand-authored, `plugin.json` validated against the published 1.0.0 schema. Not yet loaded
by a real client — that is the next step, and until it happens this is a candidate rather
than a proof.
