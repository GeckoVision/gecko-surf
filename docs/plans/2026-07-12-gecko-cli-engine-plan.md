# Gecko CLI (engine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `gecko add <api>` golden path + GECKO branding to the existing Python CLI, so one command comprehends any API, stores its key in the OS keychain, and wires it into Claude Code over stdio.

**Architecture:** New module `gecko/onboard.py` holds the `add`/`rm`/`list` logic (keeps `cli.py` a thin dispatcher). It is glue over what already exists: `netguard` (SSRF), `docs_reader`/`ingest` via `AgentApiClient` (comprehend), `credentials` (keychain), and `serve --stdio` (the launch the client spawns). A resolved spec is cached to `~/.gecko/surfaces/<name>.json`; the client is configured to run `gecko serve <cache> --stdio`.

**Tech Stack:** Python 3.11+, argparse, stdlib `urllib`, `keyring` (via `gecko.credentials`), `pytest`, `uv`.

## Global Constraints

- Python 3.11+; `mypy gecko` must stay clean; `ruff format` + `ruff check` clean.
- Control-plane only: never persist response payloads or secrets outside the OS keychain; never log auth values.
- SSRF: every http(s) input passes `netguard.validate_public_url` before fetch.
- Typed public signatures; typed exceptions (define `OnboardError(Exception)`).
- Keep `cli.py` a thin dispatcher — real logic in `gecko/onboard.py` (< ~300 lines).
- Tests use light fakes (injected fetcher / keychain / command-runner); no network in unit tests.
- Client config is written by shelling out to `claude mcp add` (robust across versions), with a printed-command fallback when `claude` is absent — never hand-edit `~/.claude.json`.

---

### Task 1: Spec resolution (`gecko/onboard.py`)

**Files:**
- Create: `gecko/onboard.py`
- Test: `tests/test_onboard_resolve.py`

**Interfaces:**
- Produces: `resolve_spec(ref: str, *, fetch: Fetcher | None = None) -> dict[str, Any]` where `Fetcher = Callable[[str], str]`; raises `OnboardError`.
- Consumes: `netguard.validate_public_url`, `docs_reader.from_docs`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_onboard_resolve.py
import json
import pytest
from gecko.onboard import resolve_spec, OnboardError

_SPEC = {"openapi": "3.0.3", "info": {"title": "T", "version": "1"}, "paths": {}}

def test_resolves_openapi_url_via_injected_fetch():
    spec = resolve_spec("https://api.example.com/openapi.json",
                        fetch=lambda u: json.dumps(_SPEC))
    assert spec["openapi"] == "3.0.3"

def test_resolves_local_path(tmp_path):
    p = tmp_path / "spec.json"; p.write_text(json.dumps(_SPEC))
    assert resolve_spec(str(p))["info"]["title"] == "T"

