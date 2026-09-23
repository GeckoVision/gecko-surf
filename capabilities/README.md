# Capability cards

One file per surface. Two things written by a human, everything else derived by the engine:

- `[bindings]` — the values a caller genuinely has to choose: the wallet, the store, the
  product, the numbers. The graph cannot know them and does not pretend to.
- `[intents]` — the sentence a person would actually say. *"get me an espresso"* is not in
  an IDL, and no derivation recovers it.

Check a card before you hand it in. Offline, free, and it signs nothing:

```bash
uv run gecko capability check capabilities/letmebuy.toml
uv run gecko capability check capabilities/letmebuy.toml --json   # the report as data
```

## What the check decides

| gate | what it means |
|---|---|
| the graph builds | from the IDL beside the card |
| every instruction derives **or refuses by name** | a raised refusal is fine; an **undeclared** one is not |
| every `[[declared_gap]]` still fires | a gap that stopped firing means the card describes a surface that moved |
| nothing signs | the check refuses to start beside a live signing key |

Routing is **reported, never rejected**. A sentence our ranker misses is a fact about the
ranker, and your card is how we found out.

## What you get, said plainly

**You will not be paid for this, and we are not going to pretend otherwise.** Gecko takes
no cut of anything and runs no marketplace, so there is no revenue to share. Traffic to
your card may be zero for a long time, and it may stay zero.

What you do get:

1. **A report with a number in it**, produced by a run and not by you: "5 of 8 instructions
   derive, 3 declared gaps, 3 of 5 sentences route". That is a better interview answer than
   "I did a bootcamp".
2. **Your name on the card**, in the file and in git. You are its owner.
3. **The card served** at `mcp.geckovision.tech` if a human reviews it and marks it
   `verified`. `tier` is `community` in every card you write; a file never promotes itself.

`letmebuy.toml` is ours, and it is the one to read first.
