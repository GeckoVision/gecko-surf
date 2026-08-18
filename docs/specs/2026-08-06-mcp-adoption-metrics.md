# MCP Adoption Metrics — design (2026-08-06)

**Owner:** data-engineer (corpus storage + retrieval lane)
**Status:** design only — nothing implemented yet.
**Purpose:** a repeatable job that produces *defensible* adoption numbers for the MCP
surface (`mcp.geckovision.tech`, `@geckovision/gecko`, `gecko-surf`), so the founder can
answer "is anyone using this?" without the answer collapsing under one follow-up question.

**Design stance:** under-claim. Every number in the output carries its denominator, its
population, and a provenance tag. The report prints its own blind spots *before* anyone
asks. A number we cannot defend is not printed at all — not printed small, not printed
grey. Not printed.

---

## 0. The one-paragraph verdict

We can measure **installs** for the local channel and **usage** for the hosted channel,
and **they are two different populations that cannot be joined.** The primary documented
onboarding path (`gecko add` → `claude mcp add` → local `gecko serve` over stdio) emits
**exactly one** event in its entire life — the onboard ping — and then goes dark forever,
because the local process has no `MONGODB_URI` and the sink is a no-op without one
(`gecko/events.py:409`). Everything we know about *usage* comes from clients that connect
to the **hosted** surface, where ~94% of sessions are indexers. Any single "adoption
number" that spans both channels is fabricated. The report must be two reports.

---

## 1. What is ACTUALLY captured today

### 1.1 The store

* MongoDB, database **`gecko_events`**, collection **`surf_events`**
  (`gecko/events.py:168-169` — single source of truth; every reader imports
  `EVENTS_DB` / `EVENTS_COLLECTION` from there, never a literal).
* Documents are lean: unset fields are dropped at write (`events.to_doc`). A reader must
  treat every field as optional.
* `ts` is an **int, epoch milliseconds**. There is no date/day field; day bucketing is a
  read-time concern (UTC).
* No indexes are created by our code. At ~10k docs/month this is fine; revisit at ~10M.
* Writes only happen when `MONGODB_URI` is set **and** `pymongo` is installed **and**
  `GECKO_TELEMETRY` is not off-like. Hosted task gets the URI from SSM
  (`infra/ecs-stack.yml:414-418`).

### 1.2 The event vocabulary and who emits what

Closed set in `gecko/events.py:57-67`. Where each is emitted, and — critically — which
correlation fields ride along:

| Event | Emitted by | `session_id` | `client` / `user_agent` | `install_id` | Notes |
|---|---|---|---|---|---|
| `surf.onboard` | hosted `POST /events/onboard` (`http_server.py:1230`), fed by `gecko add` / first-run `gecko serve` (`onboard.py:516,573`) | no | no | **yes** (hex32) | `surface_id` = API host, plus `version`, `client_os`, `mode` ∈ recorded/live/serve |
| `surf.connect` | hosted ASGI init-capture (`http_server.py:291,321`) | **yes** | **yes** | only if client echoed `X-Gecko-Install-Id` | 2xx `initialize` only |
| `surf.connect_failed` | same | no | **yes** | no | 4xx init **and** any 4xx non-init POST (crawler probes) |
| `surf.list_tools` | `McpSurface.list_tools` (`mcp_server.py:200`) | yes | **yes** | no | the connect→call bridge |
| `surf.search` | `search_capabilities` (`mcp_server.py:377`) | yes | **no** | no | plus `k` |
| `surf.call` | `McpSurface.call` (`mcp_server.py:479`) | yes | **no** | no | `plane="surface"`, plus `tool_name`, `mode` |
| `surf.blocked` | gate / honeypot (`mcp_server.py:419,438,453`) | yes | no | no | `decision`, `reasons`, `score` |
| `surf.prepare` | engine (`client.py:478`) | **no** | no | only if a local CLI declared one | `plane="engine"` |
| `surf.first_call_correct` | engine (`capture.py:55`) | **no** | no | only if a local CLI declared one | `plane="engine"`, `ok`, `error_class`, `latency_ms`, `source` |

