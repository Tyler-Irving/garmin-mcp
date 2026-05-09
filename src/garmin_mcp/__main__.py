"""Production entry point. Runs the HTTP server with auth.

``python -m garmin_mcp`` from a shell, or via the Dockerfile's CMD on
Cloud Run.
"""

from __future__ import annotations

from garmin_mcp.server import run_http


def main() -> None:
    run_http()


if __name__ == "__main__":
    main()
