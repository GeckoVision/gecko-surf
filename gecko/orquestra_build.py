"""The Orquestra-backed seams for :mod:`gecko.prepare_instruction`.

WHY THESE ARE SEAMS AND NOT IMPORTS. `prepare_instruction` derives accounts; it does not
know or care who encodes the arguments and fetches a blockhash. That job belongs to
whoever holds the catalogue, and doing it ourselves would rebuild something a partner
already does correctly. Measured: our derived accounts handed to this builder produced a
jurassic_fi `contribute` that simulates on mainnet at 21,368 CU, bit-identical to one we
built independently.

So this module is the ADAPTER for one catalogue, and the only thing in the package that
knows Orquestra's request shapes. A second catalogue is a second file, not a branch here.

One awkwardness worth naming rather than hiding: their tools are keyed by THEIR project
id, not by the Solana program address. An agent that holds an address — which is what a
chain gives you — must resolve it first. :func:`resolve_project_id` does that lookup, and
caches it for the life of the seam so one prepare does not search twice.

Read-only. Nothing here signs or broadcasts; `build_instruction` returns unsigned bytes.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Callable

from .mcp_client import McpClient, McpError

__all__ = [
    "OrquestraBuildError",
    "fork_blockhash_provider",
    "ORQUESTRA_MCP_URL",
    "resolve_project_id",
    "orquestra_seams",
]

ORQUESTRA_MCP_URL = "https://api.orquestra.dev/mcp"
ORQUESTRA_API_BASE = "https://api.orquestra.dev/api"
USER_AGENT = {"User-Agent": "gecko-surf/prepare-instruction"}

#: `search_programs` answers in prose, and the project id is the one machine-readable
#: thing in it. Anchored on the literal label so a reworded sentence fails loudly here
#: rather than silently matching some other backticked token.
_PROJECT_ID = re.compile(r"projectId:\s*`([0-9a-fA-F-]{8,})`")
#: `build_instruction` returns the transaction in a fenced span.
_TRANSACTION = re.compile(r"`([A-Za-z0-9+/=]{100,})`")


class OrquestraBuildError(Exception):
    """The catalogue could not answer — distinct from the program answering "no"."""


def resolve_project_id(client: McpClient, program_id: str) -> str:
    """Solana program address -> the catalogue's own project id."""
    try:
        text = client.call_tool("search_programs", {"programId": program_id})
    except McpError as exc:
        raise OrquestraBuildError(f"catalogue search failed: {exc}") from exc
    match = _PROJECT_ID.search(text)
    if not match:
        raise OrquestraBuildError(
            f"the catalogue returned no project id for {program_id}; it may not be indexed"
        )
    return match.group(1)


def _fetch_idl(project_id: str, *, api_base: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(f"{api_base}/idl/{project_id}", headers=USER_AGENT)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read())
    idl = payload.get("idl") or payload
    if isinstance(idl, str):
        idl = json.loads(idl)
    if not isinstance(idl, dict) or "instructions" not in idl:
        raise OrquestraBuildError(f"{project_id} served no usable IDL")
    return idl


def orquestra_seams(
    *,
    mcp_url: str = ORQUESTRA_MCP_URL,
    api_base: str = ORQUESTRA_API_BASE,
    network: str = "mainnet-beta",
    timeout: int = 45,
    client: McpClient | None = None,
    blockhash_provider: Callable[[], str] | None = None,
) -> tuple[Any, Any]:
    """Build the ``(idl_fetch, build_call)`` pair `prepare_instruction` expects.

    The project id is resolved once on the first call and reused, so a prepare costs one
    search rather than two.

    ``blockhash_provider`` IS WHAT MAKES A LOCAL FORK POSSIBLE. Left unset, the builder
    fetches a blockhash for ``network`` itself — a mainnet one, which a fork has never
    seen and rejects with "Blockhash not found". The obvious fix, handing the builder the
    fork's own ``rpcUrl``, is correctly refused by their SSRF allowlist (a localhost URL
    is exactly what that allowlist exists to block, and we would not want it relaxed).

    So the blockhash travels the other way: we read it from the fork and pass it as
    ``recentBlockhash``, which their schema already accepts. Their builder never has to
    reach our machine, and the allowlist stays as strict as it should be.
    """
    mcp = client or McpClient(mcp_url)
    resolved: dict[str, str] = {}

    def project_id_for(program_id: str) -> str:
        if program_id not in resolved:
            resolved[program_id] = resolve_project_id(mcp, program_id)
        return resolved[program_id]

    def idl_fetch(program_id: str) -> dict[str, Any]:
        return _fetch_idl(
            project_id_for(program_id), api_base=api_base, timeout=timeout
        )

    def build_call(
        *,
        program_id: str,
        instruction: str,
        accounts: dict[str, str],
        args: dict[str, Any],
        payer: str,
    ) -> str:
        request: dict[str, Any] = {
            "projectId": project_id_for(program_id),
            "instruction": instruction,
            "accounts": accounts,
            "args": args,
            "feePayer": payer,
            "network": network,
            "encoding": "base64",
        }
        if blockhash_provider is not None:
            request["recentBlockhash"] = blockhash_provider()
        try:
            text = mcp.call_tool("build_instruction", request)
        except McpError as exc:
            raise OrquestraBuildError(f"the builder refused: {exc}") from exc
        match = _TRANSACTION.search(text)
        if not match:
            raise OrquestraBuildError(
                "the builder answered without a transaction; refusing to guess which "
                "part of its reply was the bytes"
            )
        return match.group(1)

    return idl_fetch, build_call


def fork_blockhash_provider(rpc_url: str, rpc_call: Any = None) -> Callable[[], str]:
    """A provider that reads the blockhash from ONE chain — the fork you name.

    Bound to a single url on purpose: a rehearsal that could take its blockhash from
    somewhere other than the chain it lands on is a rehearsal of nothing.
    """
    from .rpc import default_rpc_call

    call = rpc_call or default_rpc_call

    def provider() -> str:
        result = call(rpc_url, "getLatestBlockhash", [])
        blockhash = ((result.get("result") or {}).get("value") or {}).get("blockhash")
        if not blockhash:
            raise OrquestraBuildError(f"{rpc_url} returned no blockhash")
        return str(blockhash)

    return provider
