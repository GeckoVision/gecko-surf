# The weekly take: "I only have USDG"

One unedited run of a real Claude Code session against the hosted MCP. The model's own
words, our real tools, numbers off the wire. Nothing here is scripted prose.

## Preconditions

| thing | state | who |
|---|---|---|
| `9fb981e` deployed to `mcp.geckovision.tech` | **required** — without it the peg blocks the route | founder |
| buyer `9cJbQKxx…` holds USDG and **no USDC** | 0.749699 USDG, 0 USDC, 0.005886677 SOL | done |
| Pegana readings | stale since 2026-08-26, and that is now a caveat rather than a block | done |
| waitlist count, cohort size | only the founder has these | founder |

Verify the deploy took before recording. This must say `route_found_peg_unverified`:

```bash
uv run python -c "
from gecko.pay_route import plan_payment_result
print(plan_payment_result({'buyer':'9cJbQKxxqCbumpoeb7YWC3QESzFD8LxpbHVAXrTUsPfh',
  'store':'geckocoffee','product':'Espresso','network':'mainnet'})['outcome'])"
```

## The prompt

Exactly this, typed once:

> I want to buy a coffee. I only have USDG. My wallet is
> `9cJbQKxxqCbumpoeb7YWC3QESzFD8LxpbHVAXrTUsPfh`

## Record

`--append-system-prompt` constrains LENGTH, never content. The model still decides what is
true; it is only told not to write an essay, because a wall of prose kills the take.

```bash
SD=/tmp/gecko-take && mkdir -p $SD && cd $SD
cat > mcp.json <<'JSON'
{"mcpServers":{"gecko-store":{"type":"http","url":"https://mcp.geckovision.tech/orquestra/mcp"}}}
JSON

asciinema rec --cols 92 --rows 26 --overwrite take.cast -c \
'claude -p "I want to buy a coffee. I only have USDG. My wallet is 9cJbQKxxqCbumpoeb7YWC3QESzFD8LxpbHVAXrTUsPfh" \
  --mcp-config mcp.json \
  --allowed-tools "mcp__gecko-store__list_stores,mcp__gecko-store__plan_payment,mcp__gecko-store__plan_swap,mcp__gecko-store__prepare_instruction,mcp__gecko-store__prepare_purchase,mcp__gecko-store__read_accounts,mcp__gecko-store__start" \
  --append-system-prompt "Be brief. Lead with what you did and the numbers. No headings, no bullet lists, under 150 words." \
  < /dev/null'
```

Run from a directory with no `.claude/settings.local.json`, and keep `< /dev/null`, or the
take opens on four permission warnings and a stdin warning instead of the answer.

**Scan the cast for secrets before rendering.** Must print 0:

```bash
grep -c "api-key\|helius\|mongodb+srv" take.cast
```

## Render

```bash
uv run --with pyte --with pillow python demo/kit/render_cast.py \
  $SD/take.cast docs/assets/weekly-usdg-coffee.mp4 --scale 2 \
  --brand "GECKO  •  TRY THE CALL BEFORE YOU MAKE IT" \
  --scene "Gecko — I only have USDG|the wallet, the menu, the mismatch" \
  --scene "Gecko — the route|a venue nobody chose" \
  --scene "Gecko — unsigned|Gecko never holds a key"
```

## Where the take ends, and the open decision

The gecko-store surface has **no signer**, so a headless agent physically cannot broadcast.
The run ends on unsigned bytes plus a receipt. That is the product boundary and it is a
fine ending: *"your wallet signs these, not me."*

To end on `predicted == charged` instead, the swap and the purchase have to land, which is
two mainnet broadcasts and needs the founder's authorisation naming the scope. The buyer
holds enough: the live plan wants 101,012 USDG of 749,699 held.

## The closing card

Four lines, from `private/demos/metrics-we-can-show.md`, which is a **proposal the founder
has not answered**. Do not label it approved. The numbers that are checkable today:

- 3 mainnet transactions on 2026-09-08, compute predicted before signing and exact on chain
- verified-exact record 18 of 18, with its denominator
- 2 design partners in conversation, 0 onboarded users on the app, and say the zero out loud
