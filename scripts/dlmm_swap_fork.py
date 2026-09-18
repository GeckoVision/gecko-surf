"""Land one Meteora DLMM swap on a fork, judged by what moved.

    uv run python scripts/dlmm_swap_fork.py --rpc-url http://127.0.0.1:8899 \\
        --input So11111111111111111111111111111111111111112 \\
        --output Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump \\
        --bin-step 250 --base-factor 4000 --amount 10000000

Fork only: the signer is an ephemeral key that exists because the endpoint proved it is
a fork. Mainnet is the founder's, through the production tools.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.sandbox import ephemeral_signer, prove_surfnet  # noqa: E402
from gecko.sandbox.rehearse_swap import rehearse_dlmm_swap  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rpc-url", required=True)
    ap.add_argument("--input", required=True, help="input mint")
    ap.add_argument("--output", required=True, help="output mint")
    ap.add_argument("--bin-step", type=int, required=True)
    ap.add_argument("--base-factor", type=int, required=True)
    ap.add_argument("--amount", type=int, required=True, help="input base units")
    args = ap.parse_args(argv)

    proof = prove_surfnet(args.rpc_url)
    signer = ephemeral_signer(proof)
    print(f"  fork proven   {proof.rpc_url}")
    print(f"  signer        {signer.pubkey}  (ephemeral)")
    r = rehearse_dlmm_swap(
        proof,
        signer=signer,
        bindings={
            "input_mint": args.input,
            "output_mint": args.output,
            "bin_step": args.bin_step,
            "base_factor": args.base_factor,
            "amount_in": args.amount,
        },
    )
    print(f"  pool          {r.pool}  bin arrays {list(r.bin_array_indexes)}")
    print(
        f"  quote         expected {r.expected_out_snapshot}  floor {r.min_amount_out}"
    )
    for refusal in r.refusals:
        print(f"  REFUSED at {refusal.step}: {refusal.reason}")
    if not r.landed:
        return 1
    print(f"  LANDED        {r.signature}")
    print(
        f"  CU            simulated {r.simulated_units}  limit {r.unit_limit}  charged {r.units_consumed}"
    )
    print(
        f"  token out     {r.token_out.before if r.token_out else None} -> {r.token_out.after if r.token_out else None}  (received {r.received} by {r.received_witness})"
    )
    print(
        f"  signer SOL    moved {r.signer_sol.moved if r.signer_sol else None}  (fee {r.fee_lamports})"
    )
    for line in r.discrepancies:
        print(f"  DISCREPANCY   {line}")
    print(f"  reset         {len(r.reset)} accounts restored")
    return 0 if not r.discrepancies else 1


if __name__ == "__main__":
    sys.exit(main())