Field allowlist: `gecko/events.py:103-126`. Control-plane invariant #1 holds by
construction — there is no field that can carry a body, an arg value, or a secret, and
`error_class` / `source` / `client_kind` / `plane` are gated to closed sets. **Nothing in
this design requires a new value-bearing field.**

### 1.3 The two existing readers

* `scripts/adoption.py` — PyPI downloads + a flat event-kind histogram + unique/repeat
  surfaces + an observed-only FCC rate. **No robot filter. No self filter. No session
  dedup.** Its `total_events` and `fcc_rate` are dominated by our own machines.
* `scripts/funnel.py` — the good one: per-surface `connects → activated → returned` over
  distinct sessions, excluding self clients (`GECKO_SELF_CLIENTS`) and robots via
  `uaclass.reclassify_client` on **read**. Gaps: ignores `surf.list_tools` entirely;
  `_CALL_EVENTS` lumps `surf.search` with `surf.call`; calls "retention" something that is
  really in-session depth; no install-scoped or cross-day view; no unknown-kind bucket.

### 1.4 Classification logic that already exists — REUSE, do not reinvent

`gecko/uaclass.py`:
* `classify_client(user_agent, client) -> robot|client|unknown` — ordered rules: named
  agent product wins, then robot substrings, then MCP-client substrings, else unknown.
* `reclassify_client(row)` — **re-derives on read** from raw `user_agent` + `client`,
  falling back to the stored `client_kind` only when both raw fields are absent. Stored
  `client_kind` is frozen at emit time and is **stale** for historical rows (an
  `mcp-scraper` connect stored before `scraper` became a robot marker reads as `client`).
  Every count in the new job goes through `reclassify_client`. No exceptions.

### 1.5 What is MISSING (the honest gap list)

