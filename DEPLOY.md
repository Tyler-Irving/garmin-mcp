# Deploying garmin-mcp to Cloud Run

End-to-end walkthrough from a fresh checkout to a working custom connector in Claude.ai.

## Prerequisites

* A Google Cloud project with billing enabled. For a single-user server you will likely stay inside the always-free tier (more on cost at the bottom of this page).
* The `gcloud` CLI installed and authenticated: `gcloud auth login` and `gcloud config set project YOUR_PROJECT_ID`.
* Docker installed locally (only required if you want to test the container before deploying; `gcloud run deploy --source` builds remotely otherwise).
* A Garmin Connect account with MFA disabled. The server logs in unattended and cannot prompt you for an MFA code on cold start.

## 1. Generate the secrets

You will need two random secrets: a server password (`MCP_AUTH_PASSWORD`) and a JWT signing key (`JWT_SECRET`). Generate them in your shell so they never get committed:

```bash
export MCP_AUTH_PASSWORD="$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)"
export JWT_SECRET="$(openssl rand -base64 48)"
```

Write the password down somewhere safe. You will type it into the login page once during the Claude connector setup.

## 2. Test the container locally (optional but recommended)

```bash
docker build -t garmin-mcp:local .

docker run --rm -p 8080:8080 \
  -e MCP_ISSUER_URL=http://localhost:8080 \
  -e MCP_AUTH_PASSWORD="$MCP_AUTH_PASSWORD" \
  -e JWT_SECRET="$JWT_SECRET" \
  -e GARMIN_EMAIL="you@example.com" \
  -e GARMIN_PASSWORD="your-garmin-password" \
  garmin-mcp:local
```

Hit `http://localhost:8080/health` and you should see `{"status":"ok","auth_enabled":true}`. The MCP endpoint is `http://localhost:8080/mcp`.

## 3. Enable Cloud Run

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
```

## 4. First deploy

`gcloud run deploy --source .` builds the container in Cloud Build and deploys it to a managed Cloud Run service in one step. We use a temporary placeholder for the issuer URL on this first deploy because we do not know the public URL yet.

```bash
gcloud run deploy garmin-mcp \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 1 \
  --memory 512Mi \
  --cpu 1 \
  --timeout 300 \
  --set-env-vars="MCP_ISSUER_URL=https://placeholder.example,MCP_AUTH_PASSWORD=$MCP_AUTH_PASSWORD,JWT_SECRET=$JWT_SECRET,GARMIN_EMAIL=you@example.com,GARMIN_PASSWORD=your-garmin-password"
```

A few notes:

* `--allow-unauthenticated` makes the service publicly reachable so Claude.ai can hit it. Auth is then handled by our own OAuth layer; Cloud Run IAM is not in the path.
* `--min-instances 0` lets it scale to zero when idle, which is the whole point if you only use Claude a few times a day.
* `--max-instances 1` keeps things simple. Single-user, single instance, no race conditions on the in-memory caches.
* Pick a region close to you. `us-central1` is the cheapest US region; `europe-west1` is a reasonable EU pick.

Once the deploy finishes, gcloud prints a URL like:

```
Service URL: https://garmin-mcp-abc123-uc.a.run.app
```

Copy that URL.

## 5. Set the real issuer URL

OAuth metadata, redirect URLs, and the JWT issuer claim all need to match the public URL of the service. Update the env var to point at the URL you just got:

```bash
gcloud run services update garmin-mcp \
  --region us-central1 \
  --update-env-vars="MCP_ISSUER_URL=https://garmin-mcp-abc123-uc.a.run.app"
```

After the update propagates (usually 30 seconds), `curl https://garmin-mcp-abc123-uc.a.run.app/health` should return `{"status":"ok","auth_enabled":true}`.

## 6. Add it as a custom connector in Claude.ai

1. Open Claude.ai and go to **Settings -> Connectors**.
2. Click **Add custom connector**.
3. Paste the MCP endpoint URL: `https://garmin-mcp-abc123-uc.a.run.app/mcp`.
4. Claude shows a sign-in window pointing at your server's `/login` page. Enter the `MCP_AUTH_PASSWORD` you set.
5. Claude completes the OAuth handshake and the connector goes green.

(Screenshots: `docs/screenshots/01-add-connector.png`, `docs/screenshots/02-login-page.png`, `docs/screenshots/03-connector-active.png`. Add yours after the first run.)

You can now ask Claude things like "what was my sleep score last night?" or "list my last five activities and tell me which one had the highest training effect." Claude calls the tools, the server hits Garmin, and the JSON comes back.

## 7. Updating

To deploy a new version after editing the code:

```bash
gcloud run deploy garmin-mcp --source . --region us-central1
```

Env vars and other settings are preserved. Cloud Run does a zero-downtime rollout.

## 8. Watching logs

```bash
gcloud run services logs tail garmin-mcp --region us-central1
```

Logs are JSON because we use `structlog`. Useful event keys: `oauth.client.registered`, `oauth.login.success`, `garmin.login.resumed`, `garmin.auth.expired`.

## Cost expectations

Cloud Run charges for active CPU, memory, and request count.

* Idle time costs nothing because `min-instances=0`.
* Each Claude conversation that calls a tool spins the instance up for a few seconds.
* The Cloud Run free tier covers 240,000 vCPU-seconds and 450,000 GiB-seconds per month, which is dramatically more than a single user generates.

For a personal usage pattern (a handful of conversations a day, a few tool calls each), the monthly bill is typically under $1, often $0.

## Troubleshooting

**Login fails with "incorrect password" but I am sure it is right.**
Check `MCP_AUTH_PASSWORD` on the running service: `gcloud run services describe garmin-mcp --region us-central1 --format='value(spec.template.spec.containers[0].env)'`. Special shell characters in the password may have been mangled when you set it. Quote them or generate a password without symbols.

**Tools return empty data.**
The Garmin session may have expired in a way the wrapper did not detect. Force a redeploy to clear the cached tokens, or hit the service shell and delete `/tmp/garth/`.

**`garminconnect` errors after a Garmin app update.**
Check the package's release notes and bump the version pin in `pyproject.toml`. The library is unofficial; it sometimes lags Garmin changes by a few days.

**Claude shows "MCP server unreachable".**
Confirm the public URL is correct and that `MCP_ISSUER_URL` matches it exactly (including `https://`, no trailing slash). The OAuth metadata served at `/.well-known/oauth-authorization-server` reflects this value, and Claude refuses to connect if it does not match.

**I want to revoke access right now.**
Rotate `JWT_SECRET`. Existing access tokens become invalid the moment the service picks up the new secret.
