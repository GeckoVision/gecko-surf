# Before ~250 people arrive (bootcamp opens 14 Sep, six days out)

The point of the cohort is not the teaching. It is the first time we will watch strangers
use Gecko. This plan is about being able to LEARN from that, and it is written from what
the telemetry actually contains today rather than what we hoped.

## The baseline, measured 2026-09-08

`gecko_events.surf_events`, 80,523 documents.

| event | count | reading |
|---|---|---|
| `surf.blocked` | 62,711 | 62,700 of them on `mcp.geckovision.tech`, all `waf.attack_probe` from `Mozilla/5.0` robots. The WAF is stopping scanners, not developers. |
| `surf.connect` | 11,841 | but only **590** are `client_kind: client`. 8,024 robot, 3,018 unknown. |
| `surf.list_tools` | 1,183 | |
| `surf.prepare` | 3,095 | |
| `surf.call` | 58 | |
| `surf.first_call_correct` | 65 | |

**590 real client connections have produced 58 calls.** That is the number the cohort
should move, and it is the first honest funnel we have had.

## The one thing that must exist by 14 Sep

**We cannot tell a cohort member from anyone else.** `tier` is populated on **0 of 80,523**
documents. `client_kind` separates robots from clients, which is why the numbers above are
readable at all, but nothing separates *our 250* from the other clients.

Without that, 250 people arrive, the counters move, and we cannot say by how much. Every
other item on this page is optional next to it.

Cheapest fix that needs no schema change: give the cohort **its own mount path** (a
bootcamp-scoped MCP URL). `surface_id` is already recorded on every event, so a distinct
surface makes cohort traffic separable by a field we already have. Second cheapest:
populate `tier` from the connecting client's name and tell the cohort what to set.

## The second thing: index before, not after

`surf_events` carries **no index beyond `_id` and no TTL**, on 80,523 documents. The
sibling collection `events` has four indexes including `ts_ttl_180d`. The small, tended
collection is the one nobody queries. After a cohort this is 300k+ rows and every funnel
query is a full scan. Index `ts`, `event`, `surface_id`, `session_id` now; decide the TTL
separately, because a TTL deletes data on a schedule and that is a retention decision, not
a performance one.

## Gecko arrives too late to learn from

Session 13 is **30 September**. The course ends **2 October**. Gecko gets two days of
cohort contact, at the end, when everyone is finishing a capstone.

If the goal is watching people use Gecko, it needs a touchpoint in **week 0 or 1**.
`modules/module-0/unit-11-mcp-data-apis-third-party` already teaches MCP against a
third-party data API and already mentions Gecko — that is the natural first contact, and it
needs no new session: connect to the hosted surface, call `list_stores`, read a real menu
off mainnet. Ten minutes, no wallet, no risk, and it turns Gecko from a week-3 payoff into
something they have touched before they need it.

## One inconsistency to fix in chapter 13

The lane table (updated 3 Sep) says the public lane is the live surface with real mainnet
data. The "Gecko safety boundary" paragraph below it still says the session uses an
"instructor-hosted Gecko MCP surface backed by a surfpool fork". Both cannot be true. The
lane table is current; the boundary paragraph is stale and will confuse the one student who
reads carefully — which is the student we want.

## What to show, and it happened today

On 2026-09-08 an agent completed a full mainnet purchase from one prompt: it read the menu,
asked PayBox which wallet it signs for, found the wallet short of the price, derived the
Orca route, prepared, had PayBox sign, submitted, then prepared and bought the coffee.
Two transactions, both verified on chain.

Three teaching moments in that run, and the second and third are better than the first:

1. **The loop closes.** One prompt, no integration code, and Gecko never held a key.
2. **It refused, unprompted.** In an earlier run the peg oracle was stale and it declined
   to route around its own guardrail: *"defeating it is the failure mode, not the
   workaround."* That is the lesson the course is actually about.
3. **It shelled out to raw RPC.** No Gecko tool answers "what does this wallet hold", so
   the agent ran `getTokenAccountsByOwner` by hand. A live demonstration of what one
   missing tool costs an agent — and a better exercise than any we could invent.

Session 13 stops before signatures and should keep stopping there. The signing half belongs
in a recording, not in 250 people's hands.

## Order

1. Cohort-separable telemetry (mount path or `tier`). Blocking.
2. Indexes on `surf_events`. Blocking at scale.
3. A ten-minute Gecko touchpoint in module 0.
4. Fix the stale safety-boundary paragraph in chapter 13.
5. Account role as an attribute, not a URL split (founder ruling, 2026-09-08).

See `docs/specs/2026-09-08-agent-ready-gaps-from-the-mainnet-run.md` for the tool gaps
themselves, and `docs/specs/2026-09-08-mongodb-inventory.md` for the storage picture.
