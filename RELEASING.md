# Releasing gecko-surf

A release must bump **every** version marker in lockstep, or consumers see mismatched
numbers (the package on PyPI, the standalone binary, and the Claude/Cursor plugin are
separate artifacts). This checklist keeps them in sync.

## The version markers (bump ALL to the new `X.Y.Z`)

| File | Field | What it versions |
|---|---|---|
| `pyproject.toml` | `version` | the PyPI package (`__version__` auto-tracks it) |
| `skills/.claude-plugin/plugin.json` | `version` | the Claude Code plugin |
| `skills/.cursor-plugin/plugin.json` | `version` | the Cursor plugin |
| `.claude-plugin/marketplace.json` | `version` | the Claude marketplace listing |
| `.cursor-plugin/marketplace.json` | `version` | the Cursor marketplace listing |
| `npm/gecko/package.json` | `version` + `optionalDependencies` pins | the `npx @geckovision/gecko` launcher (CI re-stamps to the tag on publish, but keep it current) |

> **Why this file exists:** 0.3.0 shipped with the plugin manifests still reading 0.2.3 —
> a teammate who refreshed the plugin saw no version change and couldn't tell it updated.
> Never bump `pyproject` alone.

## Steps

1. **Bump** the five markers above to `X.Y.Z`.
2. **Update `CHANGELOG.md`** — a new `## X.Y.Z — YYYY-MM-DD` section (Added / Fixed).
3. **`uv lock`** — sync the lockfile's own `gecko-surf` version.
4. **Verify green:** `uv run ruff check` · `uv run mypy gecko` · `uv run --extra serve --extra dense --extra fcc pytest` (the serve tests need `--extra serve`; a bare `uv run pytest` shows collection errors, not failures) · `uv run python -m gecko.demo`.
5. **PR → merge** the `chore/release-X.Y.Z` branch to `main`.
6. **Tag + push:** `git tag -a vX.Y.Z -m "…" && git push origin vX.Y.Z` → the `release.yml`
   workflow builds the standalone binaries, attaches them to the GitHub Release, **and
   publishes the npm packages** (`@geckovision/gecko` launcher + per-platform binary
   packages) so `npx @geckovision/gecko add <api>` works. *(One-time prereqs: the
   `@geckovision` npm org must exist and an `NPM_TOKEN` automation-token repo secret must
   be set — without it the `publish-npm` job fails but the binaries still ship.)*
7. **Publish to PyPI** (founder-run, token-gated — not in CI):
   `uv build && uv publish dist/gecko_surf-X.Y.Z*`
8. **Redeploy** `mcp.geckovision.tech` (founder-run — the Docker host, no deploy CI).
9. **Plugin refresh** propagates automatically once the marketplace source (this repo) is
   updated; users pull it with `/plugin marketplace update geckovision` + reinstall, or via
   the `/plugin` manager.
