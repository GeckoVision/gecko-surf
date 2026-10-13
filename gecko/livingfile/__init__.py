"""A living file per tokenized stock (xStocks on Solana), for lenders.

One file per mint that says which sources agree, which do not, and the proof, updated
on every run: supply, authorities, token program and multiplier from the chain; token
price and liquidity from Jupiter; the real share price from Financial Datasets through
Orthogonal (the one paid call, capped); the legal pages hashed and versioned; reserves
when a feed exists. Checks are computed in code and turn red when they fail.

Why its own package and not ``orthogonal/xstock``: four of its five sources are not
Orthogonal. It reuses the lists core rather than copying it — the keyring session,
allowlist, spend guard and runner (``orthogonal``), and the store, Parquet, field
ledger, history and status vocabulary (``orthogonal.supplier``).

Entry points: ``job.plan_file`` / ``job.run_file``, ``deps.xstock_deps_for``, the read
tools ``xstock_file`` / ``xstock_changes_since`` on ``gecko serve --lists --stdio``, and
``scripts/xstock_file.py``. Submodules are imported directly: this package stays
import-light so ``orthogonal.surface`` can load it lazily without a cycle.
"""
