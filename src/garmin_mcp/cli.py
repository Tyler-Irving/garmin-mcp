"""Command-line entry point.

Subcommands:
    (no args) / stdio   Run MCP over stdio. The default; this is what Claude
                        Desktop launches.
    serve               Run MCP over streamable-HTTP (self-hosted remote).
    login               Interactive Garmin login. Handles MFA. Saves session
                        tokens so the stdio server can resume without
                        prompting.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from garmin_mcp import __version__
from garmin_mcp.paths import default_token_dir


def cmd_stdio(_args: argparse.Namespace) -> None:
    from garmin_mcp.server import run_stdio

    run_stdio()


def cmd_serve(_args: argparse.Namespace) -> None:
    from garmin_mcp.server import run_http

    run_http()


def cmd_login(args: argparse.Namespace) -> None:
    email = args.email or os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = args.password or os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        sys.exit(1)

    token_dir = Path(args.token_dir or default_token_dir())
    token_dir.mkdir(parents=True, exist_ok=True)

    from garminconnect import Garmin

    client = Garmin(email=email, password=password, return_on_mfa=True)
    result = client.login(str(token_dir))

    # garminconnect returns ("needs_mfa", client_state) when MFA is required.
    if isinstance(result, tuple) and result and result[0] == "needs_mfa":
        mfa_code = input("Garmin MFA code: ").strip()
        client.resume_login(result[1], mfa_code)
        # resume_login does not write the tokenstore; dump explicitly.
        client.client.dump(str(token_dir))

    print(f"Logged in. Session tokens saved to {token_dir}.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin-mcp",
        description="MCP server exposing Garmin Connect data to Claude.",
    )
    parser.add_argument("--version", action="version", version=f"garmin-mcp {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_stdio = sub.add_parser(
        "stdio",
        help="Run MCP over stdio (default; for Claude Desktop).",
    )
    p_stdio.set_defaults(func=cmd_stdio)

    p_serve = sub.add_parser(
        "serve",
        help="Run MCP over streamable-HTTP (self-hosted remote).",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_login = sub.add_parser(
        "login",
        help="Interactive Garmin login. Run this once if your account has MFA.",
    )
    p_login.add_argument("--email", help="Garmin email (else uses $GARMIN_EMAIL or prompts).")
    p_login.add_argument(
        "--password",
        help="Garmin password (else uses $GARMIN_PASSWORD or prompts).",
    )
    p_login.add_argument(
        "--token-dir",
        help="Where to write the session tokens.",
    )
    p_login.set_defaults(func=cmd_login)

    return parser


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", cmd_stdio)
    func(args)


if __name__ == "__main__":
    main()
