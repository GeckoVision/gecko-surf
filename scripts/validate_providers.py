"""Print the multi-provider validation matrix (offline, $0). Thin wrapper — all logic
lives in ``gecko.provider_matrix``. Run: ``uv run python -m scripts.validate_providers``
or ``uv run python scripts/validate_providers.py``."""

from __future__ import annotations

from gecko.provider_matrix import (
    MINT_ENTITY,
    all_reports,
    exit_liquidity_chain,
    format_matrix,
    mint_correlation,
)


def main() -> None:
    reports = all_reports()
    print("=" * 78)
    print("MULTI-PROVIDER VALIDATION MATRIX (offline, $0)")
    print("=" * 78)
    print(format_matrix(reports))

    failed = [r for r in reports if not r.comprehended]
    auth = [r for r in reports if r.auth_exposed]
    quarantined = [r for r in reports if r.quarantined]
    print()
    print(
        f"comprehended: {len(reports) - len(failed)}/{len(reports)}  |  "
        f"auth-exposed surfaces: {len(auth)} (must be 0)  |  "
        f"surfaces with a quarantine: {len(quarantined)}"
    )

    print()
    print("=" * 78)
    print(f"CROSS-PROVIDER MINT CORRELATION  (entity: {MINT_ENTITY})")
    print("=" * 78)
    corr = mint_correlation()
    print(f"entities in index: {list(corr.entities)}")
    print(
        f"producers ({len(corr.producers)}): "
        + ", ".join(f"{s}.{o}.{f}" for s, o, f in corr.producers)
    )
    print(
        f"consumers ({len(corr.consumers)}): "
        + ", ".join(f"{s}.{o}.{f}" for s, o, f in corr.consumers)
    )
    print("cross-provider joins found:")
    for prod, cons, entity, eligible in corr.cross_joins:
        tag = "plan-eligible" if eligible else "candidate"
        print(f"  {prod} -> {cons}  [{entity}]  ({tag})")

    print()
    print("=" * 78)
    print("SAFE CHAIN  (Pegana -> Birdeye exit-liquidity)")
    print("=" * 78)
    chain = exit_liquidity_chain()
    if chain is None:
        print("NO CHAIN (cross_plan returned None)")
    else:
        print(f"target: {chain.target_surface}.{chain.target_tool}")
        print(f"hops: {' -> '.join(f'{n.surface_id}.{n.tool}' for n in chain.nodes)}")
        print(f"join basis: {list(chain.join_basis)}")
        print(
            f"complete={chain.complete}  refused={chain.refused}  safe={chain.safe}  "
            f"quarantined_hops={[n.tool for n in chain.quarantined_nodes]}"
        )


if __name__ == "__main__":
    main()
