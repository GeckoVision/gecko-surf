# Explainer stills — where every number came from

Three 1477×803 stills for the video, in the visual grammar of the Arazzo Initiative's own
workflow slides (dark navy, cyan headings, panelled operations, mint callouts) rendered in
Gecko's palette. They sit between the terminal scenes: the cast shows *that* it works, a
still shows *why* it works.

```bash
./demo/kit/slides/render_slides.sh     # → 01_*.png 02_*.png 03_*.png
```

**Nothing on these slides is invented.** The kit's honesty rules apply to a still exactly
as they apply to a cast, and a still is more dangerous because it cannot be re-derived by
watching it run. Every figure below is traceable to a command anyone can repeat.

| Claim on a slide | Where it came from |
|---|---|
| `let_me_buy` instructions, account counts, arg names | `tests/fixtures/let_me_buy_idl.json` — byte-identical to the program's on-chain IDL account (`2nvFnEJA1HKueLpPaje8vR4tsU8rgRktx2dZhyjAfQZG`), verified 2026-08-13 |
| the `make_purchase` discriminator and pda seed block | the same IDL, verbatim |
| 9 accounts · 6 derived · 3 pinned program ids | `gecko.prepare_purchase` account plan |
| `sell_and_deliver`, its two steps, the `receipts` link | `find_start("buy water at the bar")` → `.starts[].chain` |
| the link's `basis` and `refuted_by` strings | quoted from that same payload, abridged with `…` where a line was cut |
| `NOT_EVALUATED` | that payload's own verdict — the chain is reported, never claimed executable |
| every `extracted` tag | `program.pda_origins` after the artifact-precedence ruling (#410) |
| 4,908 bytes | the size of the `jonasbar` receipts account on mainnet |

## The rules a still has to keep

- **Abridge, never paraphrase.** A quoted string may be cut with `…`; it may not be
  reworded into something the system never said.
- **Show the refusals.** Slide 1 carries the `REFUSED / unknown store` row and slide 3
  leads on `NOT_EVALUATED` for the same reason the casts show failures: a surface that
  only ever says yes tells a viewer nothing about when to trust it.
- **No competitor names.** Arazzo is a specification we compose with and is named as such;
  nothing here positions against a product.
- **Re-derive before reuse.** These are a snapshot. If a tier, a chain, or an account
  count changes, re-run the commands in the table above before the still ships again — a
  slide that has drifted from the code is a claim nobody is checking.

## Reading order in the video

1. **01_use_case** — after the naive call reverts: *this is the shape of the job.*
2. **02_idl_to_graph** — the transformation, over the `bytes → gecko_graph` beat.
3. **03_what_the_graph_carries** — before the settle: what makes the plan trustworthy,
   including the field that admits what was not verified.
