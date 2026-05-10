"""Shared filesystem path helpers."""

from __future__ import annotations

import os
from pathlib import Path


def default_token_dir() -> str:
    """Directory where Garmin OAuth session tokens are persisted.

    Order of resolution:
        1. ``$GARMIN_TOKEN_DIR`` if set (Cloud Run image sets this to /tmp/garth).
        2. Per-user cache dir via ``platformdirs`` (macOS, Linux, Windows).
        3. ``~/.cache/garmin-mcp/garth`` as a last resort.
    """
    env = os.getenv("GARMIN_TOKEN_DIR")
    if env:
        return env
    try:
        from platformdirs import user_cache_dir

        return str(Path(user_cache_dir("garmin-mcp")) / "garth")
    except ImportError:
        return str(Path.home() / ".cache" / "garmin-mcp" / "garth")
