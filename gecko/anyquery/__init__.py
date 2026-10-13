"""Any query: a user's question becomes plan data, runs through the Orthogonal core, and
is paid once, after the run, at the price the user confirmed.

The model only proposes a plan; code validates it against the catalog and runs it.
Every shared type lives in :mod:`gecko.anyquery.types` and nowhere else.
"""
