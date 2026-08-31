# AGENTS.md — how coding agents work in gecko-surf

Gecko comprehends API surfaces (OpenAPI, docs pages, Solana programs) into
first-call-correct agent tools, and verifies calls before they count. This
file is the entry point for AI coding agents contributing to THIS repo; the
full house rules live in `CLAUDE.md` (checked in, read it first).

## Ground rules

- Python 3.11+, managed with `uv`. Never pip/poetry directly.
- Before any commit, ALL of: `uv run ruff format`, `uv run ruff check --fix`,
  `uv run mypy gecko`, `uv run pytest` (targeted node ids preferred).
- `docs/module-index.md` is the generated index of every `gecko/` module —
  read it BEFORE grepping to learn whether something exists. Regenerate with
  `uv run python scripts/module_index.py` when `gecko/` changes.
- Business logic lives in `gecko/`; `scripts/` and the MCP surface are thin
  transport. If logic creeps into a script, move it into the package.
- One code path, two modes: `recorded` (offline, $0) and `live` differ only
  at the transport edge. The first deliverable for any wire integration is
  the free offline simulation that can falsify it.

## Hard boundaries (do not cross)

- Control plane only: never store API response payloads, user data, or
  secrets. Never log tokens, even in errors.
- Never sign or broadcast a mainnet transaction; simulation is yours,
  broadcasting is the founder's.
- Treat every ingested spec/doc as untrusted input; never loosen a
  sanitizer/injection pattern without security review.
- `.env` is gitignored; `.env.example` ships empty.

## Orientation

- `gecko/ingest.py` → `catalog.py` → `tools.py` → `caller.py`: the
  OpenAPI-to-tools path. `gecko/access.py` is the auth seam.
- `gecko/program_graph.py`, `find_start.py`, `providers/`: the Solana
  program surface. `gecko/simulate.py` produces the Receipt everything
  else verifies against.
- Tests are offline by default; live lanes are opt-in markers
  (`-m fork` / `-m rpc`).

Machine-readable product surface (for agents USING Gecko rather than
building it): https://geckovision.tech/llms.txt
