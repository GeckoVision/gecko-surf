"""Run Orquestra's flow engine — HIS compiler and HIS interpreter — from Python, offline.

Gecko projects a `ProgramGraph` into an FDL document (`gecko.project.fdl`). Whether
that document is *right* is not our judgement to make: the only authority is the
engine that will execute it. This module shells out to `bun` against a local
Orquestra checkout and imports his real modules, so a projection is falsified by
his compiler's own error text rather than by our reading of his schema.

    compile_fdl(doc)                        -> CompileOutcome   # his compile()
    run_fdl(doc, inputs, rpc_url=...)       -> RunOutcome       # his compile() + run()

Nothing here signs or broadcasts. `run_fdl` reaches nodes that fetch a blockhash
and simulate; every artefact it produces is unsigned.

The TypeScript entry points live in `scripts/orquestra_bridge_ts/`. The `_ts`
suffix is load-bearing: a sibling directory named `orquestra_bridge` is a PEP-420
namespace-package portion, and while the import system prefers this module file,
mypy did NOT — it resolved `scripts.orquestra_bridge` to the (empty) package and
reported every public name here as missing, on every file that imports one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "Budgets",
    "BridgeError",
    "BridgeInvocationError",
    "BunMissingError",
    "CompileError",
    "CompileOutcome",
    "FlowPlan",
    "OrquestraCheckoutMissingError",
    "PlanNode",
    "ProjectIdl",
    "RunError",
    "RunOutcome",
    "compile_fdl",
    "let_me_buy_project",
    "orquestra_root",
    "run_fdl",
]

_REPO = Path(__file__).resolve().parent.parent
_BRIDGE_DIR = _REPO / "scripts" / "orquestra_bridge_ts"
_RESULT_MARKER = "__ORQUESTRA_BRIDGE_RESULT__"
_ENGINE_SUBPATH = Path("packages/worker/src/flow-engine")
# `orquestra.build_instruction@1` fetches a blockhash and `solana.compose_transaction@1`
# simulates, both over the network, so a run is slower than a compile by an RPC round trip.
_COMPILE_TIMEOUT_S = 60.0
_RUN_TIMEOUT_S = 180.0


class BridgeError(RuntimeError):
    """Base class for every way the bridge fails to reach his engine."""


class BunMissingError(BridgeError):
    """`bun` is not on PATH. His worker is a bun/TypeScript project; there is no
    Python path to his compiler, and reimplementing it would defeat the purpose."""


class OrquestraCheckoutMissingError(BridgeError):
    """No Orquestra checkout at the configured path."""


class BridgeInvocationError(BridgeError):
    """`bun` ran but did not return a result line — a crash, a timeout, or output
    this bridge cannot parse. Carries his stderr, with the RPC URL redacted."""


# ---------------------------------------------------------------------------
# outcomes — his result shapes, typed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompileError:
    """One entry of his `CompileResult.errors`, verbatim."""

    message: str
    node_id: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class PlanNode:
    """One node of a compiled stratum. `inputs` is his `in` object, unresolved —
    the interpreter resolves `$refs` at run time, so it is kept exactly as authored."""

    id: str
    type: str
    inputs: Mapping[str, Any]
    condition: str | None = None


@dataclass(frozen=True)
class Budgets:
    wall_time_ms: int
    external_calls: int
    rpc_calls: int


@dataclass(frozen=True)
class FlowPlan:
    """His `FlowPlan`: a content hash plus topologically batched strata."""

    hash: str
    meta: Mapping[str, Any]
    inputs: Mapping[str, Any]
    outputs: Mapping[str, Any]
    strata: tuple[tuple[PlanNode, ...], ...]
    budgets: Budgets

    @property
    def node_ids(self) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(node.id for node in stratum) for stratum in self.strata)


@dataclass(frozen=True)
class CompileOutcome:
    """`{ok: true, plan}` or `{ok: false, errors}` — his words, not ours."""

    ok: bool
    plan: FlowPlan | None = None
    errors: tuple[CompileError, ...] = ()

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(error.message for error in self.errors)


@dataclass(frozen=True)
class RunError:
    message: str
    node_id: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    """A run that never compiled is still a `RunOutcome` — with `compile_errors`
    set and `ok` false — because "his compiler rejected it" is an answer about the
    document, not an exception in this process."""

    ok: bool
    node_outputs: Mapping[str, Any] = field(default_factory=dict)
    rpc_calls: int = 0
    external_calls: int = 0
    error: RunError | None = None
    compile_errors: tuple[CompileError, ...] = ()


@dataclass(frozen=True)
class ProjectIdl:
    """One row of his IDL registry, as `fetchProjectIdl` reads it out of KV.

    The bridge's fake serves these and nothing else: an unregistered project id
    throws inside his node rather than resolving to null, so a flow naming the
    wrong project cannot look like a registry miss.
    """

    project_id: str
    idl: Mapping[str, Any]
    program_id: str
    project_name: str


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


def orquestra_root() -> Path:
    """The Orquestra checkout to import from: `$GECKO_ORQUESTRA_ROOT`, else the
    sibling `../orquestra`. We never modify it — it is imported read-only."""
    configured = os.environ.get("GECKO_ORQUESTRA_ROOT")
    root = Path(configured).expanduser() if configured else _REPO.parent / "orquestra"
    if not (root / _ENGINE_SUBPATH / "compiler.ts").is_file():
        raise OrquestraCheckoutMissingError(
            f"no Orquestra flow engine at {root / _ENGINE_SUBPATH} — clone the "
            "orquestra repo beside this one (or set GECKO_ORQUESTRA_ROOT to it) "
            "and run `bun install` there"
        )
    return root


def _bun() -> str:
    found = shutil.which("bun")
    if not found:
        raise BunMissingError(
            "`bun` is not on PATH — install it (https://bun.sh: "
            "`curl -fsSL https://bun.sh/install | bash`). This bridge runs "
            "Orquestra's real TypeScript compiler; there is no Python fallback."
        )
    return found


def let_me_buy_project() -> ProjectIdl:
    """The default registry row: our let_me_buy IDL fixture under the project id
    the committed provider config already carries. Both files are in this repo, so
    the offline path needs no catalog call."""
    config = json.loads(
        (_REPO / "gecko/providers/configs/orquestra/let_me_buy.json").read_text()
    )["program"]
    idl = json.loads((_REPO / "tests/fixtures/let_me_buy_idl.json").read_text())
    return ProjectIdl(
        project_id=config["orquestra_project"],
        idl=idl,
        program_id=config["program_id"],
        project_name=idl.get("metadata", {}).get("name", "let_me_buy"),
    )


# ---------------------------------------------------------------------------
# invocation
# ---------------------------------------------------------------------------


def _redact(text: str, secrets: Sequence[str]) -> str:
    """An RPC URL is routinely a bearer credential (`?api-key=…`). It must never
    reach an exception message, so it is replaced before anything is raised."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<rpc-url>")
    return text


