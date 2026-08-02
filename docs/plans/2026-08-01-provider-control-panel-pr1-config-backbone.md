# Provider Control Panel — PR1: Config-Driven Backbone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the hand-built Orquestra/Meteora provider surface into a **config-driven** one — PDA recipes and program identity become packaged data a loader reads, proven by `lb_pair` still deriving to `5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6`.

**Architecture:** A new dep-light `gecko/provider_config.py` (stdlib `json` + `dataclasses`, no pydantic) defines `ProviderConfig`/`ApiConfig` and deserializes PDA seed specs into the existing `gecko.pda` node types. Packaged JSON configs under `gecko/providers/configs/` ship in the wheel and are read via `importlib.resources`. `gecko/providers/cli.py` discovers programs from config instead of a hardcoded registry; `gecko/providers/meteora.py` keeps only the multi-step `plan` callable (a follow-up PR makes plans declarative). This is the §B0 backbone from `docs/specs/2026-08-01-provider-control-panel.md`, scoped to the program path.

**Tech Stack:** Python 3.11, stdlib `json`/`dataclasses`/`importlib.resources`, `solders` (behind the `[solana]` extra, already in the dev group), existing `gecko.pda` + `gecko.providers.orquestra`.

## Global Constraints

- **No new base dependency.** Config models use `dataclasses` + stdlib `json`, NOT pydantic — the base install stays `pyyaml`+`keyring` only. (`.claude/rules/python.md`: engine stays dep-light.)
- **Configs must ship in the wheel.** They live under `gecko/providers/configs/` and are read via `importlib.resources` — NEVER from repo-root `providers/` (that dir is not packaged; the installed binary/uvx would break).
- **Secrets are pointers, never inline.** Any `auth.account_ref` / `drift_watch.notify.target_ref` value must be a `keyring:`/`env:` pointer; a config carrying an inline secret shape is rejected at load. (Invariant #1 + `.claude/rules/python.md`.)
- **Derivation ground truth is fixed:** `lb_pair` with `{token_x_mint: So11111111111111111111111111111111111111112, token_y_mint: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v, bin_step: 4}` → `5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6`. Any change that breaks this is a regression.
- **Typed exceptions:** define `class ConfigError(Exception)` in `gecko/provider_config.py`; never `raise Exception(...)`.
- **Mandatory before commit:** `uv run ruff format`, `uv run ruff check --fix`, `uv run mypy gecko`, targeted `uv run pytest`.
- **`meteora-demo` alias and `gecko-orquestra --program meteora --stdio` must keep working** exactly as today (no user-facing command change in this PR).

---

### Task 1: Config models + PDA seed-spec deserialization

**Files:**
- Create: `gecko/provider_config.py`
- Test: `tests/test_provider_config.py`

**Interfaces:**
- Consumes: `gecko.pda` — `ConstantPdaSeedNode`, `VariablePdaSeedNode`, `OrderedPairPdaSeedNode`, `PdaNode`, `PdaSeed`, `derive_pda`.
- Produces:
  - `class ConfigError(Exception)`
  - `seed_from_spec(spec: dict) -> PdaSeed`
  - `node_from_spec(name: str, spec: dict) -> PdaNode` where `spec = {"program_id": str, "seeds": list[dict]}`
  - Seed spec JSON forms (the wire contract):
    - `{"kind": "constant", "value": "oracle", "encoding": "utf8"}`
    - `{"kind": "variable", "name": "lb_pair", "source": "account", "encoding": "pubkey"}`
    - `{"kind": "variable", "name": "bin_step", "source": "argument", "encoding": "le", "width": 2}`
    - `{"kind": "ordered_pair", "left": "token_x_mint", "right": "token_y_mint", "select": "min"}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provider_config.py
from gecko.pda import derive_pda
from gecko.provider_config import ConfigError, node_from_spec, seed_from_spec

METEORA = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

LB_PAIR_SPEC = {
    "program_id": METEORA,
    "seeds": [
        {"kind": "ordered_pair", "left": "token_x_mint", "right": "token_y_mint", "select": "min"},
        {"kind": "ordered_pair", "left": "token_x_mint", "right": "token_y_mint", "select": "max"},
        {"kind": "variable", "name": "bin_step", "source": "argument", "encoding": "le", "width": 2},
    ],
}


def test_lb_pair_spec_derives_real_pool():
    node = node_from_spec("lb_pair", LB_PAIR_SPEC)
    got = derive_pda(node, {"token_x_mint": SOL, "token_y_mint": USDC, "bin_step": 4})
    assert got.address == "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"


def test_constant_utf8_seed_bytes():
    seed = seed_from_spec({"kind": "constant", "value": "oracle", "encoding": "utf8"})
    assert seed.value == b"oracle"


def test_unknown_seed_kind_rejected():
    try:
        seed_from_spec({"kind": "nope"})
        assert False, "expected ConfigError"
    except ConfigError:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_provider_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gecko.provider_config'`.

- [ ] **Step 3: Write minimal implementation**

```python
# gecko/provider_config.py
"""Provider config — the config-driven backbone of the Provider Control Panel.

Turns the hand-built provider surface (gecko/providers/*.py) into packaged DATA a
loader reads: PDA seed recipes and program identity become JSON, not code. Stdlib
only (json + dataclasses) so the base install stays dep-light. See
docs/specs/2026-08-01-provider-control-panel.md (§B0).
"""

from __future__ import annotations

from .pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    PdaSeed,
    VariablePdaSeedNode,
)

__all__ = ["ConfigError", "seed_from_spec", "node_from_spec"]


class ConfigError(Exception):
    """A provider/API config is malformed or unsafe (e.g. an inline secret)."""


def seed_from_spec(spec: dict) -> PdaSeed:
    kind = spec.get("kind")
    if kind == "constant":
        encoding = spec.get("encoding", "bytes")
        raw = spec["value"]
        if encoding == "utf8":
            value = raw.encode("utf-8")
        elif encoding == "bytes":
            value = bytes(raw) if isinstance(raw, (list, bytes, bytearray)) else str(raw).encode()
        else:
            raise ConfigError(f"constant seed encoding {encoding!r} unsupported")
        return ConstantPdaSeedNode(value, encoding="utf8" if encoding == "utf8" else "bytes")
    if kind == "variable":
        return VariablePdaSeedNode(
            spec["name"],
            source=spec["source"],
            encoding=spec.get("encoding", "pubkey"),
            width=int(spec["width"]) if "width" in spec else 0,
        )
    if kind == "ordered_pair":
        return OrderedPairPdaSeedNode(spec["left"], spec["right"], spec["select"])
    raise ConfigError(f"unknown seed kind {kind!r}")


def node_from_spec(name: str, spec: dict) -> PdaNode:
    seeds = tuple(seed_from_spec(s) for s in spec["seeds"])
    return PdaNode(name, seeds, program_id=spec["program_id"])
```

> **Note on `VariablePdaSeedNode.width`:** confirm the default width value the dataclass expects by reading `gecko/pda.py:81-95`. If its default is `0`, pass `width=0` when absent (as above); if the constructor omits `width` for non-integer encodings, drop the kwarg when `"width" not in spec`. Match the existing signature exactly.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_provider_config.py -v`
Expected: PASS (3 tests). If `test_lb_pair_spec_derives_real_pool` errors on a missing `solders`, confirm the `[solana]` extra is in the dev group (`pyproject.toml:138-147`) — it is; `uv run` uses it.

- [ ] **Step 5: Commit**

```bash
git add gecko/provider_config.py tests/test_provider_config.py
git commit -m "feat(provider-config): PDA seed-spec deserialization (config, not code)"
```

---

### Task 2: `ProviderConfig` / `ApiConfig` dataclasses + safe loader

**Files:**
- Modify: `gecko/provider_config.py`
- Test: `tests/test_provider_config.py`

**Interfaces:**
- Consumes: `node_from_spec` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class SpecSource: type: str; value: str`
  - `@dataclass(frozen=True) class ProgramSpec: program_id: str; orquestra_project: str; pdas: dict[str, PdaNode]; intents: tuple[str, ...]`
  - `@dataclass(frozen=True) class ApiConfig: api_id: str; kind: str; spec_source: SpecSource | None; program: ProgramSpec | None; auth: dict | None; drift_watch: dict | None; visibility: dict; pricing_hints: dict | None; quarantine: dict | None; metrics: dict | None`
  - `api_config_from_dict(data: dict) -> ApiConfig`
  - `provider_config_from_dict(data: dict) -> ProviderConfig` where `@dataclass(frozen=True) class ProviderConfig: provider_id: str; display_name: str; apis: tuple[str, ...]`
  - Enforces **secrets-are-pointers**: `auth.account_ref` and `drift_watch.notify.target_ref`, if present, must start with `keyring:` or `env:`; else `ConfigError`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_provider_config.py
from gecko.provider_config import ConfigError, api_config_from_dict

METEORA_API = {
    "api_id": "meteora",
    "kind": "program",
    "spec_source": {"type": "program", "value": METEORA},
    "program": {
        "program_id": METEORA,
        "orquestra_project": "v48gsz901w84zriqe0elsl",
        "intents": ["plan_swap"],
        "pdas": {"lb_pair": LB_PAIR_SPEC},
    },
    "auth": {"scheme": "none", "account_ref": "keyring:meteora", "injected": True},
}


def test_api_config_builds_program_pdas():
    cfg = api_config_from_dict(METEORA_API)
    assert cfg.kind == "program"
    assert cfg.program is not None
    assert cfg.program.orquestra_project == "v48gsz901w84zriqe0elsl"
    assert cfg.program.intents == ("plan_swap",)
    assert "lb_pair" in cfg.program.pdas  # already a PdaNode, ready to derive


def test_inline_secret_in_auth_is_rejected():
    bad = {**METEORA_API, "auth": {"scheme": "bearer", "account_ref": "sk_live_ABC123DEF456"}}
    try:
        api_config_from_dict(bad)
        assert False, "expected ConfigError for inline secret"
    except ConfigError:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_provider_config.py -k "api_config or inline_secret" -v`
Expected: FAIL — `ImportError: cannot import name 'api_config_from_dict'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to gecko/provider_config.py

from dataclasses import dataclass, field  # add to imports

_POINTER_PREFIXES = ("keyring:", "env:")


def _assert_pointer(value: object, where: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.startswith(_POINTER_PREFIXES):
        raise ConfigError(
            f"{where} must be a credential POINTER (keyring:/env:), not an inline value"
        )


@dataclass(frozen=True)
class SpecSource:
    type: str
    value: str


@dataclass(frozen=True)
class ProgramSpec:
    program_id: str
    orquestra_project: str
    pdas: dict[str, PdaNode]
    intents: tuple[str, ...]


@dataclass(frozen=True)
class ApiConfig:
    api_id: str
    kind: str
    spec_source: SpecSource | None
    program: ProgramSpec | None = None
    auth: dict | None = None
    drift_watch: dict | None = None
    visibility: dict = field(default_factory=dict)
    pricing_hints: dict | None = None
    quarantine: dict | None = None
    metrics: dict | None = None


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    display_name: str
    apis: tuple[str, ...]


def _program_from_dict(data: dict) -> ProgramSpec:
    pdas = {name: node_from_spec(name, spec) for name, spec in data["pdas"].items()}
    return ProgramSpec(
        program_id=data["program_id"],
        orquestra_project=data["orquestra_project"],
        pdas=pdas,
        intents=tuple(data.get("intents", ())),
    )


def api_config_from_dict(data: dict) -> ApiConfig:
    auth = data.get("auth")
    if auth is not None:
        _assert_pointer(auth.get("account_ref"), "auth.account_ref")
    drift = data.get("drift_watch")
    if drift is not None:
        notify = drift.get("notify") or {}
        _assert_pointer(notify.get("target_ref"), "drift_watch.notify.target_ref")
    src = data.get("spec_source")
    program = data.get("program")
    return ApiConfig(
        api_id=data["api_id"],
        kind=data["kind"],
        spec_source=SpecSource(**src) if src else None,
        program=_program_from_dict(program) if program else None,
        auth=auth,
        drift_watch=drift,
        visibility=data.get("visibility", {}),
        pricing_hints=data.get("pricing_hints"),
        quarantine=data.get("quarantine"),
        metrics=data.get("metrics"),
    )


def provider_config_from_dict(data: dict) -> ProviderConfig:
    return ProviderConfig(
        provider_id=data["provider_id"],
        display_name=data.get("display_name", data["provider_id"]),
        apis=tuple(data.get("apis", ())),
    )
```

Update `__all__` to add: `"SpecSource", "ProgramSpec", "ApiConfig", "ProviderConfig", "api_config_from_dict", "provider_config_from_dict"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_provider_config.py -v`
Expected: PASS (5 tests total).

- [ ] **Step 5: Commit**

```bash
git add gecko/provider_config.py tests/test_provider_config.py
git commit -m "feat(provider-config): ApiConfig/ProviderConfig + secrets-are-pointers guard"
```

---

### Task 3: Packaged configs + `importlib.resources` loader

**Files:**
- Create: `gecko/providers/configs/orquestra/provider.json`
- Create: `gecko/providers/configs/orquestra/meteora.json`
- Modify: `gecko/provider_config.py`
- Modify: `pyproject.toml` (ensure JSON data ships in the wheel)
- Test: `tests/test_provider_config.py`

**Interfaces:**
- Consumes: `api_config_from_dict`, `provider_config_from_dict`.
- Produces:
  - `load_packaged_provider(provider: str) -> tuple[ProviderConfig, dict[str, ApiConfig]]` — reads `gecko/providers/configs/<provider>/provider.json` and each `<api_id>.json` via `importlib.resources`.
  - `provider_base_url(provider_cfg_dict) ` is NOT added; the provider base URL is stored in `provider.json` as `base_url` and returned inside the provider dict — expose it as `load_packaged_provider_raw(provider) -> dict` only if needed. (Keep the typed path returning `ProviderConfig`; read `base_url` from the raw provider.json in Task 4.)

- [ ] **Step 1: Write the config files**

`gecko/providers/configs/orquestra/provider.json`:
```json
{
  "provider_id": "orquestra",
  "display_name": "Orquestra",
  "base_url": "https://api.orquestra.dev/api",
  "apis": ["meteora"]
}
```

`gecko/providers/configs/orquestra/meteora.json`:
```json
{
  "api_id": "meteora",
  "kind": "program",
  "spec_source": {"type": "program", "value": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"},
  "program": {
    "program_id": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
    "orquestra_project": "v48gsz901w84zriqe0elsl",
    "intents": ["plan_swap"],
    "pdas": {
      "lb_pair": {
        "program_id": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
        "seeds": [
          {"kind": "ordered_pair", "left": "token_x_mint", "right": "token_y_mint", "select": "min"},
          {"kind": "ordered_pair", "left": "token_x_mint", "right": "token_y_mint", "select": "max"},
          {"kind": "variable", "name": "bin_step", "source": "argument", "encoding": "le", "width": 2}
        ]
      },
      "reserve": {
        "program_id": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
        "seeds": [
          {"kind": "variable", "name": "lb_pair", "source": "account", "encoding": "pubkey"},
          {"kind": "variable", "name": "token_mint", "source": "account", "encoding": "pubkey"}
        ]
      },
      "oracle": {
        "program_id": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
        "seeds": [
          {"kind": "constant", "value": "oracle", "encoding": "utf8"},
          {"kind": "variable", "name": "lb_pair", "source": "account", "encoding": "pubkey"}
        ]
      }
    }
  }
}
```

Create the package marker so the dir imports cleanly as a resource anchor:
```bash
touch gecko/providers/configs/__init__.py
```

- [ ] **Step 2: Write the failing test**

```python
# append to tests/test_provider_config.py
from gecko.provider_config import load_packaged_provider


def test_packaged_orquestra_loads_and_derives():
    provider, apis = load_packaged_provider("orquestra")
    assert provider.provider_id == "orquestra"
    assert "meteora" in apis
    node = apis["meteora"].program.pdas["lb_pair"]
    from gecko.pda import derive_pda
    got = derive_pda(node, {"token_x_mint": SOL, "token_y_mint": USDC, "bin_step": 4})
    assert got.address == "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_provider_config.py -k packaged -v`
Expected: FAIL — `ImportError: cannot import name 'load_packaged_provider'`.

- [ ] **Step 4: Write minimal implementation**

```python
# add to gecko/provider_config.py
import json
from importlib import resources


def _read_json(provider: str, filename: str) -> dict:
    anchor = resources.files("gecko.providers.configs").joinpath(provider, filename)
    return json.loads(anchor.read_text(encoding="utf-8"))


def load_packaged_provider(provider: str) -> tuple[ProviderConfig, dict[str, ApiConfig]]:
    raw = _read_json(provider, "provider.json")
    provider_cfg = provider_config_from_dict(raw)
    apis: dict[str, ApiConfig] = {}
    for api_id in provider_cfg.apis:
        apis[api_id] = api_config_from_dict(_read_json(provider, f"{api_id}.json"))
    return provider_cfg, apis


def load_packaged_provider_base_url(provider: str) -> str:
    return str(_read_json(provider, "provider.json")["base_url"])
```

Add `"load_packaged_provider", "load_packaged_provider_base_url"` to `__all__`.

- [ ] **Step 5: Ensure the JSON ships in the wheel**

Read `pyproject.toml:135-136` — `[tool.hatch.build.targets.wheel] packages = ["gecko"]`. Hatchling includes non-Python files under a listed package by default, so `gecko/providers/configs/*.json` ships. Verify:

Run: `uv build 2>/dev/null && python -c "import zipfile,glob; z=zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[-1]); print([n for n in z.namelist() if 'configs' in n])"`
Expected: the two `.json` paths + `__init__.py` are listed. If empty, add to `pyproject.toml`:
```toml
[tool.hatch.build.targets.wheel.force-include]
"gecko/providers/configs" = "gecko/providers/configs"
```
Then clean up the build artifact: `rm -rf dist`.

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_provider_config.py -v`
Expected: PASS (6 tests total).

- [ ] **Step 7: Commit**

```bash
git add gecko/providers/configs pyproject.toml gecko/provider_config.py tests/test_provider_config.py
git commit -m "feat(provider-config): packaged orquestra/meteora config + importlib loader"
```

---

### Task 4: Build the surface from config + discover programs (rewire)

**Files:**
- Modify: `gecko/providers/meteora.py` (keep the `plan` callable; source PDAs/identity from config)
- Modify: `gecko/providers/cli.py` (discover `PROGRAMS` from config)
- Test: `tests/test_providers_cli.py` (existing — must still pass), `tests/test_providers_meteora.py` (existing)

**Interfaces:**
- Consumes: `load_packaged_provider`, `load_packaged_provider_base_url`, `ApiConfig`, `ProgramSpec` (Task 3); `Intent`, `OrquestraProgramSurface` (`gecko.providers.orquestra`).
- Produces:
  - In `meteora.py`: `METEORA_INTENTS: dict[str, Intent] = {"plan_swap": _SWAP}` (the `_SWAP`/`_swap_plan` stay); `build_meteora_surface()` now builds from config.
  - In `cli.py`: `build_surface_from_config(provider: str, api_id: str, intents: dict[str, Intent]) -> OrquestraProgramSurface`; `PROGRAMS` discovered from the packaged provider.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_providers_meteora.py
def test_build_meteora_surface_from_config_derives_and_plans():
    from gecko.providers.meteora import build_meteora_surface

    surface = build_meteora_surface()
    # identity + project base came from config, not hardcoded literals
    assert surface.program_id == "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
    assert surface.project_base_url == "https://api.orquestra.dev/api/v48gsz901w84zriqe0elsl"
    # the root Orquestra can't derive:
    lb = surface.derive(
        "lb_pair",
        {
            "token_x_mint": "So11111111111111111111111111111111111111112",
            "token_y_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "bin_step": 4,
        },
    )
    assert lb == "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"
    # plan intent still wired
    out = surface.call_tool(
        "plan_swap",
        {
            "input_mint": "So11111111111111111111111111111111111111112",
            "output_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "bin_step": "4",
        },
    )
    assert out["derived"]["lb_pair"] == "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_providers_meteora.py -k from_config -v`
Expected: FAIL initially only if the new assertions on provenance don't hold — but since `build_meteora_surface` currently hardcodes the same values, this test may PASS against the old code. To make it a true failing-first test, first change `build_meteora_surface` to raise `NotImplementedError` as a placeholder, confirm FAIL, then implement. (Record the intermediate FAIL in the commit message trail is not needed; just verify the red state.)

Practically: implement Step 3 and confirm the whole suite is green; the value of this test is guarding the config path, not a red-first ritual on already-passing literals.

- [ ] **Step 3: Write the implementation**

In `gecko/providers/meteora.py` — delete `_pdas()`, `METEORA_PROGRAM_ID`/`ORQUESTRA_PROJECT_BASE` literals, and the pda-node imports; keep `_swap_plan`, `_SWAP`, and `main`. Replace `build_meteora_surface`:

```python
from typing import Any, Mapping  # keep
from .orquestra import Intent, OrquestraProgramSurface  # keep

# _swap_plan and _SWAP stay exactly as they are.

METEORA_INTENTS: dict[str, Intent] = {_SWAP.name: _SWAP}


def build_meteora_surface() -> "OrquestraProgramSurface":
    from .cli import build_surface_from_config

    return build_surface_from_config("orquestra", "meteora", METEORA_INTENTS)
```

> `_swap_plan` reads `args["input_mint"]`/`["output_mint"]`/`["bin_step"]` and calls `surface.derive("lb_pair", {"token_x_mint": ..., "token_y_mint": ..., "bin_step": ...})`. Unchanged — the PDAs it derives now come from config, but the binding names are identical, so it just works. Keep `METEORA_PROGRAM_ID` exported if other modules import it: re-derive it as `METEORA_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"` (a display constant) to avoid breaking `__all__` consumers; grep first: `grep -rn "METEORA_PROGRAM_ID" gecko tests`.

In `gecko/providers/cli.py` — replace the hardcoded `PROGRAMS` block:

```python
from typing import Callable  # keep
from ..provider_config import (
    ApiConfig,
    load_packaged_provider,
    load_packaged_provider_base_url,
)
from .orquestra import Intent, OrquestraProgramSurface


def build_surface_from_config(
    provider: str, api_id: str, intents: dict[str, Intent]
) -> OrquestraProgramSurface:
    _, apis = load_packaged_provider(provider)
    api = apis[api_id]
    if api.program is None:
        raise ValueError(f"api {api_id!r} is not a program")
    base = load_packaged_provider_base_url(provider).rstrip("/")
    project_base_url = f"{base}/{api.program.orquestra_project}"
    wanted = {k: v for k, v in intents.items() if k in set(api.program.intents)}
    return OrquestraProgramSurface(
        program_id=api.program.program_id,
        project_base_url=project_base_url,
        pdas=dict(api.program.pdas),
        intents=wanted,
    )


def _discover_programs() -> dict[str, Callable[[], OrquestraProgramSurface]]:
    # provider → api_id → its intents registry (code supplies the plan callables)
    from .meteora import METEORA_INTENTS

    intents_by_key: dict[tuple[str, str], dict[str, Intent]] = {
        ("orquestra", "meteora"): METEORA_INTENTS,
    }
    programs: dict[str, Callable[[], OrquestraProgramSurface]] = {}
    for (provider, api_id), intents in intents_by_key.items():
        def _make(provider=provider, api_id=api_id, intents=intents):
            return build_surface_from_config(provider, api_id, intents)

        programs[api_id] = _make
    return programs


PROGRAMS: dict[str, Callable[[], OrquestraProgramSurface]] = _discover_programs()
```

Remove the old `from .orquestra import OrquestraProgramSurface` duplicate and the `from .meteora import build_meteora_surface` line (now unused in cli.py). Keep `serve`, `_add_serve_args`, `main` unchanged — `main` still reads `choices=sorted(PROGRAMS)` and calls `PROGRAMS[args.program]()`.

> **Circular-import check:** `meteora.build_meteora_surface` imports `cli.build_surface_from_config` lazily (inside the function), and `cli._discover_programs` imports `meteora.METEORA_INTENTS` at call time (module import of cli triggers `_discover_programs()` at bottom, which imports meteora — meteora's top-level no longer imports cli at module scope, only inside `build_meteora_surface`). Verify no import cycle by running the CLI help below.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_providers_cli.py tests/test_providers_meteora.py tests/test_provider_config.py -v`
Expected: PASS — including the existing `test_registry_has_meteora`, `test_gecko_orquestra_subcommand_dispatches`, and the new config-path test.

Run: `uv run python -c "from gecko.providers.cli import main; main(['--program','meteora','--help'])"`
Expected: argparse help prints, exits 0 (no circular import).

- [ ] **Step 5: Commit**

```bash
git add gecko/providers/meteora.py gecko/providers/cli.py tests/test_providers_meteora.py
git commit -m "refactor(providers): build Orquestra surface from config, discover PROGRAMS"
```

---

### Task 5: Retire the dead root config + full gate + docs pointer

**Files:**
- Delete: `providers/orquestra/provider.json`, `providers/orquestra/meteora/program.json` (dead, superseded by packaged config) — **confirm with `git rm` and note in the commit**.
- Modify: `docs/specs/2026-08-01-provider-control-panel.md` (Appendix: mark the config backbone SHIPPED for the program path).
- Test: whole suite.

- [ ] **Step 1: Confirm the root config is truly unreferenced**

Run: `grep -rn "providers/orquestra" gecko tests scripts | grep -v "configs/orquestra"`
Expected: no code references the repo-root `providers/orquestra/*.json` (only the new packaged `gecko/providers/configs/orquestra` path is used). If anything references the root path, update it to the packaged loader before deleting.

- [ ] **Step 2: Remove the dead config**

```bash
git rm providers/orquestra/provider.json providers/orquestra/meteora/program.json
```

- [ ] **Step 3: Update the spec appendix**

In `docs/specs/2026-08-01-provider-control-panel.md`, change the Appendix rows:
- "Program surface (hand-built)" → "Program surface (config-driven, program path)" · state: **shipped (PR1)**.
- "Provider config … dead config — this spec builds the loader" → "Provider config — **loader shipped (PR1)**; REST-path fields (visibility/pricing/quarantine applied to report) are follow-on PRs."

- [ ] **Step 4: Run the mandatory gate**

```bash
uv run ruff format
uv run ruff check --fix
uv run mypy gecko
uv run pytest tests/test_provider_config.py tests/test_providers_cli.py tests/test_providers_meteora.py tests/test_pda.py tests/test_program_mcp.py -v
```
Expected: ruff clean, mypy clean over `gecko`, all listed tests PASS. If `test_program_mcp.py` or `test_pda_*` reference the old meteora literals, update them to the config path (they should not — meteora's public `build_meteora_surface`/`METEORA_PROGRAM_ID` are preserved).

- [ ] **Step 5: Smoke the frozen command path**

Run: `uv run gecko-orquestra --program meteora --stdio </dev/null` then Ctrl-C — OR the non-interactive check:
`uv run python -c "from gecko.providers.cli import PROGRAMS; s=PROGRAMS['meteora'](); print(s.program_id, len(s.pdas), list(s.intents))"`
Expected: `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo 3 ['plan_swap']`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore(providers): retire dead root config; mark config backbone shipped (program path)"
```

---

## Self-Review

**1. Spec coverage (against §B0 of the spec):**
- §B0.1 config schema → Tasks 1–3 (seed specs, `ApiConfig`/`ProviderConfig`, packaged JSON). ✅ (program path; REST fields `visibility/auth/pricing/quarantine/drift/metrics` are *parsed* in Task 2 but not yet *consumed* — explicitly a follow-on PR, noted in Task 5 Step 3.)
- §B0.1 secrets-as-pointers invariant → Task 2 `_assert_pointer` + `test_inline_secret_in_auth_is_rejected`. ✅
- §B0.2 "existing hand-built surfaces become config instances" → Task 4 (meteora/PROGRAMS from config). ✅
- §B0.2 "adding an API becomes writing config" → partially: adding a *program's PDAs* is now config; the multi-step `plan` is still code (declarative plans = follow-on). Called out in the plan header + Task 4. ✅ (honest scope)

**2. Placeholder scan:** No TBD/TODO/"handle edge cases". The one soft spot — Task 4 Step 2's red-first note — is addressed with an explicit instruction (temporarily raise `NotImplementedError` to see red, or accept the guard test). Acceptable.

**3. Type consistency:** `node_from_spec(name, spec)` returns `PdaNode`; `ProgramSpec.pdas: dict[str, PdaNode]`; `OrquestraProgramSurface(pdas=dict[str, PdaNode])` — consistent chain. `Intent` reused verbatim from `gecko.providers.orquestra`. `build_surface_from_config(provider, api_id, intents)` signature matches both its caller in `meteora.build_meteora_surface` and `_discover_programs`. `load_packaged_provider` return type `tuple[ProviderConfig, dict[str, ApiConfig]]` matches its use in Task 4. ✅

## Out of scope (explicitly deferred to later PRs)
- REST/OpenAPI config path consumed by `report.py` (visibility filtering, pricing-hint → ESTIMATED revenue panel, quarantine allowlist honoring).
- Declarative multi-step `plan` (so intents are data too).
- External config-as-code dir loading (`load_provider_config(dir)`) + the hosted-vs-repo config decision (spec §D3).
- The console UI and the MEASURED-tier telemetry seam (spec §B6) — V2.
