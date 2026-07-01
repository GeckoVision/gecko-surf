"""PyInstaller entry point for the standalone ``gecko`` binary.

This is the frozen-binary equivalent of the ``gecko`` console script
(``gecko.cli:_run`` — the serve/test/from-docs dispatcher). PyInstaller needs a real
module path to point ``--onefile`` at; the console-script entry point is invisible to
it. Keep this file a pure shim — all logic stays in the package (``gecko.cli``).
"""

from gecko.cli import _run

if __name__ == "__main__":
    _run()
