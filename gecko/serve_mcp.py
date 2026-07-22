"""Container entrypoint — serve the humanitarian MCP surfaces over Streamable HTTP.

Binds ``0.0.0.0:$PORT`` behind the ALB, ``mode="live"`` (real upstream calls). Serves
MANY comprehended surfaces from one host — each under ``/{name}`` — so an agent adds
``/{name}/mcp`` and finds ``/{name}/llms.txt``, and everyone finds everything on one
server. ``/`` lists what's available; ``/healthz`` is the ALB target.

Most surfaces are public (no auth). One (``refugios``) is gated by a PUBLISHABLE
Supabase ``apikey`` (public by design) — served only when ``REFUGIOS_APIKEY`` is set,
via a static-header session that injects the key at call time (hidden from the agent).
No key ⇒ that surface is simply not served; the repo carries no key.

One surface (``birdeye``) is a PAID third-party API, so it is gated to named developers
holding a minted Gecko key — see :data:`GATED_SURFACES`. That gate applies to those names
ONLY; every public/humanitarian mount stays keyless. The host REFUSES TO BOOT if a
declared-paid surface would be served with the gate off
(:func:`assert_paid_surfaces_are_gated`), and a gated surface is kept out of the anonymous
``/registry/...`` distribution store (its full OpenAPI is not a public artifact).

Every host also exposes the 'submit your API' front doors (wired in by
``build_multi_surface_app``): ``POST /comprehend`` (human page backend, also directly
agent-POST-able) and the ``comprehend_api`` MCP tool at ``/gecko/mcp``. A provider
submits one URL and gets it comprehended into first-call-correct tools, returned to the
submitter only — never hosted or added to ``/`` (no public catalog).

``mcp.geckovision.tech`` is allowlisted for the DNS-rebinding defense: the ALB preserves
that ``Host`` (port-less on 443) on real ``/mcp`` traffic, so the bare hostname must be
present verbatim.

Thin by design — all logic lives in ``gecko.http_server``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .access import public_session, static_session, stub_session
from .client import AgentApiClient
from .enforce import EnforceMode, resolve_hosted_enforce
from .http_server import (
    REQUIRE_GECKO_KEY_ENV,
    resolve_gated_surfaces,
    resolve_require_gecko_key,
    serve_multi_http,
)
from .jito_surface import build_jito_surface
from .mcp_server import McpSurface
from .registry.api import registry_routes as _registry_routes
from .registry.store import RegistrySurface, SurfaceStore
from .registry.wiring import build_keystore_from_env

logger = logging.getLogger("gecko.serve_mcp")

# In the image: /app/gecko/serve_mcp.py -> parents[1] = /app (repo root).
_ROOT = Path(__file__).resolve().parents[1]

# The surfaces this host serves. Each spec is shipped in-image (a local path bypasses
# the SSRF guard by design — trusted, in-image), comprehended live at startup. Add a
# humanitarian API here + ship its spec to expose it at /{name}/mcp.
_SURFACES: list[tuple[str, Path]] = [
    (
        "reportavnzla",
        _ROOT / "examples" / "reportavnzla_demo" / "spec" / "reportavnzla_openapi.json",
    ),
    (
        "sosvenezuela",
        _ROOT / "examples" / "sos_vzla_bot" / "spec" / "sosvenezuela_openapi.json",
    ),
]

# Auth-gated surface: served only when its publishable key is present in the env.
_REFUGIOS_SPEC = _ROOT / "examples" / "refugios_demo" / "spec" / "refugios_openapi.json"

# Gecko-brand DEMO surfaces.
# TxLINE is a paid / mainnet-money API we can't serve live publicly, so it stays RECORDED:
# every response is synthesized from the schema ($0, offline), no real credential is used
# or exposed — the point is first-call-correct comprehension of the exact API Gecko
# pitches; live data needs the caller's own subscription. TxLINE uses a stub session so its
# fully auth-gated ops stay visible as tools (recorded mode never sends the stub header).
#
# Jito is SPLIT (see gecko.jito_surface — the single source of truth for its boundary):
# its four READ ops (getTipFloor + the JSON-RPC status reads — public, keyless, no money,
# no signing) are served LIVE against mainnet, while its two money-moving WRITE ops
# (sendBundle, sendTransaction) stay RECORDED — catalog-only. We are the catalog, not the
# relay: serving those live would make this public endpoint an open MEV relay (control-
# plane violation). The agent takes our first-call-correct comprehension and submits those
# writes DIRECTLY to Jito with its own wallet.
_TXLINE_SPEC = _ROOT / "examples" / "txline_demo" / "spec" / "txline_openapi.yaml"

# Birdeye (Solana/DeFi market data) — a PAID, key-gated API, served in EITHER mode:
#
#   BIRDEYE_API_KEY set   -> LIVE against Birdeye, our key injected at call time.
#   unset / `__unset__`   -> RECORDED ($0, synthesized from Birdeye's own schema).
#
# Live was only ever safe once TWO things were true, and both now are: the surface is in
# GATED_SURFACES (so anonymous traffic cannot reach it at all) and access is granted PER
# ACCOUNT (`gecko keys grant <account> --surface birdeye`), so the quota is spent only by
# developers we named. Serving it live before that would have billed our key for anyone.
#
# Either way all 89 ops stay visible and first-call-correct, and the upstream key never
# appears in a tool def (invariant #4) — the agent describes intent, we inject auth.
# Its 88-path spec is shipped in-image (NOT in the pip wheel — 559KB would bloat the
# zero-friction npx/uvx install), matching the jito/txline hosted pattern.
_BIRDEYE_SPEC = _ROOT / "examples" / "birdeye_demo" / "spec" / "birdeye_openapi.json"
#: Birdeye's own host (from the spec's `servers`). Only used when BIRDEYE_API_KEY is set.
_BIRDEYE_BASE = "https://public-api.birdeye.so"
#: The push-ssm sentinel for "declared but not filled in" — never treat it as a real key.
_UNSET_SENTINEL = "__unset__"

# Jupiter Swap — keyless + PUBLIC, so UNLIKE TxLINE/Jito we serve it LIVE: real swap
# quotes from Jupiter's free lite-api host. No key, no cost, public data — a genuine
# external-call demo (the agent gets real data, not a synthesized sample), which is why
# it's the surface we point external agents at. The hosted risk gate is active on it too.
_JUPITER_SPEC = _ROOT / "gecko" / "examples" / "jupiter_swap_openapi.json"
_JUPITER_BASE = "https://lite-api.jup.ag/swap/v1"  # keyless free-tier host

PUBLIC_HOST = "mcp.geckovision.tech"
PUBLIC_URL = f"https://{PUBLIC_HOST}"

# The ONLY surfaces the Gecko-key gate closes when GECKO_REQUIRE_KEY is on.
#
# `birdeye` is a PAID, key-gated third-party API. Serving a paid surface openly would
# drift Gecko into being a marketplace / payment rail (the thesis explicitly forbids
# both), so it is gated to NAMED developers holding a minted `gecko_sk_…` key
# (`gecko keys mint <account>`).
#
# Everything else stays PUBLIC and keyless — the humanitarian surfaces (reportavnzla,
# sosvenezuela, refugios) are real public-good users, and the keyless demos (txline,
# jito, jupiter, paysh) plus the /comprehend + /gecko/mcp submit doors are the funnel.
# Gating them would close the front door.
#
# Add a future PAID surface here (and only here); `GECKO_GATED_SURFACES` (comma-separated)
# overrides this set at deploy time without a code change.
GATED_SURFACES = frozenset({"birdeye"})


class GateStanceError(RuntimeError):
    """Boot refusal: a DECLARED-PAID surface would be served with the Gecko-key gate OFF.

    Never carries a token or a key — only surface names + the env var to fix.
    """


def assert_paid_surfaces_are_gated(
    surfaces: list[tuple[str, Any]],
    gated: Iterable[str] | None,
    *,
    require_key: bool | None = None,
) -> None:
    """Refuse to boot when a surface DECLARED paid is actually served while the gate is OFF.

    Two independent env vars must both be right — ``GECKO_REQUIRE_KEY`` (the stance) and
    ``GECKO_GATED_SURFACES``/:data:`GATED_SURFACES` (the scope) — and nothing asserted it:
    with the stance unset, ``/birdeye/mcp`` answered 200 to anyone. Serving a PAID
    third-party API openly is the marketplace/rail drift the thesis forbids, so this is
    FATAL rather than a log line.

    The scope is deliberately tight — it fires ONLY for a declared name that is ALSO in
    the served list:

    * a public-only deploy can never be blocked by it (the humanitarian mounts and the
      keyless demos are the funnel; a paid-surface guard must not take them down);
    * a declared name this host does not serve is NOT fatal — that stays the existing
      inert-but-LOUD ERROR case in ``build_multi_surface_app`` (an operator may
      forward-declare a future paid surface);
    * ``gated is None`` (the library "gate every mount" default) declares nothing paid.

    Matching is case-insensitive, exactly like the gate itself: folding can only ever
    catch MORE, never fewer.
    """
    if gated is None or resolve_require_gecko_key(require_key):
        return
    served = {name.casefold(): name for name, _ in surfaces}
    exposed = sorted(served[n.casefold()] for n in gated if n.casefold() in served)
    if not exposed:
        return
    message = (
        f"refusing to start: {exposed} declared PAID (gated) and served, but "
        f"{REQUIRE_GECKO_KEY_ENV} is OFF — those mounts would answer anyone, with no "
        f"Gecko key. Fix: set {REQUIRE_GECKO_KEY_ENV}=1 (and wire the key registry via "
        "MONGODB_URI so minted gecko_sk_ keys verify), or stop serving them."
    )
    logger.critical("%s", message)
    raise GateStanceError(message)


def _build_surfaces(hosted_enforce: EnforceMode) -> list[tuple[str, Any]]:
    """The surfaces this host serves over MCP — factored out of ``main()`` so tests
    can build the exact list ``_registry_store`` sees, without starting a server."""
    surfaces: list[tuple[str, Any]] = [
        (name, json.loads(path.read_text("utf-8"))) for name, path in _SURFACES
    ]
    # Recorded brand demo surfaces (pre-built so their mode overrides the host default).
    # Built with the hosted enforce stance so the risk gate is active on them too.
    surfaces.append(
        (
            "txline",
            McpSurface(
                AgentApiClient(str(_TXLINE_SPEC), session=stub_session()),
                mode="recorded",
                enforce=hosted_enforce,
            ),
        )
    )
    # Birdeye — paid/key-gated (see _BIRDEYE_SPEC). 89 first-call-correct tools.
    #
    # LIVE only when BIRDEYE_API_KEY is set, otherwise RECORDED ($0, schema-synthesized).
    # Fail-SAFE, not fail-closed: a missing/sentinel key degrades to recorded rather than
    # erroring, so an SSM slip can never take the surface down — and, more importantly,
    # can never silently start spending on a key we do not have.
    #
    # Live mode spends real Birdeye quota on every call, and agents call far more often
    # than humans do. Two things bound that: the Gecko-key gate (only accounts explicitly
    # granted `birdeye` reach this mount at all) and the fact that the upstream key is
    # injected at call time and never appears in a tool def (invariant #4).
    birdeye_key = os.environ.get("BIRDEYE_API_KEY", "").strip()
    birdeye_live = bool(birdeye_key) and birdeye_key != _UNSET_SENTINEL
    surfaces.append(
        (
            "birdeye",
            McpSurface(
                AgentApiClient(
                    str(_BIRDEYE_SPEC),
                    base_url=_BIRDEYE_BASE if birdeye_live else None,
                    session=(
                        static_session({"X-API-KEY": birdeye_key})
                        if birdeye_live
                        else stub_session()
                    ),
                ),
                mode="live" if birdeye_live else "recorded",
                enforce=hosted_enforce,
            ),
        )
    )
    logger.info("birdeye surface mode=%s", "live" if birdeye_live else "recorded")
    # Jito — reads LIVE against mainnet, the two money-moving writes RECORDED
    # (catalog-only). Built via the shared boundary builder so serve_mcp and
    # serve_providers enforce the identical split; hosted enforce keeps the risk gate on.
    surfaces.append(("jito", build_jito_surface(hosted_enforce)))
    # Jupiter Swap — served LIVE (keyless, public): a real external-call demo. base_url is
    # the free-tier host; public_session (no auth) means no secret can leak, and all four
    # ops are ungated so they're visible. The risk gate still runs.
    surfaces.append(
        (
            "jupiter",
            McpSurface(
                AgentApiClient(
                    str(_JUPITER_SPEC),
                    base_url=_JUPITER_BASE,
                    session=public_session(),
                ),
                mode="live",
                enforce=hosted_enforce,
            ),
        )
    )
    # Refugios (shelters) — comprehended with the publishable apikey injected as a
    # static header. Passed as a CLIENT (not a bare spec) so the multi-surface builder
    # uses its session; the key is invisible to the agent.
    refugios_key = os.environ.get("REFUGIOS_APIKEY", "").strip()
    if refugios_key:
        surfaces.append(
            (
                "refugios",
                AgentApiClient(
                    str(_REFUGIOS_SPEC),
                    session=static_session({"apikey": refugios_key}),
                ),
            )
        )
    return surfaces


def _build_paysh_surface() -> tuple[Any, Any]:
    """Build the aggregated pay.sh catalog surface from the LIVE catalog once at startup.

    Returns ``(registry, surface)``. The fetch is SSRF-safe (``fetch_catalog`` runs
    ``validate_public_url``) and the catalog is treated as untrusted. If the live fetch
    FAILS at boot, we log and serve an EMPTY surface rather than crashing the whole server
    — the hourly self-refresh loop will populate it on the next successful tick. Imports
    are lazy (mirrors the colosseum pattern) so the catalog modules stay off the hot
    import path until this host actually serves pay.sh."""
    from gecko.catalog_mcp import CatalogMcpSurface
    from gecko.paysh_catalog import CatalogRegistry, fetch_catalog

    try:
        entries = fetch_catalog()
    except Exception:  # noqa: BLE001 - a boot fetch failure must not crash the server
        logger.warning(
            "pay.sh catalog fetch failed at boot; serving empty catalog surface"
        )
        entries = []
    registry = CatalogRegistry.build(entries)
    return registry, CatalogMcpSurface(registry)


def _surface_spec(value: Any) -> dict[str, Any] | None:
    """Recover the raw OpenAPI dict behind a ``_build_surfaces`` entry, whatever shape
    it was built in: a bare parsed spec (reportavnzla/sosvenezuela), an ``McpSurface``
    wrapping a client (txline/jito), or a bare ``AgentApiClient`` (refugios).

    Returns ``None`` for a spec-less AGGREGATE surface (the pay.sh catalog is 70 pinned
    clients with NO single OpenAPI document) — the caller skips it rather than fabricating
    a fake unified spec or crashing the registry routes."""
    if isinstance(value, McpSurface):
        return value.client.spec
    if isinstance(value, AgentApiClient):
        return value.spec
    if isinstance(value, dict):
        return value
    return None


def _registry_store(
    surfaces: list[tuple[str, Any]],
    *,
    exclude: Iterable[str] = frozenset(),
) -> SurfaceStore:
    """Build the /registry/... SurfaceStore from every MCP-hosted surface, PLUS
    colosseum — which is registry-DISTRIBUTED (its console-entry runner fetches
    "colosseum" from here, see gecko/examples/colosseum.py) but deliberately not
    hosted as an MCP surface on this server (it's a BYOK console entry the operator
    runs themselves). Registry distribution != MCP hosting.

    ``exclude`` drops names from the store entirely — the hosted server passes the GATED
    (paid) set. The registry is the ANONYMOUS distribution plane: every route there is
    keyless by design and it has no Gecko-key seam, so a gated surface sitting in the
    store served its FULL OpenAPI spec to anyone at ``/registry/surfaces/{name}``
    (measured: 71KB), entirely outside the mount's gate. Excluding it is the cleanest
    mechanism because it fixes fetch, listing AND ``/registry/search`` at one point —
    they all read ``store.names()`` — and it leaves the free/public distribution path
    byte-identical. Marking it ``tier="premium"`` instead would still ADVERTISE the paid
    name (and 402 rather than deny), which is the leak we are closing. The result is a
    clean 404 ``unknown_surface``: identical to a name that was never registered, so the
    registry gives an anonymous prober no oracle.

    The colosseum import is lazy and reads only packaged data (no network) — it just
    keeps that console-entry module out of this server's import graph until needed.
    """
    from gecko.examples.colosseum import load_spec as _load_colosseum_spec

    # Case-insensitive, like the gate itself: folding can only ever exclude MORE.
    hidden = {name.casefold() for name in exclude}
    docs = [
        RegistrySurface(name=name, spec=spec, tier="free")
        for name, value in surfaces
        if name.casefold() not in hidden
        and (spec := _surface_spec(value))
        is not None  # aggregate surfaces have no spec
    ]
    docs.append(
        RegistrySurface(name="colosseum", spec=_load_colosseum_spec(), tier="free")
    )
    return SurfaceStore(docs)


def main() -> None:  # pragma: no cover - run-the-server entrypoint
    from gecko import paysh_watch
    from gecko.paysh_catalog import challenge_probe, fetch_catalog

    port = int(os.environ.get("PORT", "8000"))
    # Hosted default resolved in the ONE shared place (resolve_hosted_enforce): block,
    # unless GECKO_ENFORCE dials it down (needs a redeploy).
    hosted_enforce = resolve_hosted_enforce()
    surfaces = _build_surfaces(hosted_enforce)
    # Resolve the gate SCOPE once (env override -> the hosted default) and reuse it for
    # every downstream decision, so the mounts, the registry store and this guard can
    # never read a different set.
    gated = resolve_gated_surfaces(default=GATED_SURFACES)
    # BOOT GUARD (R2): a declared-PAID surface must never be served with the gate OFF.
    # Deliberately BEFORE any network work below, so a misconfigured deploy dies fast.
    assert_paid_surfaces_are_gated(surfaces, gated)
    # Registry store for the /registry/... HTTP surface, built from the SINGLE-SPEC
    # surfaces only (before pay.sh is appended) — the aggregate pay.sh catalog has no
    # single OpenAPI document, so it is not registry-distributed (see `_surface_spec`).
    # `build_keystore_from_env()` wires a real Mongo-backed KeyStore when
    # MONGODB_URI + GECKO_OTP_FROM are set; it fails soft to None (issuance disabled,
    # 402-on-premium-never) rather than crashing the server.
    # GATED (paid) surfaces are excluded: the registry plane is anonymous by design, so
    # leaving one in would serve its full OpenAPI spec outside the gate (see _registry_store).
    registry_store = _registry_store(surfaces, exclude=gated or frozenset())

    # pay.sh catalog — the aggregate surface, mounted at /paysh/mcp ALONGSIDE the rest.
    # Built once from the live catalog; the served mode stays "recorded" (the surface
    # carries it), so this MCP never triggers a live payment.
    paysh_registry, paysh_surface = _build_paysh_surface()
    surfaces = surfaces + [("paysh", paysh_surface)]

    # Hourly self-refresh drift-watch: Tier-1 sha-diff refresh + Tier-2 challenge-only
    # 402 re-probe, mutating the registry in place so /paysh/mcp reflects the fresh state.
    async def _paysh_worker() -> None:
        await paysh_watch.watch_loop(
            paysh_registry,
            interval=paysh_watch.refresh_seconds(),
            fetch=fetch_catalog,
            probe=challenge_probe,
        )

    serve_multi_http(
        surfaces,
        host="0.0.0.0",  # noqa: S104 - bind all interfaces; the ALB fronts it
        port=port,
        mode="live",
        allowed_hosts=[PUBLIC_HOST],
        public_url=PUBLIC_URL,
        enforce=hosted_enforce,
        registry_routes=_registry_routes(
            registry_store,
            build_keystore_from_env(),
            feedback_path=os.environ.get("GECKO_FEEDBACK_PATH"),
        ),
        background_tasks=[_paysh_worker],
        # Gate ONLY the paid surfaces (env can override; see GATED_SURFACES). Without
        # this, GECKO_REQUIRE_KEY=on would 403 the humanitarian + keyless demo mounts too.
        gated_surfaces=gated,
    )


if __name__ == "__main__":
    main()