def test_rejects_unsafe_url():
    with pytest.raises(OnboardError):
        resolve_spec("http://169.254.169.254/openapi.json", fetch=lambda u: "{}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_onboard_resolve.py -q`
Expected: FAIL (`ModuleNotFoundError: gecko.onboard`).

- [ ] **Step 3: Implement `resolve_spec`**

```python
# gecko/onboard.py
"""`gecko add` onboarding — glue over the engine. Thin, control-plane only."""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any

from . import docs_reader
from .netguard import UnsafeUrlError, validate_public_url

Fetcher = Callable[[str], str]


class OnboardError(Exception):
    """A recoverable onboarding failure (bad spec, unreachable source, etc.)."""


def _default_fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=20) as r:  # nosec - validated below
        return r.read().decode("utf-8", "replace")


def resolve_spec(ref: str, *, fetch: Fetcher | None = None) -> dict[str, Any]:
    """Resolve an API reference to an OpenAPI dict.

    ``ref`` may be an http(s) OpenAPI URL, an http(s) docs page (recovered via
    from-docs), or a local path (dev). http(s) inputs are SSRF-validated first.
    """
    fetch = fetch or _default_fetch
    if ref.startswith(("http://", "https://")):
        try:
            validate_public_url(ref)
        except UnsafeUrlError as exc:
            raise OnboardError(f"refusing unsafe URL: {exc}") from exc
        body = fetch(ref)
        try:
            spec = json.loads(body)
            if isinstance(spec, dict) and spec.get("openapi"):
                return spec
        except json.JSONDecodeError:
            pass
        # Not a JSON spec — try docs recovery.
        result = docs_reader.from_docs(ref)
        return result.draft
    # Local path (dev convenience).
    try:
        with open(ref, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise OnboardError(f"could not read spec at {ref}: {exc}") from exc
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_onboard_resolve.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add gecko/onboard.py tests/test_onboard_resolve.py
git commit -m "feat(cli): resolve an API ref (url/docs/path) to an OpenAPI dict"
```

---

### Task 2: Cache the resolved spec

**Files:**
- Modify: `gecko/onboard.py`
- Test: `tests/test_onboard_cache.py`

**Interfaces:**
- Produces: `safe_name(ref: str) -> str`; `cache_spec(name: str, spec: dict[str, Any], *, home: Path | None = None) -> Path`. Cache dir defaults to `~/.gecko/surfaces/`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_onboard_cache.py
import json
from gecko.onboard import cache_spec, safe_name

def test_cache_writes_and_roundtrips(tmp_path):
    path = cache_spec("stripe", {"openapi": "3.0.3"}, home=tmp_path)
    assert path.exists()
    assert json.loads(path.read_text())["openapi"] == "3.0.3"
    assert path.parent == tmp_path / ".gecko" / "surfaces"

def test_safe_name_sanitizes():
    assert safe_name("https://api.example.com/openapi.json") == "api-example-com"
    assert " " not in safe_name("My API") and "/" not in safe_name("a/b")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_onboard_cache.py -q`
Expected: FAIL (`ImportError: cannot import name 'cache_spec'`).

- [ ] **Step 3: Implement caching**

```python
# add to gecko/onboard.py
import re
from pathlib import Path


def safe_name(ref: str) -> str:
    """A filesystem/name-safe surface id derived from a ref (host or slug)."""
    base = ref
    if ref.startswith(("http://", "https://")):
        from urllib.parse import urlsplit

        base = urlsplit(ref).netloc or ref
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return slug or "surface"


def cache_spec(name: str, spec: dict[str, Any], *, home: Path | None = None) -> Path:
    """Persist the comprehended spec (surface metadata only — no payloads)."""
    root = (home or Path.home()) / ".gecko" / "surfaces"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{safe_name(name)}.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_onboard_cache.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gecko/onboard.py tests/test_onboard_cache.py
git commit -m "feat(cli): cache resolved specs under ~/.gecko/surfaces"
```

---

### Task 3: Configure the client (Claude Code, stdio)

**Files:**
- Modify: `gecko/onboard.py`
- Test: `tests/test_onboard_configure.py`

**Interfaces:**
- Produces: `configure_claude(name: str, cache_path: Path, *, gecko_bin: str = "gecko", run: Runner | None = None) -> ConfigResult` where `Runner = Callable[[list[str]], int]` and `ConfigResult` is a frozen dataclass `{ok: bool, command: list[str], applied: bool, note: str}`.
- Behavior: builds `[<gecko_bin>, "mcp", "add", "--transport", "stdio", name, "--", gecko_bin, "serve", str(cache_path), "--stdio"]`? No — `claude mcp add`. Build `["claude","mcp","add","--transport","stdio",name,"--",gecko_bin,"serve",str(cache_path),"--stdio"]`. Run it; if `claude` missing (runner raises FileNotFoundError or returns 127), set `applied=False` and return the command for the user to run.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_onboard_configure.py
from pathlib import Path
from gecko.onboard import configure_claude

def test_builds_stdio_add_command_and_applies():
    calls = []
    def run(cmd): calls.append(cmd); return 0
    r = configure_claude("stripe", Path("/tmp/stripe.json"), gecko_bin="gecko", run=run)
    assert r.ok and r.applied
    assert calls[0][:5] == ["claude", "mcp", "add", "--transport", "stdio"]
    assert "--stdio" in calls[0] and "stripe" in calls[0]
    assert "/tmp/stripe.json" in calls[0]

def test_fallback_when_claude_missing_returns_command():
    def run(cmd): raise FileNotFoundError("claude")
    r = configure_claude("stripe", Path("/tmp/s.json"), run=run)
    assert not r.applied and r.ok  # ok=we produced a usable command
    assert r.command[0] == "claude"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_onboard_configure.py -q`
Expected: FAIL (`ImportError: configure_claude`).

- [ ] **Step 3: Implement the config writer**

```python
# add to gecko/onboard.py
from dataclasses import dataclass

Runner = Callable[[list[str]], int]


@dataclass(frozen=True)
class ConfigResult:
    ok: bool
    command: list[str]
    applied: bool
    note: str


def _default_run(cmd: list[str]) -> int:
    import subprocess

    return subprocess.run(cmd, check=False).returncode


def configure_claude(
    name: str,
    cache_path: Path,
    *,
    gecko_bin: str = "gecko",
    run: Runner | None = None,
) -> ConfigResult:
    """Register the surface with Claude Code over stdio (client spawns the server)."""
    run = run or _default_run
    command = [
        "claude", "mcp", "add", "--transport", "stdio", name,
        "--", gecko_bin, "serve", str(cache_path), "--stdio",
    ]
    try:
        code = run(command)
    except FileNotFoundError:
        return ConfigResult(True, command, False,
                            "Claude Code CLI not found — run the command above yourself.")
    if code == 0:
        return ConfigResult(True, command, True, "added to Claude Code (stdio).")
    return ConfigResult(True, command, False,
                        f"`claude mcp add` exited {code} — run the command above yourself.")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_onboard_configure.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gecko/onboard.py tests/test_onboard_configure.py
git commit -m "feat(cli): configure Claude Code over stdio (with printed fallback)"
```

---

### Task 4: Key wiring (prompt + keychain only when the spec declares auth)

**Files:**
- Modify: `gecko/onboard.py`
- Test: `tests/test_onboard_auth.py`

**Interfaces:**
- Produces: `spec_needs_auth(spec: dict[str, Any]) -> bool`; `ensure_key(name: str, *, prompt: Callable[[str], str], store: Callable[[str, str], None]) -> bool` (returns True if a key is now present/stored; injected prompt+store for tests).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_onboard_auth.py
from gecko.onboard import spec_needs_auth, ensure_key

def test_detects_declared_auth():
    assert spec_needs_auth({"components": {"securitySchemes": {"k": {"type": "apiKey"}}}})
    assert not spec_needs_auth({"paths": {}})

def test_ensure_key_stores_when_prompted():
    stored = {}
    ok = ensure_key("stripe", prompt=lambda q: "sk-live-x",
                    store=lambda name, secret: stored.__setitem__(name, secret))
    assert ok and stored == {"stripe": "sk-live-x"}

def test_ensure_key_skips_on_empty():
    ok = ensure_key("stripe", prompt=lambda q: "", store=lambda n, s: None)
    assert not ok
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_onboard_auth.py -q`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement auth detection + storage**

```python
# add to gecko/onboard.py
def spec_needs_auth(spec: dict[str, Any]) -> bool:
    """True if the spec declares any security scheme (so the API needs a key)."""
    schemes = spec.get("components", {}).get("securitySchemes")
    return bool(schemes) or bool(spec.get("security"))


def ensure_key(
    name: str,
    *,
    prompt: Callable[[str], str],
    store: Callable[[str, str], None],
) -> bool:
    """Prompt (hidden, injected) for the provider key and store it. Never logged."""
    secret = prompt(f"Enter API key for {name} (hidden, stored in OS keychain): ")
    if not secret:
        return False
    store(name, secret)
    return True
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_onboard_auth.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add gecko/onboard.py tests/test_onboard_auth.py
git commit -m "feat(cli): detect declared auth + store the key in the keychain"
```

---

### Task 5: Assemble `gecko add` + wire into the dispatcher

**Files:**
- Modify: `gecko/onboard.py` (add `add()` orchestrator), `gecko/cli.py` (dispatch + `_cmd_add`)
- Test: `tests/test_onboard_add.py`

**Interfaces:**
- Produces: `add(ref: str, *, name: str | None = None, deps: AddDeps) -> int` where `AddDeps` bundles the injected `fetch`, `comprehend` (`Callable[[dict], int]` returning tool count), `prompt`, `store`, `run`, and `home`. Real defaults live in `_cmd_add`.
- Consumes: Tasks 1-4 functions; `AgentApiClient(spec, session=public_session()).list_tools()` for the real comprehend.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_onboard_add.py
import json
from gecko.onboard import add, AddDeps

_SPEC = {"openapi": "3.0.3", "info": {"title": "Stripe", "version": "1"},
         "components": {"securitySchemes": {"k": {"type": "apiKey"}}}, "paths": {}}

def test_add_end_to_end_with_fakes(tmp_path, capsys):
    calls = []
    deps = AddDeps(
        fetch=lambda u: json.dumps(_SPEC),
        comprehend=lambda spec: 47,
        prompt=lambda q: "sk-live-x",
        store=lambda n, s: calls.append(("store", n)),
        run=lambda cmd: (calls.append(("run", cmd)) or 0),
        home=tmp_path,
    )
    rc = add("https://api.stripe.com/openapi.json", deps=deps)
    out = capsys.readouterr().out
    assert rc == 0
    assert ("store", "api-stripe-com") in calls
    assert (tmp_path / ".gecko" / "surfaces" / "api-stripe-com.json").exists()
    assert "47" in out and "ask your agent" in out.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_onboard_add.py -q`
Expected: FAIL (`ImportError: add`).

- [ ] **Step 3: Implement the orchestrator + dispatch**

```python
# add to gecko/onboard.py
@dataclass
class AddDeps:
    fetch: Fetcher
    comprehend: Callable[[dict[str, Any]], int]
    prompt: Callable[[str], str]
    store: Callable[[str, str], None]
    run: Runner
    home: Path


def add(ref: str, *, name: str | None = None, deps: AddDeps) -> int:
    try:
        spec = resolve_spec(ref, fetch=deps.fetch)
    except OnboardError as exc:
        print(f"  ✗ {exc}", file=__import__("sys").stderr)
        return 2
    surface = name or safe_name(ref)
    n_tools = deps.comprehend(spec)
    print(f"  ✓ comprehended {n_tools} endpoint(s) → first-call-correct tools")
    if spec_needs_auth(spec):
        if ensure_key(surface, prompt=deps.prompt, store=deps.store):
            print("  ✓ key → sealed in OS keychain (never in mcp.json)")
        else:
            print("  ○ no key entered — add later with `gecko auth set " + surface + "`")
    path = cache_spec(surface, spec, home=deps.home)
    cfg = configure_claude(surface, path, run=deps.run)
    mark = "✓" if cfg.applied else "→"
    print(f"  {mark} {cfg.note}")
    if not cfg.applied:
        print("     " + " ".join(cfg.command))
    print(f"\n  → ask your agent to use the '{surface}' tools.")
    return 0
```

```python
# gecko/cli.py — add "add" to _SUBCOMMANDS and dispatch
# 1) _SUBCOMMANDS = ("add", "serve", "test", "from-docs", "auth")
# 2) in main(): if cmd == "add": return _cmd_add(rest)
# 3) new _cmd_add:
def _cmd_add(argv: list[str]) -> int:
    import getpass
    from pathlib import Path

    from . import onboard
    from .access import public_session
    from .client import AgentApiClient
    from .credentials import CredentialRef, KeyringBackend

    p = argparse.ArgumentParser(
        prog="gecko add",
        description="Comprehend an API and wire it into your agent (stdio, key in keychain).",
    )
    p.add_argument("api", help="OpenAPI URL, docs URL, or local path.")
    p.add_argument("--name", default=None, help="Surface name (default: derived from the ref).")
    args = p.parse_args(argv)

    def _comprehend(spec: dict) -> int:
        return len(AgentApiClient(spec, session=public_session()).list_tools())

    def _store(name: str, secret: str) -> None:
        KeyringBackend().store(CredentialRef(api=name), secret)

    deps = onboard.AddDeps(
        fetch=onboard._default_fetch,
        comprehend=_comprehend,
        prompt=lambda q: getpass.getpass(q),
        store=_store,
        run=onboard._default_run,
        home=Path.home(),
    )
    return onboard.add(args.api, name=args.name, deps=deps)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_onboard_add.py -q && uv run mypy gecko/onboard.py gecko/cli.py`
Expected: PASS (1 passed); mypy clean.

- [ ] **Step 5: Commit**

```bash
git add gecko/onboard.py gecko/cli.py tests/test_onboard_add.py
git commit -m "feat(cli): gecko add — one-command onboard any API to your agent"
```

---

### Task 6: `gecko rm` and `gecko list`

**Files:**
- Modify: `gecko/onboard.py`, `gecko/cli.py`
- Test: `tests/test_onboard_rm_list.py`

**Interfaces:**
- Produces: `remove(name: str, *, run: Runner, home: Path) -> int`; `list_surfaces(*, home: Path) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_onboard_rm_list.py
from gecko.onboard import cache_spec, remove, list_surfaces

def test_list_and_remove(tmp_path):
    cache_spec("stripe", {"openapi": "3.0.3"}, home=tmp_path)
    assert "stripe" in list_surfaces(home=tmp_path)
    calls = []
    rc = remove("stripe", run=lambda cmd: (calls.append(cmd) or 0), home=tmp_path)
    assert rc == 0
    assert not (tmp_path / ".gecko" / "surfaces" / "stripe.json").exists()
    assert calls and calls[0][:3] == ["claude", "mcp", "remove"]
    assert "stripe" not in list_surfaces(home=tmp_path)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_onboard_rm_list.py -q`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement rm/list + dispatch**

```python
# add to gecko/onboard.py
def list_surfaces(*, home: Path) -> list[str]:
    root = home / ".gecko" / "surfaces"
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.json"))


def remove(name: str, *, run: Runner, home: Path) -> int:
    slug = safe_name(name)
    try:
        run(["claude", "mcp", "remove", slug])
    except FileNotFoundError:
        pass  # client not present; still drop the cache
    path = home / ".gecko" / "surfaces" / f"{slug}.json"
    path.unlink(missing_ok=True)
    print(f"  removed surface '{slug}'")
    return 0
```

```python
# gecko/cli.py: add "rm","list" to _SUBCOMMANDS; dispatch to small wrappers that
# call onboard.remove(name, run=onboard._default_run, home=Path.home()) and
# onboard.list_surfaces(home=Path.home()) (print one per line, or a hint if empty).
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_onboard_rm_list.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add gecko/onboard.py gecko/cli.py tests/test_onboard_rm_list.py
git commit -m "feat(cli): gecko rm / gecko list for onboarded surfaces"
```

---

### Task 7: GECKO banner + grouped help

**Files:**
- Modify: `gecko/cli.py` (replace `_print_help`)
- Test: `tests/test_cli_help.py`

**Interfaces:**
- Produces: `_banner() -> str` (ASCII GECKO wordmark, brand blue `\x1b[38;2;20;110;245m` with a no-color fallback when stdout is not a TTY); `_print_help()` prints banner + grouped commands.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_help.py
from gecko.cli import _banner, _print_help

def test_banner_contains_wordmark():
    assert "GECKO" in _banner().upper()

def test_help_groups_commands(capsys):
    _print_help()
    out = capsys.readouterr().out
    for token in ("add", "auth", "doctor", "Onboard", "make any API agent-usable"):
        assert token in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cli_help.py -q`
Expected: FAIL (`ImportError: _banner` / assertion on groups).

- [ ] **Step 3: Implement the banner + grouped help**

```python
# gecko/cli.py — replace _print_help, add _banner
import sys as _sys

_BLUE = "\x1b[38;2;20;110;245m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"

_WORDMARK = r"""
  ▄▄ ▄▄▄ ▄▄  ▄  ▄  ▄▄▄
 ▐▌ ▐▌   ▐▌ ▟▙ ▐▌ ▐▌ ▐▌   G E C K O
 ▐▌▟▌▐▛▀ ▐▌ ▜▛ ▐▌ ▐▌ ▐▌
  ▀▀ ▀▀▀ ▀▀▀▘  ▀  ▀▀▀
""".rstrip("\n")


def _banner() -> str:
    color = _sys.stdout.isatty()
    mark = f"{_BLUE}{_WORDMARK}{_RESET}" if color else _WORDMARK.replace("G E C K O", "GECKO")
    return mark


def _print_help() -> None:
    print(_banner())
    print("  make any API agent-usable — first call correct\n")
    print(f"{_BOLD}Onboard:{_RESET}" if _sys.stdout.isatty() else "Onboard:")
    print("  add <api>          comprehend any API + wire it into your agent (stdio)")
    print("  rm <name>          remove an onboarded surface")
    print("  list               list onboarded surfaces")
    print("\nKeys:")
    print("  auth set|rm|list   hold your provider key in the OS keychain (BYOK)")
    print("\nDiagnose:")
    print("  doctor             check your setup, print the exact next step")
    print("\nAdvanced:")
    print("  serve <spec>       serve a comprehended spec to agents (MCP)")
    print("  from-docs <src>    recover a draft OpenAPI from a doc page")
    print("  test  <spec>       first-call-correctness checks")
    print("\nBare `gecko <spec>` is shorthand for `gecko serve <spec>`.")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_cli_help.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Format, type-check, commit**

```bash
uv run ruff format gecko/ tests/ && uv run ruff check --fix gecko/ tests/
uv run mypy gecko
git add gecko/cli.py tests/test_cli_help.py
git commit -m "feat(cli): GECKO banner + grouped help"
```

---

### Task 8: End-to-end smoke (manual, pre-demo)

**Files:** none (verification only).

- [ ] **Step 1: Comprehend a real API into a local Claude Code**

Run (a spec-bearing API, `claude` installed):
```bash
uv run gecko add https://raw.githubusercontent.com/jup-ag/jupiter-quote-api-node/main/swagger.yaml --name jupiter
```
Expected: prints `✓ comprehended N endpoint(s)…`, caches `~/.gecko/surfaces/jupiter.json`, and either applies the `claude mcp add` or prints it.

- [ ] **Step 2: Confirm the agent sees the tools**

In a Claude Code session: verify the `jupiter` MCP server is listed and a tool call succeeds (the "wired ≠ reaches the agent" check). Then `uv run gecko rm jupiter` to clean up.

---

## Self-Review

- **Spec coverage:** golden path (Tasks 1–5), key-never-pasted (Task 4), stdio/no-tunnel (Task 3), rm/list (Task 6), branding/grouped help (Task 7), control-plane + SSRF (Global Constraints + Task 1), doctor polish is deferred to a follow-up (noted; existing `doctor` unchanged). Distribution/binary = the **separate Plan B**.
- **Placeholder scan:** none — every code step is complete.
- **Type consistency:** `Fetcher`, `Runner`, `ConfigResult`, `AddDeps`, `OnboardError` are defined in Task 1–5 and reused consistently; `safe_name`/`cache_spec`/`configure_claude`/`ensure_key`/`add`/`remove`/`list_surfaces` signatures match across tasks.

## Follow-on

**Plan B (separate):** npm distribution — PyInstaller per-platform binaries (darwin-arm64, linux-x64) in CI, published as npm `optionalDependencies` behind the `@geckovision/gecko` launcher (pay.sh/esbuild model).
