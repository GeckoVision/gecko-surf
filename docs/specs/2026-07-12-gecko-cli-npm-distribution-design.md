# Gecko CLI — npm/npx Distribution (Plan B) Design

**Date:** 2026-07-12
**Status:** Approved design → implementation plan
**Owner:** founder + devops-engineer

## Purpose

Let any dev install and run the `gecko` CLI with **`npx @geckovision/gecko add <api>`**
— no Python, no uv, only Node (for `npx`). This is the distribution half of the CLI;
the engine (`gecko add`/`serve`/`doctor`/…) is already built and merged.

## What already exists (reuse, don't rebuild)

`.github/workflows/release.yml` already, on a `v*` tag, builds a **PyInstaller
`--onefile` standalone binary** (via `packaging/gecko_entry.py`) for a matrix of
targets, smoke-tests each (`--help` exits 0), and uploads them to the GitHub Release
with `SHA256SUMS`. Current asset names (contract — `install.sh` depends on them):

- `gecko-linux-x86_64`
- `gecko-linux-arm64`
- `gecko-darwin-arm64`

So the **binaries are solved.** Plan B only adds the npm packaging layer on top and a
publish step that reuses these exact binaries.

## Design — the esbuild / bundled-binary model

A thin **launcher** package with per-platform binary packages as
`optionalDependencies`. npm installs only the one matching the host's `os`/`cpu`; the
launcher execs its binary. No install-time download, works offline, no `postinstall`
script (robust against `--ignore-scripts` and corporate proxies — the reason esbuild
abandoned postinstall-download).

### Packages

| Package | Contents | `os` / `cpu` |
|---|---|---|
| `@geckovision/gecko` (launcher) | `bin/gecko.js` + `package.json` (no binary) | any |
| `@geckovision/gecko-linux-x64` | the `gecko-linux-x86_64` binary + `package.json` | linux / x64 |
| `@geckovision/gecko-linux-arm64` | the `gecko-linux-arm64` binary | linux / arm64 |
| `@geckovision/gecko-darwin-arm64` | the `gecko-darwin-arm64` binary | darwin / arm64 |

(win + darwin-x64 are a fast-follow once their binaries are added to `release.yml`.)

### Naming map (binary asset → node platform)

`process.platform`+`process.arch` → package: `linux`+`x64` → `gecko-linux-x64`
(binary `gecko-linux-x86_64`); `linux`+`arm64` → `gecko-linux-arm64`; `darwin`+`arm64`
→ `gecko-darwin-arm64`. The launcher maps `x86_64↔x64` internally.

### The launcher (`bin/gecko.js`)

1. Compute `pkg = @geckovision/gecko-${platform}-${arch}` (with the arch map).
2. `require.resolve(pkg + "/package.json")` → its dir → the binary path from the
   platform package's `bin` field.
3. If not resolvable (unsupported platform / optionalDependency skipped), print a
   clear message listing supported platforms + a link, exit 1.
4. `execFileSync(binary, process.argv.slice(2), { stdio: "inherit" })`; propagate the
   child's exit code; forward signals.

The **binary-resolution logic is a pure function** (`resolveBinary(platform, arch,
resolver)`), unit-tested with a fake resolver — no real install needed.

### Platform `package.json`

`{ name, version, os: ["<os>"], cpu: ["<cpu>"], bin: { … } }` — `os`/`cpu` let npm
skip non-matching packages silently (that's what makes optionalDependencies work).

### Version sync

Every package shares ONE version = the release tag (minus the `v`). The launcher's
`optionalDependencies` pin each platform package to `"=<version>"` (exact). A release
publishes all of them together (RELEASING.md-style lockstep, extended).

## CI (extend `release.yml` or a sibling `npm-publish.yml`)

On the same `v*` tag, after the binaries are built + uploaded:
1. A publish job downloads the release binaries (or takes them from the matrix
   artifacts), assembles each `@geckovision/gecko-<plat>` package (copy binary in,
   stamp version + os/cpu), and `npm publish --access public` each.
2. Assembles the launcher (stamp version + the matching `optionalDependencies`) and
   `npm publish` it **last** (so the platform deps exist when it lands).
Requires an **`NPM_TOKEN`** repo secret (founder provides). Publishing is idempotent
per version (a re-run of an already-published version is a no-op / skipped).

## Testing

- **Launcher unit tests** (Node, no network): `resolveBinary` returns the right package
  for supported (platform,arch) pairs, the correct arch mapping, and a clear error for
  unsupported pairs.
- **`npm pack --dry-run`** on the launcher + a platform package (assembled locally from
  a dummy binary) → confirm the tarball contents (bin + package.json, os/cpu set).
- **Manual pre-demo smoke** (documented): on a real mac-arm64 + linux-x64, `npm i -g`
  the assembled packages from a local tarball → `gecko --help` runs the native binary,
  no Python. This is the "wired ≠ works" check.

## Scope (v1)

Ship the launcher + the 3 platform packages that `release.yml` already builds, plus the
publish CI. **Out:** win / darwin-x64 (need binaries first), a Homebrew tap, autoupdate.

## Decisions (resolved)

- Distribution model = **bundled binary via npm optionalDependencies + launcher**
  (esbuild pattern), NOT a `postinstall`-download launcher — robustness over simplicity. ✅
- Reuse the **existing `release.yml` PyInstaller binaries** — do not add a second freezer. ✅
- Package scope = `@geckovision/*`; launcher command = `gecko`. ✅
- `npm publish` credential is the founder's `NPM_TOKEN` (like the demo package). ✅
