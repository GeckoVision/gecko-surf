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

## The signer, wired (2026-09-08)

There are TWO PayBox paths and they are not interchangeable. The claude.ai **connector**
signs through an in-chat window that cannot render headlessly, so a request there parks at
`pending_signature` forever — that is what the first take hit, and it is a property of the
path, not a fault. The **SDK** path signs in-process in about a second and is what landed
mainnet tx #24, #25 and #26.

Until now no agent could reach the working one. `gecko-app/scripts/paybox-mcp.mjs` is a
local stdio MCP server that gives it a door: `paybox_wallet` returns the address PayBox
will sign for, and `paybox_sign_solana` signs unsigned bytes and returns them. It **never
broadcasts**, and it refuses any transaction whose fee payer is not the PayBox wallet —
the same rule `gecko/signer.py` applies locally.

Proven end to end on 2026-09-08: prepared a real Whirlpool swap (46,197 CU, binding
`3f5110af…` exact), signed it through the server, and `verify_signed` came back
`verified: True, binding_matches: True` — byte-identical over its message to the one the
receipt attested. The wrong-wallet refusal was tested too, and fires.

Wallet `GpaLFMwQWh2xuBkMQGKmcYT5A1WgYJekofu6DJjp8W9c`, funded 2026-09-08 with 0.3 USDG
plus the 0.059352 USDC and 0.022895 SOL it already held. Both token accounts exist, so no
rent surprise.

To record the take with it, add the server alongside gecko-store and allow its two tools.
It needs `PAYBOX_TOKEN` and `PAYBOX_SIGNIN_KEY` in its environment, from
`gecko-app/.env.local`, and they must never reach the cast:

```json
{"mcpServers":{
  "gecko-store":{"type":"http","url":"https://mcp.geckovision.tech/orquestra/mcp"},
  "paybox":{"command":"node","args":["scripts/paybox-mcp.mjs"],"cwd":"../gecko-app"}}}
```

**Residual, stated rather than hidden:** the signer does not re-derive the binding, so it
cannot tell a prepared transaction from any other well-formed one with the same fee payer.
The binding check lives on the Gecko side, before and after. Do not move it into the signer
and call that a gate.

## Where the take ends, and the open decision

The gecko-store surface has **no signer**, so a headless agent physically cannot broadcast.
The run ends on unsigned bytes plus a receipt. That is the product boundary and it is a
fine ending: *"your wallet signs these, not me."*

To end on `predicted == charged` instead, the swap and the purchase have to land, which is
two mainnet broadcasts and needs the founder's authorisation naming the scope. The buyer
holds enough: the live plan wants 101,012 USDG of 749,699 held.

## The closing card

Four lines, same shape every week. The real state, founder-confirmed 2026-09-08: **one**
waitlist signup, and we do not know whether she is even an API provider. Two design
partners, both from conversations the founder started himself. **Neither came from the
waitlist.**

    11 mainnet transactions today. Compute predicted before signing, exact on chain.
    Verified-exact record: 20 of 20.
    2 design partners in conversation, neither from the waitlist. Onboarding starts this week.
    Waitlist: 1. Users on the app: 0.

The ratio is the point and it is worth saying rather than hiding: one signup, two partners,
and the partners came from talking to people. That is a finding about how this actually
grows, and it is more useful to a build-in-public audience than any number we could dress
up. It also sets a real target for next week.

Rules that govern those four lines:

- **"Onboarding starts" is a plan, not a result.** Future tense on screen, and it stays
  future tense until someone has actually finished onboarding. Next week it becomes a
  number, which is the whole point of a weekly cadence.
- **Say the zero out loud.** A build-in-public audience forgives a zero and never forgives
  a number that turns out to be crawlers. Roughly 94% of sessions on our hosted surface are
  indexers, so we of all people know.
- **Design partners are not customers.** Willingness to pay is unvalidated.
- **The compute record carries its denominator.** 18 of 18, never 53 of 53: 15 rows have no
  recorded prediction and the rest are predicted but not re-read from the chain.
- Cohort size is still unconfirmed by the founder, so it is not on the card.
- **"11 today" and "20 of 20 verified" count different things, and both are true.** Eleven
  landed, and all eleven were predicted before signing and exact on chain (re-verified by
  signature on 2026-09-08). Five were written to `docs/mainnet-ledger.jsonl` by our own
  scripts with a recorded prediction, and those five take the verified record from 15 to
  20. The other six were landed by the AGENT through the hosted surface, which does not
  write our ledger — they matched when read back by hand, but carry no recorded
  prediction, so they do not join the verified count. That is the
  post-broadcast verification gap in
  `docs/specs/2026-09-08-agent-ready-gaps-from-the-mainnet-run.md`, demonstrated the same
  day it was written.
- **Never name a design partner on screen without asking them first.** One of the two is
  waiting to onboard and has not agreed to be named publicly. "2 design partners in
  conversation" carries the same information and needs nobody's permission. Naming an
  un-onboarded partner in a public update is how a relationship gets spent.
- **One waitlist signup is not one lead.** We do not know she is an API provider. Report
  the count, never a qualification we have not made.
