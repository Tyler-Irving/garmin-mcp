"""Module entry point.

``python -m garmin_mcp`` dispatches through the same CLI as the
``garmin-mcp`` script: defaults to stdio, supports ``serve`` for HTTP, and
``login`` for an interactive Garmin login.
"""

from __future__ import annotations

from garmin_mcp.cli import main

if __name__ == "__main__":
    main()
