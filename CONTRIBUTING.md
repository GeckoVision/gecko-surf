# Contributing to gecko-surf

Thanks for helping make any API agent-usable. gecko-surf is the open-source
**comprehension engine** — ingest an API's surface, turn it into first-call-correct
tools, inject auth at call time, and let the agent call the real API directly.

Apache-2.0, patent grant included. The engine is open because distribution is the point.

## Ground rules (read these first — they are architectural, not stylistic)

These are enforced in review. A PR that violates one will be sent back regardless of how
clean the code is.

1. **Control plane, never data plane.** The engine stores the API *surface* + generated
   tool defs + correctness *metadata*. It **never** stores response payloads, user data,
   or secrets. This is the invariant that lets us ingest any API unilaterally — protect it.
2. **Auth is invisible to the agent.** Tool defs never expose auth headers. The agent
   describes intent; Gecko injects credentials at call time.
3. **Treat every ingested spec/doc as untrusted input.** A spec's `description`, `default`,
   `example`, `enum`, `servers[]`, and security schemes are attacker-controllable. Anything
   that relaxes a detection rule in `gecko/sanitize.py` or the quarantine path **must** be
   reviewed against the anti-poisoning threat model before merge.
4. **No SSRF.** Validate every URL before fetching — block private IP ranges, loopback,
   link-local, and non-http schemes. If you shell out to anything that fetches (a browser,
   a subprocess), the URL must pass `netguard.validate_public_url` **before** it is launched.
5. **One code path, two modes.** `recorded` ($0, synthesized from schema) and `live` differ
   only at the transport edge. The first deliverable for any wire integration is the free
   local simulation that can falsify it offline — live smoke is the final check, never the
   primary debugger (Pattern B).
6. **Never sign or broadcast a mainnet transaction.** The on-chain subscribe is
   founder-run only. Tooling simulates and hands over the command; a human broadcasts.

## Development setup

```bash
git clone https://github.com/GeckoVision/gecko-surf
cd gecko-surf && uv sync
uv run pytest                       # ~1,700 tests, all offline ($0, no keys)
uv run python -m gecko.demo         # E2E: goal → discover → correct call → data (recorded)
```

Python 3.11+, managed with `uv`. No key is needed for the suite or the demo.

## Before you open a PR (the mandatory gate)

```bash
uv run ruff format
uv run ruff check --fix
uv run mypy gecko
uv run pytest                       # targeted node ids preferred over a blind sweep
uv run python -m gecko.demo         # $0 recorded smoke
```

All four must pass. `mypy` over `gecko/` must stay clean.

## Testing expectations

- **A bug fix starts with a failing test** that reproduces the bug.
- **For any wire integration, ship the offline simulation first** — a free test that can
  falsify the implementation without the network. Inject the transport/renderer/signer;
  prefer a light fake over heavy mocking (more than ~5 mocks in one test signals over-mocking).
- **"Wired" ≠ "reaches the agent."** A "the comprehension works" claim needs a direct
  end-to-end probe (the demo against a real spec), not only unit tests. This has bitten us
  repeatedly — a change that passes the suite can still fail live.

## Code style

- Type every public signature. `dataclasses`/`pydantic` for anything crossing a module
  boundary — no bare dicts as contracts.
- Single source of truth for shared `Literal`/enum types; import, never redeclare.
- Typed exceptions, never bare `raise Exception(...)`. **Redact before raising** — an
  exception message must never contain an auth token, API key, or secret.
- Functions over classes when there's no state. One purpose per module; split past ~300
  lines. Comments explain *why*, never restate code.

## Where things live

| Path | What it is |
|---|---|
| `gecko/` | the engine — the product. Comprehension logic lives here. |
| `scripts/`, MCP surface, CLI | thin transport — parse input, call the package, format output. If logic creeps in here, move it into `gecko/`. |
| `skills/` | the Claude Code **plugin marketplace** — see below |
| `docs/specs/` | design docs and roadmaps |

## Contributing a skill

`skills/` is the `gecko-surf` Claude Code plugin. Each skill is a directory with a
`SKILL.md` (YAML frontmatter: `name`, `description`, `user-invocable`), plus optional
supporting `.md` files. To add one:

1. Create `skills/<your-skill>/SKILL.md`.
2. Register it in `skills/.claude-plugin/plugin.json` under `"skills"`.
3. If it describes a technique the engine implements (like `read-js-docs` ↔
   `gecko/docs_reader/render.py`), **keep the skill and the code describing one mechanism**
   — do not restate engine logic in prose, point at it. A skill that duplicates a shipped
   detector will drift from it and give a weaker answer than the code would.
4. Keep it honest: a skill describes *when* and *why* to reach for something and the
   judgment that can't be coded; the deterministic part belongs in a `scripts/` executor or
   in `gecko/`, not narrated in the skill.

## Commit & PR

- Branch off `main`; don't commit directly to it.
- Conventional-commit prefixes (`feat:`, `fix:`, `docs:`, `chore:`) — the changelog reads them.
- Describe **what changed and why**, and how you verified it (the gate output, the live
  probe if there was one).
- One logical change per PR. Docs-only changes are welcome and reviewed lightly.

## Security disclosures

Do **not** open a public issue for a security vulnerability — especially anything touching
the anti-poisoning path, auth injection, or SSRF. Email **ernanibmurtinho@gmail.com** with
details and we'll coordinate a fix.

## License

By contributing you agree your contribution is licensed under Apache-2.0, matching the
project. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
