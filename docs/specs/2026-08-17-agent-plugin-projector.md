# `to_agent_plugin` — the build-kit a provider ships under their own name

**Status:** design, 2026-08-17, read from the 1.0.0 specification (`agent-plugins.org/llms.txt`,
1,242 lines) and from `google/agents-cli`, which ships a conforming plugin today.

---

## Why this is the right format to adopt

The blog post's own diagnosis is the part worth quoting, because it is the same shape as every
gap we work on:

> Agent Skills already gives reusable instructions and resources. MCP already connects agents
> to tools and services. Both are portable on their own. **What has not been portable is the
> box you put them in** — and that box is the thing every client had to invent for itself.

So the format is a *box*, deliberately. Version 1.0.0 is vendor-neutral, openly developed, with
a Technical Steering Committee of Core Maintainers from Amazon, Cursor, Microsoft, OpenAI and
Vercel. **We adopt it. We do not invent a format**, for the same reason we emit A2A rather than
our own card.

And it names its own limits rather than hiding them: 1.0 is *"a package format and nothing
more"* — no install machinery, no distribution protocol, no permission model, no sandboxing,
no trust or provenance verification, no user experience. Those omissions are listed openly in
its future considerations. **That is where we live.**

## The four layers, and where we already sit

The ecosystem diagram is four independent layers, and *"adopting one never obligates you to the
next"*:

| layer | the standard | us |
|---|---|---|
| **Find it** | Agentic Resource Discovery | `find_start`, and the REACHABLE axis of the score — we *measure* whether it can be found |
| **Describe it** | AI Catalog · A2A card | the graph, projected — `to_agent_card` |
| **Package it** | **Agent Plugins** | **`to_agent_plugin`** ← this document |
| **Run it** | MCP + Agent Skills | already shipped |

We are not competing with any of the four. We are the thing that makes what travels inside
them *correct*, and the thing that measures whether it is.

## The package, exactly

A directory with a required manifest and optional components in fixed locations. From
`google/agents-cli`, conforming today:

```
gecko-<provider>/
├── plugin.json          required, at the root
├── skills/
│   └── <capability>/
│       ├── SKILL.md
│       └── references/
├── mcp.json             optional
└── tech.geckovision/    a reverse-domain extension namespace
```

`plugin.json` has a **closed** schema — the only permitted top-level fields are `$schema`,
`name`, `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords`
and `extensions`. Anything else is reported and ignored. `$schema` must be the canonical
`https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`, and a client **must not fetch
a schema while loading**.

`mcp.json` carries `$schema` plus `mcpServers`, nothing else, with an **explicit `type` on
every server** so a client never infers a transport from the shape of a config object. Its
version must match `plugin.json`'s.

Two properties matter more than the field list:

* **The manifest cannot relocate or inline components.** No discovery path to configure, no
  precedence order to learn. That is what makes it portable, and it is a constraint we should
  welcome rather than route around.
* **Components fail independently.** An `mcp.json` server that will not start does not take
  the plugin's skills down with it. The client skips it, keeps loading, and reports it.

## What OUR plugin contains

```
plugin.json        name, version, the provider's own author/homepage/license
skills/            one per capability the graph found reachable — and the SKILL.md
                   description carries the same disambiguation our catalog does
mcp.json           points at the PROVIDER'S OWN MCP server. Never a proxy through us.
tech.geckovision/  the score, the graph, provenance, the rehearsal manifest
```

**The provider's name is on it.** Same as the surface page: we generate, they ship. A plugin
that says Gecko on the front is a plugin we are selling; a plugin that says their name on the
front is a plugin they are selling, and only one of those is the business.

### The one architectural point worth the whole document

**`extensions` is a sanctioned slot for exactly what the standard cannot express** — and
unlike FDL and unlike A2A, we do not have to propose a field upstream to use it.

FDL has no place for provenance, so we carry it in an `x-gecko` annex and propose the field.
A2A cannot express derivation order or prerequisites, so we propose there too. Agent Plugins
**already** defines reverse-domain extension namespaces for client-specific behaviour that must
not change the portable core. So:

* the portable core stays exactly conformant — a client that has never heard of us loads the
  skills and the MCP servers and works;
* `tech.geckovision` carries the score, the provenance per claim, the derivation order, the
  prerequisites and the rehearsal pointer;
* a client that *has* heard of us gets the verification layer, and one that has not loses
  nothing.

That is the composition pattern we keep looking for, handed to us by the spec.

## What it must refuse to do

* **Never point `mcp.json` at us.** The provider's server, or nothing. The moment we are in
  the data path we have broken the invariant that lets us ingest anything.
* **Never emit a skill for a capability the score could not reach.** A packaged skill an agent
  cannot find is the failure we measure in other people's surfaces.
* **Never claim a version we did not verify.** `plugin.json` `version` is the provider's
  release, not our run.
* **Do not build a distribution channel.** The spec leaves install and distribution out on
  purpose, and so should we — that is a marketplace by another name.

## What I have not established

* **Whether a provider wants this.** No provider has asked for a plugin. It is a plausible
  build-kit shape and an unvalidated one, exactly like the surface page.
* **What goes in a generated `SKILL.md`.** The frontmatter shape is clear from the real example
  — `name`, a trigger-phrase `description` including explicit negative routing (*"Do NOT use
  for X, use Y"*), and `metadata` with `requires.bins` / `requires.install`. What is not clear
  is how much of a good skill body can be generated from a graph versus authored. Our
  `agentnative.py` is the closest thing and it was written for a different output.
* **Whether skills or `mcp.json` comes first.** `agents-cli` ships skills only and no
  `mcp.json`. Components are independent, so we can ship the half we can generate honestly and
  add the other when it earns its place.

## First step, when this is picked up

Not the projector. **One plugin, by hand, for `let_me_buy`**, validated against the 1.0.0
schema and loaded by a real client — then generate the second one. The same order that worked
for FDL: prove the target accepts the artifact before writing the thing that emits it at
scale.
