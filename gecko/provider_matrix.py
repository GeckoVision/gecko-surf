"""Multi-provider validation matrix — "can Gecko actually connect to MANY providers?"

The repeatable, offline ($0, Pattern B) evidence that the comprehension engine ingests
a *universe* of real, committed provider specs — not one hand-picked API — and, for each,
derives an agent surface that (a) comprehends without raising, (b) NEVER exposes an auth
header to the agent (invariant #4), and (c) reports its Skill-Guard quarantine honestly.

This module is pure comprehension + reporting over the shipped engine — it invents no new
inference. It builds one :class:`~gecko.surface.Surface` per spec (the no-auth public
session, so "tools usable" is the honest with-zero-credentials view) and reads:

  * ``ingest.extract_operations``  -> #operations comprehended
  * ``surface.client.tools``       -> #question-shaped tool defs derived (auth hidden)
  * ``surface.tools()``            -> #usable with no credentials (auth-gated ops hidden)
  * ``surface.safety.quarantined`` -> Skill-Guard per-tool quarantine (the anti-poisoning)
  * a scan of every surfaced tool's input schema -> auth-exposed count (MUST be 0)
  * ``value_domain_index``         -> the DECLARED value-domain entities the surface joins on

Honesty is law: a spec that quarantines, exposes auth, or surfaces nothing without
credentials is reported AS SUCH — the matrix surfaces what does NOT cleanly connect, it
never loosens a rule to go green. Control-plane only (invariant #1): every field is a
NAME or a count, never a response payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import ingest
from .access import public_session
from .correlate import CorrelationResult, correlate_surfaces
from .safechain import SafeChainResult, compose_safe_chain
from .surface import Surface
from .vindex import ValueDomainIndex, value_domain_index

#: The repo root — this module lives in ``gecko/``.
_ROOT = Path(__file__).resolve().parents[1]

#: The committed provider universe: display name -> spec path (relative to repo root).
#: These are the SAME specs shipped in the repo's examples / fixtures; the harness never
#: fetches anything, so the whole matrix is deterministic and $0.
PROVIDERS: dict[str, str] = {
    "birdeye": "examples/birdeye_demo/spec/birdeye_openapi.json",
    "payapi": "examples/charge_before_live/spec/payapi_openapi.json",
    "jito": "examples/jito_demo/spec/jito_openapi.json",
    "jito_blockengine": "examples/jito/spec/jito_blockengine_openapi.json",
    "pegana_api2": "examples/pegana_demo/spec/pegana_openapi.json",
    "refugios": "examples/refugios_demo/spec/refugios_openapi.json",
    "reportavnzla": "examples/reportavnzla_demo/spec/reportavnzla_openapi.json",
    "sosvenezuela": "examples/sos_vzla_bot/spec/sosvenezuela_openapi.json",
    "surfpool": "examples/surfpool/spec/surfpool_openapi.json",
    "txline": "examples/txline_demo/spec/txline_openapi.yaml",
    "colosseum": "gecko/examples/colosseum_copilot_openapi.json",
    "jupiter": "gecko/examples/jupiter_swap_openapi.json",
    "privy": "tests/fixtures/golden/privy_openapi.json",
    "pegana_p0": "tests/fixtures/pegana_p0_openapi.json",
}

#: The Solana-token-mint value domain and its normalized entity (``vindex`` vocabulary).
MINT_VD = "solana-token-mint"
MINT_ENTITY = "solanatokenmint"

#: The customer-CONFIRMED mint vocabulary each mint provider declares — the offline
#: equivalent of ``gecko graph confirm``. Each spec NAMES the mint differently, which is
#: exactly why only a DECLARED value-domain join (never name/signature) bridges them.
#: A provider absent here does NOT participate in the mint domain (honest: a wallet
#: ``address`` is not a token mint without a customer declaration).
MINT_HINTS: dict[str, dict[str, str]] = {
    "pegana_p0": {"mint": MINT_VD},
    "birdeye": {"address": MINT_VD, "token_address": MINT_VD, "list_address": MINT_VD},
    "jupiter": {"inputMint": MINT_VD, "outputMint": MINT_VD},
}

#: Auth-shaped input-property names that must NEVER appear on an agent-facing tool.
_AUTH_KEYS = {"authorization", "x-api-key", "api_key", "apikey", "token"}

#: Birdeye's mint-consuming, never-emitted param — forces a genuine cross-surface chain.
_BIRDEYE_EXIT_OP = "get-defi-multi_price"


@dataclass(frozen=True)
class ProviderReport:
    """One row of the matrix — control-plane only (names + counts, never a payload)."""

    name: str
    spec_path: str
    operations: int
    tools_total: int  # every question-shaped tool def derived (auth hidden inside)
    tools_usable: int  # usable with NO credentials (auth-gated ops hidden)
    quarantined: tuple[str, ...]  # Skill-Guard per-tool quarantine (anti-poisoning)
    auth_exposed: tuple[
        str, ...
    ]  # surfaced tools exposing an auth header (MUST be empty)
    value_domains: tuple[
        str, ...
    ]  # DECLARED value-domain entities this surface joins on
    error: str | None = None

    @property
    def comprehended(self) -> bool:
        return self.error is None

    @property
    def mint_domain(self) -> bool:
        return MINT_ENTITY in self.value_domains


def _surface_for(name: str, path: Path) -> Surface:
    """Build the agent surface for one provider under the no-auth public session, with the
    customer-confirmed mint hints applied when the provider is a declared mint participant.
    """
    return Surface.from_spec(
        str(path),
        session=public_session(),
        surface_id=name,
        declared_hints=MINT_HINTS.get(name, {}),
    )


def _auth_exposed_tools(surface: Surface) -> tuple[str, ...]:
    """The surfaced tools that leak an auth-shaped input property to the agent.

    Invariant #4: this must be empty for every provider — auth is injected at call time,
    never described to the agent. (Auth-gated ops are hidden entirely by the no-auth
    session; this catches the stronger failure of a *surfaced* tool exposing a credential.)
    """
    bad: list[str] = []
    for tool in surface.tools():
        props = tool.get("input_schema", {}).get("properties", {})
        if any(str(k).lower() in _AUTH_KEYS for k in props):
            bad.append(str(tool["name"]))
    return tuple(sorted(bad))


def comprehend(name: str, rel_path: str) -> ProviderReport:
    """Comprehend one committed spec into a matrix row. Never raises — a spec that fails
    to comprehend is reported with its ``error`` set (an honest RED row), not swallowed."""
    path = _ROOT / rel_path
    try:
        operations = len(ingest.extract_operations(ingest.load_spec(str(path))))
        surface = _surface_for(name, path)
        entities = value_domain_index([surface]).entities()
        return ProviderReport(
            name=name,
            spec_path=rel_path,
            operations=operations,
            tools_total=len(surface.client.tools),
            tools_usable=len(surface.tools()),
            quarantined=surface.safety.quarantined,
            auth_exposed=_auth_exposed_tools(surface),
            value_domains=entities,
        )
    except Exception as exc:  # noqa: BLE001 — the harness reports failures, never hides them
        return ProviderReport(
            name=name,
            spec_path=rel_path,
            operations=0,
            tools_total=0,
            tools_usable=0,
            quarantined=(),
            auth_exposed=(),
            value_domains=(),
            error=f"{type(exc).__name__}: {exc}",
        )


def all_reports() -> list[ProviderReport]:
    """The whole provider universe, one :class:`ProviderReport` each (spec order)."""
    return [comprehend(name, path) for name, path in PROVIDERS.items()]


@dataclass(frozen=True)
class MintCorrelation:
    """The cross-provider mint index + the pairwise Pegana↔Birdeye DECLARED edge."""

    entities: tuple[str, ...]
    producers: tuple[tuple[str, str, str], ...]  # (surface, op, field)
    consumers: tuple[tuple[str, str, str], ...]
    cross_joins: tuple[
        tuple[str, str, str, bool], ...
    ]  # (producer_sfc, consumer_sfc, entity, plan_eligible)
    pegana_birdeye: CorrelationResult = field(repr=False)


def _mint_surface(name: str) -> Surface:
    return _surface_for(name, _ROOT / PROVIDERS[name])


def mint_index() -> ValueDomainIndex:
    """The value-domain index over the three declared Solana-mint providers."""
    surfaces = [_mint_surface(n) for n in ("pegana_p0", "birdeye", "jupiter")]
    return value_domain_index(surfaces)


def mint_correlation() -> MintCorrelation:
    """The cross-provider mint join across Pegana↔Birdeye↔Jupiter, with provenance.

    Reuses the shipped ``value_domain_index`` / ``correlate_surfaces`` — the join exists
    ONLY as a customer-DECLARED value-domain edge (the specs name the mint differently and
    ship no ``x-gecko`` entity), so this is the honest "one value-domain -> many services".
    """
    index = mint_index()
    group = index.group(MINT_ENTITY)
    producers = (
        tuple((r.surface_id, r.op_id, r.field) for r in group.producers)
        if group
        else ()
    )
    consumers = (
        tuple((r.surface_id, r.op_id, r.field) for r in group.consumers)
        if group
        else ()
    )
    joins = index.find_correlations(MINT_ENTITY)
    cross = tuple(
        (j.producer.surface_id, j.consumer.surface_id, j.entity, j.plan_eligible)
        for j in joins
        if j.producer.surface_id != j.consumer.surface_id
    )
    pair = correlate_surfaces(_mint_surface("pegana_p0"), _mint_surface("birdeye"))
    return MintCorrelation(
        entities=index.entities(),
        producers=producers,
        consumers=consumers,
        cross_joins=cross,
        pegana_birdeye=pair,
    )


def exit_liquidity_chain() -> SafeChainResult | None:
    """The flagship Pegana → Birdeye exit-liquidity safe chain (offline).

    Pegana (peg-state oracle) supplies the mint that Birdeye's exit-liquidity op consumes;
    the shipped ``compose_safe_chain`` recovers it as a first-plan-correct, keyless chain.
    Returns the composed result (``complete``, no quarantined hop) — or ``None`` if no
    confident plan exists (an honest "no chain", never a speculative join).

    Birdeye is confirmed on ``list_address`` ONLY (not its self-producible ``address``) so
    the exit-liquidity input can only be sourced ACROSS the boundary from Pegana — forcing
    a genuine cross-surface chain rather than an intra-Birdeye self-supply. This mirrors the
    shipped ``test_pegana_safety_chain`` setup exactly.
    """
    surfaces = {
        "pegana_p0": Surface.from_spec(
            str(_ROOT / PROVIDERS["pegana_p0"]),
            session=public_session(),
            surface_id="pegana_p0",
            declared_hints={"mint": MINT_VD},
        ),
        "birdeye": Surface.from_spec(
            str(_ROOT / PROVIDERS["birdeye"]),
            session=public_session(),
            surface_id="birdeye",
            declared_hints={"list_address": MINT_VD},
        ),
    }
    return compose_safe_chain(surfaces, "birdeye", _BIRDEYE_EXIT_OP)


# --- reporting -------------------------------------------------------------------------
def format_matrix(reports: list[ProviderReport]) -> str:
    """The one-row-per-provider matrix as a printable table (provider → ops / tools /
    quarantined / auth-hidden / mint-domain)."""
    header = (
        f"{'provider':16} {'ops':>4} {'tools':>6} {'usable':>7} "
        f"{'quar':>5} {'auth!':>6} {'mint':>5}  value-domains / notes"
    )
    lines = [header, "-" * len(header)]
    for r in reports:
        if not r.comprehended:
            lines.append(
                f"{r.name:16} {'--':>4} {'--':>6} {'--':>7} {'--':>5} "
                f"{'--':>6} {'--':>5}  DID NOT COMPREHEND: {r.error}"
            )
            continue
        auth_flag = "FAIL" if r.auth_exposed else "0"
        mint = "yes" if r.mint_domain else "-"
        notes = ",".join(r.value_domains) if r.value_domains else ""
        if r.quarantined:
            notes = (
                notes + "  " if notes else ""
            ) + f"quarantined={list(r.quarantined)}"
        lines.append(
            f"{r.name:16} {r.operations:>4} {r.tools_total:>6} {r.tools_usable:>7} "
            f"{len(r.quarantined):>5} {auth_flag:>6} {mint:>5}  {notes}"
        )
    return "\n".join(lines)
