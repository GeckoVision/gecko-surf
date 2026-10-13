"""Gecko's own service registry ("servicemap"): services, operations, the catalogs that
list them, the fields real replies carry, and their health. Public metadata only.

Submodules are imported by name; this package imports nothing so that
:mod:`gecko.anyquery.types` and the supplier source map can take their shared types
from :mod:`gecko.servicemap.types` without a cycle.
"""