1. **Local stdio usage: zero visibility.** The `gecko add` flow wires a *local* `gecko
   serve`. No `MONGODB_URI` → the sink is `None` → no event ever leaves. We see the
   onboard ping and nothing else, ever. This is not a bug to fix casually: pointing local
   installs at our Mongo is a default-on data-collection decision the founder has not
   ratified (and `telemetry.py`'s docstring says so explicitly). **Treat as permanent for
   this report.**
2. **`surf.first_call_correct` has no `session_id` and no `client`.** It is engine-plane.
   It therefore cannot be de-robotted, de-selfed, or attributed to a session. It can
   never be used as an adoption metric. (It is a fine *comprehension* metric on our own
   benchmark specs — different report, different lane: `ai-ml-engineer`.)
3. **No identity on hosted connects from third-party clients.** `X-Gecko-Install-Id` is
   sent only by `gecko connect` (`connect.py:104-120`). Claude Code or Cursor pointed
   directly at the URL gives us a per-session id and a client label — nothing stable
   across sessions or days.
4. **Two incompatible `install_id` implementations (real bug).**
   * `gecko/telemetry.py:197,227` → file `~/.gecko/install-id`, value `str(uuid.uuid4())`
     (36 chars, **dashed**). This is what `gecko/cli.py:1924` stamps onto local events.
   * `gecko/onboard.py:394,413` → file `~/.gecko/install_id`, value `uuid4().hex`
     (32 chars, **no dashes**). This is what the onboard ping and the connect header carry,
     and `http_server.py:151` `_INSTALL_ID_RE = ^[0-9a-f]{32}$` accepts **only** this form.

   One machine therefore has **two identities**, in two files whose names differ by one
   character. A dashed id in an event can never be joined to that machine's onboard ping,
   and any distinct-install count that unions event sources double-counts. The events
   allowlist shape-gate does not catch it (both are safe labels). **Fix before the first
   report ships**, or scope every install count to a single source and say so.
5. **`install_id` counts machine-instances, not people.** A fresh CI container or a
   sandboxed agent shell has a fresh `HOME` → a fresh id. The historical "50 installs, all
   linux, narrow version band" is containers.
6. **No landing/discovery measurement.** The hosted `/` index route emits nothing. There
   is no joinable top-of-funnel.
7. **npm downloads are not fetched at all** (`adoption.py` does PyPI only) — and see §6 for
   why the npm number is structurally inflated by our own launcher.
8. **`/events/onboard` is unauthenticated and forgeable** (`http_server.py:1230`, always
   204). Low risk today; it means onboard counts are *indicative*, not auditable. Say so
   once in the report footer.
9. **No churn / uninstall signal.** By design (once-per-install+surface ping marker,
   `onboard.py:468-488`), an install appears in `surf.onboard` at most once per surface —
   ever. Retention can never be derived from onboard events. This is correct behaviour and
   a permanent measurement limit.

---

## 2. The funnel — stages, exact fields, and what we cannot measure

Two channels. They are reported side by side and **never divided into each other**.

### 2.1 HOSTED channel (`mcp.geckovision.tech`) — the only channel with usage data

| # | Stage | Definition (exact) | Measurable? |
|---|---|---|---|
| H0 | Discovered | someone saw the surface exists | **NO.** No landing telemetry, nothing joinable. Downloads are a *different* population (§3.4). |
| H1 | Connected | distinct `session_id` where `event == "surf.connect"` (emitted only on a 2xx `initialize`) | **YES** |
| H2 | Enumerated | H1 sessions that also have `event == "surf.list_tools"` | **YES** — and this is the sharpest robot signature (connect → enumerate → leave). `funnel.py` ignores it today. |
| H3 | Searched | H1 sessions with `event == "surf.search"` | **YES** — intent expressed. Keep it *separate* from H4; `funnel.py` currently merges them and inflates activation. |
| H4 | Called | H1 sessions with `event == "surf.call"` (`plane == "surface"`) | **YES** — this is activation. |
| H5 | Call succeeded | the H4 call returned 2xx | **NO.** `surf.first_call_correct` carries no `session_id`. Unmeasurable per session until a session id is threaded into `capture_outcome`. |
| H6 | Repeat use on a later day | same identity called on ≥2 distinct UTC days | **PARTIAL** — only for sessions whose connect carried `install_id` (i.e. `gecko connect` users). See §4. |

Session-level joins are legitimate: `surf.call` / `surf.search` / `surf.list_tools` all
carry the same opaque `session_id` the transport assigned at `surf.connect`.

**Handling calls with no in-window connect:** count them in a separate `unattributed`
line. They are real activity (an honest floor) but carry no classification, so they must
never be folded into the human funnel silently. `funnel.py` today counts them toward
`activated`, which is a small over-count.

**Handling `session_id`-less call events:** excluded from every per-session number, and
their count is printed. (Already the behaviour in `funnel.py`; keep it and surface it.)

### 2.2 LOCAL channel (`gecko add` / `gecko serve` → stdio)

| # | Stage | Definition | Measurable? |
|---|---|---|---|
| L1 | Onboarded | distinct `install_id` in `surf.onboard`, split by `mode` (recorded / live / serve) and by `surface_id` (the API host they pointed us at) | **YES**, with the container caveat (§1.5-5) |
| L2 | Connected / enumerated / called / repeated | — | **NO. Nothing. Ever.** The local process never emits. |

The single most useful thing L1 gives us is **which APIs people point us at** — a
`surface_id` histogram that is *not* our demo surfaces. That is a real, defensible signal
about ICP fit even though we cannot see a single call. Lead with it for the local channel;
it is the one place where "is anyone using this?" gets an honest, non-zero answer.

### 2.3 Stages we CANNOT currently measure — printed in every report

* H0 discovered → connected (no top-of-funnel).
* H5 first call succeeded, per session.
* Local-channel anything past install.
* Any cross-channel rate (local install → hosted call).
* Person-level anything. We have no identity and are not adding one for metrics.

---

## 3. Honest denominators and the human-vs-indexer split rule

### 3.1 The split rule (one rule, applied on read, every time)

```
kind        := uaclass.reclassify_client(connect_row)      # NEVER the stored client_kind
is_self     := connect_row.client matches GECKO_SELF_CLIENTS (case-insensitive name prefix)
               OR connect_row.install_id ∈ GECKO_SELF_INSTALL_IDS   # NEW — see below
excluded    := is_self OR kind == "robot"
```

Classification lives on the **connect** row and propagates to every event sharing that
`session_id`. Two additions to what `funnel.py` does today:

* **`GECKO_SELF_INSTALL_IDS`** (new env, comma-separated): the founder's machines and CI.
  Required because the founder's local `.env` has `MONGODB_URI`, so his own CLI runs write
  into the production collection. Without this, dogfooding is indistinguishable from
  adoption — which is exactly the "collapses under one question" failure.
* **`unknown` is its own bucket, never merged.** Given the measured base rate
  (~94% robots), an unclassified client is far more likely a bot than a human. Default:
  exclude `unknown` from the human funnel and print the "if every unknown were human"
  upper bound as an explicit range. `4 humans (upper bound 11 if every unclassified client
  were real)` is defensible. `11 humans` is not.

### 3.2 The denominators we publish

| Name | Definition | Why it is honest |
|---|---|---|
| `sessions_raw` | distinct `session_id` with a 2xx connect | the unfiltered top; shown only as the first row of the exclusion waterfall |
| `sessions_robot` | excluded by `reclassify_client == "robot"` | |
| `sessions_self` | excluded as ours | |
| `sessions_unknown` | neither robot nor client marker | shown, never merged |
| **`sessions_external`** | `raw − robot − self − unknown` | **the only denominator any published rate may use** |
| `products_external` | distinct **collapsed** client product = `client.split("/")[0].lower()` | `claude-code/2.1.207 … /2.1.215` is ONE product, not six |
| `installs_onboard` | distinct `install_id` in `surf.onboard` | local channel only; never a denominator for hosted rates |

The report renders the exclusion as a **waterfall**, not a footnote:

```
sessions (raw)            2009
  − indexers/crawlers    -1887
  − our own clients        -87
  − unclassified           -31
  = external sessions        4
```

Anyone who asks "how do you know they're not bots?" gets the answer before they ask.

### 3.3 The small-n rule (non-negotiable)

**No percentage is printed where the denominator < 30.** Below 30 the report prints
`k of n` only. `2 of 8 sessions came back` survives scrutiny; `25% retention` does not.
This one rule prevents most of the ways these numbers explode.

### 3.4 Downloads are context, not funnel

PyPI (`gecko-surf`) and npm (`@geckovision/gecko`) counts go in a clearly-separated
`DISTRIBUTION` block, labelled *"downloads — includes mirrors, CI, and our own launcher;
not people, not installs."* They are never a funnel stage and never a denominator.

---

## 4. Retention — the definition I would defend

**What `funnel.py` calls "returned" today (≥2 tool calls in one session) is not
retention.** It is in-session depth. Rename it `multi_call_sessions` and say it as
*"of the sessions that called a tool, K made more than one call in that session."*

### The defended definition

> **D7 install-scoped return-to-call:** an `install_id` that produced at least one
> `surf.call` on some UTC day, and at least one `surf.call` on a **different, later** UTC
> day within 7 days. Reported as `k of n installs`, with n = installs that ever called.

Notes on why this one holds up:

* **Identity = `install_id`, resolved via the session join** (`surf.call.session_id` →
  that session's `surf.connect.install_id`). No new field, no new event, implementable
  against today's data.
* It is scoped to the population that *can* carry an install id — `gecko connect` users.
  The report states that population size explicitly. A retention number over a population
  of 3 is reported as `1 of 3`, never as 33%.
* **Cross-request/session reuse is required, not optional.** The identity is a permanent,
  machine-scoped id; the retention query must never be gated behind a session id, or the
  metric silently reads zero. (This is the exact "permanent corpus, per-request scoping"
  trap; retention is a cross-session join by construction.)
* UTC calendar days, not rolling 24h — so "came back the next morning" counts and a
  20-minute burst does not.

### The fallback we may state when D7's population is empty

> **Surface-level return:** for surface X, the number of distinct UTC days on which at
> least one external session made a call. `txline: called on 3 separate days by 2 distinct
> client products.`

This is a weaker claim about *the surface* and not about *a user*, and it must be said
that way. It is still worth saying: it distinguishes "one afternoon of us testing" from
"traffic that recurs."

### What we must not say

* "Retention" derived from repeat *connects*. Clients re-`initialize` constantly
  (restarts, auto-updates, reconnects); repeat connects measure client behaviour, not
  human intent.
* Any retention derived from `surf.onboard` — the ping fires **once per install+surface,
  ever**, by design. There is no second data point to retain.

---

## 5. Where the job lives, and its output shape

### 5.1 Placement

Per the repo rule (logic in the package, scripts thin):

* **New module `gecko/adoption.py`** — pure, read-only aggregation functions over a list
  of event dicts. No network, no Mongo, no printing. Frozen dataclasses out
  (`ChannelFunnel`, `ExclusionWaterfall`, `InstallSummary`, `RetentionSummary`,
  `AdoptionReport`). Fully testable offline from a JSONL fixture — the Pattern B
  requirement: the falsifiable offline simulation ships *first*, the live Mongo read is the
  final check.
* **`scripts/adoption.py` becomes the thin runner** — argparse, loads from Mongo or
  `--jsonl`, calls the package, renders. It absorbs `scripts/funnel.py` (one report, one
  truth); keep `funnel.py` as a 5-line deprecation shim for one release.
* **NOT a `gecko` subcommand.** It requires `MONGODB_URI` and reads *our* private
  collection. Shipping it in the public CLI advertises a capability an OSS user cannot
  use and implies we collect more from them than we do. Keep the public CLI clean.
* **Classification stays in `gecko/uaclass.py`.** The new module imports it. No second
  classifier, ever.

### 5.2 Invocation

```bash
uv run python scripts/adoption.py --days 30                    # text, for the terminal
uv run python scripts/adoption.py --days 30 --format md        # paste into an update
uv run python scripts/adoption.py --days 30 --format json \
    --snapshot private/adoption/2026-08-06.json                # immutable weekly snapshot
uv run python scripts/adoption.py --jsonl fixture.jsonl        # offline, no Mongo
```

**Weekly snapshots into `private/adoption/YYYY-MM-DD.json`** (gitignored) matter more than
they look: they freeze the window so week-over-week comparisons are real, instead of
recomputed from a sliding window every time someone asks. "Flat for three weeks" is only
sayable if the three weeks were each written down.

### 5.3 Output shape

Four blocks, always in this order, always all four — including the last one.

```
surfcall adoption — 2026-08-06 · window 2026-07-07 → 2026-08-06 (30d, UTC)
================================================================================

DISTRIBUTION  (downloads ≠ people; includes mirrors, CI, and our own npx launcher)
  PyPI gecko-surf         last 30d   ####
  npm  @geckovision/gecko last 30d   ####     ⚠ inflated: our wiring registers
                                              `npx -y @geckovision/gecko`, which
                                              re-resolves the package on every spawn

LOCAL CHANNEL  (gecko add / gecko serve → stdio)   [MEASURED: installs only]
  distinct installs             ##      (machine-instances; a fresh CI container = +1)
    · by mode        recorded ## · live ## · serve ##
    · by OS          linux ## · darwin ## · windows ##
    · suspected CI   ##        (linux + same version + same-day cluster) — disclosed,
                               not silently dropped
  APIs people pointed us at     (surface_id histogram, our own demo surfaces separated)
    api.example.com     ##
    ...
  ⛔ usage past install is NOT MEASURABLE on this channel (no events leave the machine)

HOSTED CHANNEL  (mcp.geckovision.tech)             [MEASURED: sessions + calls]
  exclusion waterfall
    sessions (raw)              ####
      − indexers/crawlers      -####
      − our own clients          -##
      − unclassified             -##
      = external sessions          #

  funnel (distinct external sessions)
    connected                     #
    enumerated tools              #     (list_tools)
    searched                      #     (search_capabilities)
    CALLED A TOOL                 #     ← activation
    unattributed calls            #     (call with no in-window connect — a floor)
  rates: suppressed (n < 30) — reported as k of n

  distinct external client products   #   (versions collapsed)
  per-surface breakdown …

  retention (D7 install-scoped, population = installs that ever called: n=#)
    k of n installs called again on a later day within 7d
  ⛔ per-session call SUCCESS is NOT MEASURABLE (first_call_correct carries no session)

WHAT THIS REPORT CANNOT TELL YOU
  · how many people saw the surface (no top-of-funnel telemetry)
  · whether a local-install user ever made a call (channel is dark by design)
  · who anyone is (no identity captured, deliberately)
  · whether a call succeeded, per session
  · onboard pings are unauthenticated and therefore indicative, not auditable
```

Every measured line carries a provenance tag internally (`MEASURED` / `PARTIAL` /
`NOT MEASURABLE`) so the JSON snapshot is self-describing and a future dashboard cannot
accidentally promote a `PARTIAL` number to a headline.

### 5.4 Control-plane conformance

The job reads only the fields `gecko/events.py` was allowed to write, aggregates them into
counts and categories, and writes counts and categories. No payload, no arg value, no
secret, no PII — because none was ever stored. The snapshot file contains counts,
category labels, and opaque ids only. No new field is introduced anywhere in this design.

---

## 6. Numbers that would be MISLEADING to show — and why

Ordered by how badly each one blows up when questioned.

1. **"N installs" from PyPI/npm downloads.** Mirrors, CI, and — the self-inflicted one —
   `onboard._serve_launcher()` registers wired servers as `npx -y @geckovision/gecko`,
   which re-resolves the package on **every client spawn**. Our own architecture inflates
   the npm number. One informed question destroys it.
2. **"2,000 clients connected."** ~94% are indexers/crawlers. Any connect count without
   the exclusion waterfall is a lie by omission.
3. **Distinct `client` strings as distinct users.** The string embeds the version;
   `claude-code` alone reads as six "clients" across a point-release band. Always collapse
   at `/`.
4. **Stored `client_kind` used raw.** Frozen at emit; historical rows carry stale labels
   and over-report humans. Always `reclassify_client` on read.
5. **`total_events`** (what `scripts/adoption.py` prints today). One session emits many
   events; one crawler moves it more than ten humans. Never a headline.
6. **The first-call-correct rate as an adoption/usage metric.** `surf.first_call_correct`
   is engine-plane, has no session or client, cannot be de-robotted or de-selfed, and in
   practice is dominated by our own machines and CI. It is a comprehension metric measured
   on our own specs. Presenting it as "users get it right first try" is the single most
   dangerous available claim.
7. **Any cross-channel rate** — e.g. hosted calls ÷ local onboards. Different populations
   with zero overlap guarantee. Pure fabrication.
8. **`install_id` counts as "adopters."** Machine-instances; a fresh container is a new
   install; and today two different id schemes coexist (§1.5-4), so a union double-counts.
9. **Any percentage with n < 30** — "50% retention" from 4 activated / 2 returned is two
   sessions. Print `2 of 4`.
10. **Totals summed across surfaces** where our own demo surfaces (`gecko`, `txline`,
    `jupiter`, `jito`) dominate. Always split "our demo surfaces" from "a surface a user
    brought."
11. **`surf.search` counted as activation.** Searching is intent, calling is activation.
    `funnel.py`'s `_CALL_EVENTS` currently merges them; separate them.
12. **Repeat connects as retention.** Clients reconnect on restart and auto-update. It
    measures the client, not the human.
13. **"Sessions" as "people."** One person can open dozens per day. If a per-person claim
    is needed, it must ride `install_id`, and then the population is tiny — say the tiny
    number.

---

## 7. Prerequisites before the first report is shown to anyone

1. **Fix the dual `install_id`** (`telemetry.py` dashed at `~/.gecko/install-id` vs
   `onboard.py` hex at `~/.gecko/install_id`). One id, one file, one shape — the hex32 form,
   because the server regex and the connect header already require it. Until then, scope
   every install count to a single source and label it.
2. **Populate `GECKO_SELF_INSTALL_IDS`** with the founder's machines and CI, or stop
   pointing local dev at the production `MONGODB_URI`. Dogfooding currently writes into the
   same collection the report reads.
3. **Build the JSONL fixture first** (a synthetic mixed crawler/human/self event file) and
   make every aggregate falsifiable offline before the first Mongo read. Pattern B.

## 8. Deliberately deferred

* **Threading `session_id` into `capture_outcome`** so H5 (per-session call success)
  becomes measurable. It is a small change and it is the highest-value single addition —
  but it touches the call path and belongs to the `staff-engineer` feedback-path decision,
  not to a metrics job.
* **`account_id`** (the hashed-identity foundation) — out of scope; adoption metrics do not
  need identity and should not become the reason we add it.
* **Any local-channel usage capture.** Default-on collection from other people's machines
  is a founder decision, not a metrics decision. The report says "dark by design" until
  then.