def _invoke(
    entry: str,
    payload: Mapping[str, Any],
    *,
    timeout: float,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    root = orquestra_root()
    try:
        completed = subprocess.run(
            [_bun(), "run", str(_BRIDGE_DIR / entry)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "ORQUESTRA_ROOT": str(root)},
            cwd=str(_BRIDGE_DIR),
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeInvocationError(f"bun run {entry} exceeded {timeout:.0f}s") from exc

    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(_RESULT_MARKER):
            result: dict[str, Any] = json.loads(line[len(_RESULT_MARKER) :])
            if "bridgeError" in result:
                raise BridgeInvocationError(
                    f"{entry}: {_redact(str(result['bridgeError']), secrets)}"
                )
            return result

    stderr = _redact(completed.stderr, secrets)[-4000:]
    raise BridgeInvocationError(
        f"bun run {entry} produced no result line (exit {completed.returncode}). "
        f"stderr:\n{stderr}"
    )


def _compile_outcome(raw: Mapping[str, Any]) -> CompileOutcome:
    if not raw.get("ok"):
        return CompileOutcome(
            ok=False,
            errors=tuple(
                CompileError(
                    message=str(item.get("message", "")),
                    node_id=item.get("nodeId"),
                    path=item.get("path"),
                )
                for item in raw.get("errors", [])
            ),
        )
    plan = raw["plan"]
    budgets = plan["budgets"]
    return CompileOutcome(
        ok=True,
        plan=FlowPlan(
            hash=plan["hash"],
            meta=plan["meta"],
            inputs=plan["inputs"],
            outputs=plan["outputs"],
            strata=tuple(
                tuple(
                    PlanNode(
                        id=node["id"],
                        type=node["type"],
                        inputs=node.get("in", {}),
                        condition=node.get("if"),
                    )
                    for node in stratum
                )
                for stratum in plan["strata"]
            ),
            budgets=Budgets(
                wall_time_ms=budgets["wallTimeMs"],
                external_calls=budgets["externalCalls"],
                rpc_calls=budgets["rpcCalls"],
            ),
        ),
    )


# ---------------------------------------------------------------------------
# the two public calls
# ---------------------------------------------------------------------------


def compile_fdl(
    doc: Mapping[str, Any], *, skip_node_type_check: bool = False
) -> CompileOutcome:
    """Compile `doc` with Orquestra's own `compile()`.

    `skip_node_type_check` forwards his `CompileOptions` flag; leave it off — with
    the flag off, an unregistered node type is caught, which is exactly the check a
    projector emitting `map.over@1` or `flow.call@1` would need to fail.
    """
    return _compile_outcome(
        _invoke(
            "compile.ts",
            {"doc": doc, "skipNodeTypeCheck": skip_node_type_check},
            timeout=_COMPILE_TIMEOUT_S,
        )
    )


def run_fdl(
    doc: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    rpc_url: str,
    projects: Sequence[ProjectIdl] | None = None,
) -> RunOutcome:
    """Compile `doc`, then execute the plan with his interpreter.

    `projects` is the IDL registry the fake `NodeContext` serves; it defaults to
    our let_me_buy fixture. Any project id NOT in it makes the fake throw by name,
    so a flow pointing at an unknown project fails loudly instead of resolving to
    "not found".

    A flow of only `resolve.pda@1` nodes is `pure` and touches no network; anything
    building or composing a transaction reads `rpc_url` for a blockhash and a
    simulation. Reads only — nothing signs, nothing is submitted.
    """
    registry = list(projects) if projects is not None else [let_me_buy_project()]
    raw = _invoke(
        "run.ts",
        {
            "doc": doc,
            "inputs": dict(inputs),
            "rpcUrl": rpc_url,
            "projects": {
                project.project_id: {
                    "idl": project.idl,
                    "programId": project.program_id,
                    "projectName": project.project_name,
                }
                for project in registry
            },
        },
        timeout=_RUN_TIMEOUT_S,
        secrets=(rpc_url,),
    )

    compiled = _compile_outcome(raw["compile"])
    if not compiled.ok:
        return RunOutcome(ok=False, compile_errors=compiled.errors)

    result = raw["run"]
    error = result.get("error")
    return RunOutcome(
        ok=bool(result.get("ok")),
        node_outputs=result.get("nodeOutputs", {}),
        rpc_calls=int(result.get("rpcCalls", 0)),
        external_calls=int(result.get("externalCalls", 0)),
        error=(
            RunError(message=str(error.get("message", "")), node_id=error.get("nodeId"))
            if error
            else None
        ),
    )
