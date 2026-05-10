# garmin-mcp

An [MCP](https://modelcontextprotocol.io/) server that exposes your Garmin Connect data to Claude as tools. Ask things like "how did I sleep last night?" or "summarise my training load this week" and Claude can answer using your real Garmin data instead of you copy-pasting screenshots from the app.

Single-user, read-only. Two ways to run it:

| Mode                 | Where it runs           | Works with                       | Setup            |
| -------------------- | ----------------------- | -------------------------------- | ---------------- |
| **Local (stdio)**    | Your own machine        | Claude Desktop                   | One command      |
| **Self-hosted HTTP** | Cloud Run (or anywhere) | Claude.ai web, mobile, Desktop   | ~10 min, ~$0/mo  |

## Tools

| Tool                       | What it returns                                                              |
| -------------------------- | ---------------------------------------------------------------------------- |
| `get_sleep`                | Sleep duration, stages (deep / light / REM / awake), score, overnight HRV.   |
| `get_recent_activities`    | List of recent activities with type, duration, distance, average heart rate. |
| `get_activity_details`     | Full metrics for one activity, including splits, HR zones, and power.        |
| `get_training_load`        | Daily training load with acute (ATL), chronic (CTL), and current status.     |
| `get_hrv_status`           | Current HRV status, baseline range, and the last 7 nights of readings.      |
| `get_body_battery`         | Body battery values across the day with min, max, charged, drained.          |
| `get_steps_and_calories`   | Daily step count, distance, calories, floors, and intensity minutes.         |
| `get_resting_heart_rate`   | Resting heart rate trend and average over the requested window.              |
| `get_stress`               | Stress levels across the day and time-in-zone breakdown.                     |

Every response is a Pydantic model serialised to JSON, with `null` for fields Garmin did not record.

## Quick start — Claude Desktop (recommended)

Requires Python 3.12+, [`uv`](https://docs.astral.sh/uv/) (install with `curl -LsSf https://astral.sh/uv/install.sh | sh`), and a Garmin Connect account.

### 1. Seed your Garmin session

Run the interactive login once. It handles MFA if your account has it enabled and saves session tokens to a per-user cache directory.

```bash
uvx garmin-mcp login
```

You'll be prompted for your Garmin email, password, and (if applicable) an MFA code.

### 2. Add it to Claude Desktop

Edit your Claude Desktop config:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uvx",
      "args": ["garmin-mcp"],
      "env": {
        "GARMIN_EMAIL": "you@example.com",
        "GARMIN_PASSWORD": "your-garmin-password"
      }
    }
  }
}
```

The credentials in `env` are used to silently refresh the session when the saved tokens expire. Without them, you'd need to re-run `garmin-mcp login` periodically.

Restart Claude Desktop. The Garmin tools appear in the tool picker. Ask Claude something like *"what was my resting heart rate this week?"* to test.

## Self-hosted HTTP (Claude.ai web/mobile)

If you want to use the connector from Claude.ai on the web or your phone, run the same server in HTTP mode. The `serve` subcommand wraps it in an OAuth 2.1 layer with PKCE and Dynamic Client Registration so Claude.ai can connect to it as a custom connector.

See [DEPLOY.md](DEPLOY.md) for the full Cloud Run walkthrough. The short version:

```bash
# Build and run locally first to verify
docker build -t garmin-mcp .
docker run --rm -p 8080:8080 \
  -e MCP_ISSUER_URL=http://localhost:8080 \
  -e MCP_AUTH_PASSWORD=$(openssl rand -base64 24) \
  -e JWT_SECRET=$(openssl rand -base64 48) \
  -e GARMIN_EMAIL=you@example.com \
  -e GARMIN_PASSWORD=your-garmin-password \
  garmin-mcp
```

For Cloud Run, the always-free tier covers personal usage. Expect under \$1/month.

## How auth works (HTTP mode)

The server is its own OAuth 2.1 authorisation server. When you add the connector in Claude.ai, Claude registers itself using RFC 7591 Dynamic Client Registration, then sends you through a PKCE-protected flow. You enter the password set as `MCP_AUTH_PASSWORD`, and the server issues a 24-hour JWT access token plus a refresh token that rotates on every use.

This is intentionally minimal: one password, one user. Anyone with the password can read your Garmin data.

## Security caveats

* Your Garmin credentials sit in environment variables (Claude Desktop config or Cloud Run env). They never leave your machine / server, but anyone with read access there can see them. Pick a Garmin account that does not double as anything important.
* The HTTP-mode password is compared with `secrets.compare_digest`. Pick a long, random one (32+ bytes).
* The unofficial `garminconnect` library can break when Garmin changes their internal API. If a tool starts returning empty data, check that package's changelog.
* In HTTP mode, registered DCR clients and refresh tokens live in process memory and disappear on restart. Access tokens (JWTs) survive because they are stateless.
* This server is read-only. It does not write activities, edit profile fields, or upload anything to Garmin.

## Project layout

```
garmin-mcp/
├── pyproject.toml
├── Dockerfile
├── README.md
├── DEPLOY.md
└── src/
    └── garmin_mcp/
        ├── __init__.py
        ├── __main__.py        # python -m garmin_mcp -> CLI
        ├── cli.py             # argparse entry: stdio / serve / login
        ├── server.py          # FastMCP app, tools, login UI
        ├── garmin_client.py   # garminconnect wrapper
        ├── auth.py            # OAuth 2.1 provider
        ├── cache.py           # TTL cache
        ├── paths.py           # token directory resolution
        └── models.py          # Pydantic response models
```

## Running from source

```bash
git clone https://github.com/Tyler-Irving/garmin-mcp.git
cd garmin-mcp
uv sync

# Interactive Garmin login (handles MFA)
uv run garmin-mcp login

# Inspect tools in the MCP dev inspector
uv run mcp dev src/garmin_mcp/server.py

# Stdio mode (for Claude Desktop)
uv run garmin-mcp

# HTTP mode (for remote / Cloud Run)
uv run garmin-mcp serve
```

## Tests and lint

```bash
uv run pytest          # unit tests
uv run ruff check .    # lint
uv run ruff format --check .
uv run mypy src tests  # type check
```

## Acknowledgements

* [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) by cyberjunky for doing the hard work of reverse-engineering the Garmin Connect API.
* The [Model Context Protocol](https://modelcontextprotocol.io/) team for the SDK.
