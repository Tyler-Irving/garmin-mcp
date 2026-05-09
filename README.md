# garmin-mcp

A remote MCP (Model Context Protocol) server that exposes your Garmin Connect data to Claude as tools. Add it once as a custom connector in Claude.ai (web, desktop, mobile) and you can ask questions like "how did I sleep last night?" or "summarise my training load this week" without copy-pasting screenshots from the Garmin app.

It is single-user, read-only, and designed to deploy to Google Cloud Run for under one dollar a month.

## What it does

The server logs into Garmin Connect on your behalf using the unofficial [`garminconnect`](https://pypi.org/project/garminconnect/) Python library, caches the session tokens, and exposes a fixed set of tools over the MCP streamable-HTTP transport. Claude calls those tools when you ask a question that needs Garmin data.

## Tools

| Tool                       | What it returns                                                              |
| -------------------------- | ---------------------------------------------------------------------------- |
| `get_sleep`                | Sleep duration, stages (deep / light / REM / awake), score, overnight HRV.   |
| `get_recent_activities`    | List of recent activities with type, duration, distance, average heart rate. |
| `get_activity_details`     | Full metrics for one activity, including splits, HR zones, and power.        |
| `get_training_load`        | Daily training load with acute (ATL), chronic (CTL), and current status.     |
| `get_hrv_status`           | Current HRV status, baseline range, and the last 7 nights of readings.       |
| `get_body_battery`         | Body battery values across the day with min, max, charged, drained.          |
| `get_steps_and_calories`   | Daily step count, distance, calories, floors, and intensity minutes.         |
| `get_resting_heart_rate`   | Resting heart rate trend and average over the requested window.              |
| `get_stress`               | Stress levels across the day and time-in-zone breakdown.                     |

Every response is a Pydantic model serialised to JSON, with `null` for fields Garmin did not record.

## Quick start (local)

Requirements: Python 3.12 or later and [`uv`](https://docs.astral.sh/uv/).

```bash
# 1. Install dependencies
uv sync

# 2. Configure credentials
cp .env.example .env
# edit .env with your Garmin login

# 3. Inspect the tools in the MCP dev inspector
uv run mcp dev src/garmin_mcp/server.py
```

`mcp dev` runs the server over stdio and opens the MCP inspector in your browser. From there you can call any tool and see the JSON it returns. Auth is skipped in stdio mode, so you only need the Garmin credentials set.

To run the production HTTP server locally:

```bash
uv run python -m garmin_mcp
# server listens on http://localhost:8080/mcp
```

## Deploying

See [DEPLOY.md](DEPLOY.md) for the full Cloud Run walkthrough, including building the container, configuring environment variables, and adding the deployed URL as a custom connector in Claude.

## How auth works

The server is its own OAuth 2.1 authorisation server. When you add the connector in Claude, Claude registers itself with the server using RFC 7591 Dynamic Client Registration, then sends you through a PKCE-protected authorisation flow. You enter the password you set as `MCP_AUTH_PASSWORD`, and the server issues a 24-hour JWT access token plus a refresh token that rotates on every use.

This is intentionally minimal: one password, one user. If someone has the password they can read your Garmin data.

## Security caveats

* Garmin credentials live in environment variables on Cloud Run. They never leave the server, but anyone with access to the Cloud Run console can read them. Use a Garmin account that does not double as anything important. Disabling MFA on Garmin is required for unattended login.
* The OAuth password is stored as a plain env var and compared with `secrets.compare_digest`. Pick a long, random one (32+ bytes).
* The unofficial `garminconnect` library can break when Garmin changes their internal API. If a tool starts returning empty data, check that package's changelog.
* In-memory state (registered clients, refresh tokens) is wiped on every cold start. You will be re-prompted to authorise after the server has been idle for a while; this is expected.
* This server is read-only. It does not write activities, edit profile fields, or upload anything to Garmin.

## Project layout

```
garmin-mcp/
├── pyproject.toml
├── Dockerfile
├── .env.example
├── README.md
├── DEPLOY.md
├── src/
│   └── garmin_mcp/
│       ├── __init__.py
│       ├── __main__.py        # python -m garmin_mcp -> HTTP server
│       ├── server.py          # FastMCP app, tools, login UI
│       ├── garmin_client.py   # garminconnect wrapper
│       ├── auth.py            # OAuth 2.1 provider
│       ├── cache.py           # TTL cache
│       └── models.py          # Pydantic response models
└── tests/
    ├── test_cache.py
    └── test_tools.py
```

## Running tests and lints

```bash
uv run pytest          # unit tests
uv run ruff check .    # lint
uv run ruff format --check .
uv run mypy src tests  # type check
```

## Acknowledgements

* [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) by cyberjunky for doing the hard work of reverse-engineering the Garmin Connect API.
* The [Model Context Protocol](https://modelcontextprotocol.io/) team for the SDK.
