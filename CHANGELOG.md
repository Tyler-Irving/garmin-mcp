# Changelog

All notable changes to garmin-mcp are documented here. The project uses semantic versioning.

## [Unreleased]

### Added

- **`get_daily_briefing`** — a composite tool that fuses the recovery and training-load signals (sleep, HRV, Body Battery, training readiness, training load, resting HR) into one payload, fetched concurrently. Each section degrades to `null` (and is named in `sections_unavailable`) instead of failing the whole briefing, and the response adds `rhr_vs_baseline_bpm` — the most recent resting HR relative to its trailing average. Returns facts only; no training advice is computed server-side.

## [0.3.1] — 2026-05-30

### Added

- **Automated releases via PyPI Trusted Publishing (OIDC).** A GitHub Actions workflow (`.github/workflows/publish.yml`) runs the test/lint/type gate, builds the sdist + wheel, and publishes to PyPI on a published GitHub Release — with no stored API token. Includes a guard that the release tag matches the package version.

### Docs

- Fixed the CHANGELOG version-compare links left stale by the 0.3.0 stamp.

## [0.3.0] — 2026-05-30

### Added

- **Strength workout creation** — the server's first write capability. `preview_strength_workout` resolves free-text exercises to Garmin's catalog (47 categories / 1510 exercises in `data/exercise_taxonomy.json`) and returns a summary + per-exercise confidence + a content-bound confirmation token, making no network call; `create_strength_workout` uploads to your Garmin Connect library after the token matches. Supports supersets (multiple exercises per repeat group), per-step notes (rep range / RPE), and reps- or time-based sets.
- `GARMIN_WRITE_ENABLED` env flag (default off). Writes are also enforced at the client layer via a method allowlist — only `upload_workout` is writable, and only when enabled; everything else is read-only by enforcement, not convention.

### Fixed

- **Resolver mis-mapped some names at false-high confidence.** "Seated Leg Curl" resolved to an abdominal `CRUNCH` instead of a hamstring `LEG_CURL` (added a curated override). The junk-category discount (banded/cardio/plyo) was a dead 4th-place tie-breaker that never affected ranking or confidence — it now lowers the score directly.
- **Write-path hardening.** Over HTTP the server refuses to start when `GARMIN_WRITE_ENABLED` is set but auth is disabled; the `confirm=True` dev bypass is honored only on local stdio (never HTTP); the confirmation token is compared with `hmac.compare_digest` (constant time).
- **`weight_unit` is validated.** Any value other than exactly `kilogram` used to fall through to pounds; aliases (`kg`, `lb`, …) are normalised and unknown units rejected, so a 100 kg load can't silently upload as 100 lb.
- **Explicit overrides honored when partial.** Passing `exercise_name` alone now bypasses the resolver (category derived from the catalog) instead of being silently ignored; a bare `category` with no `exercise_name` raises.
- **Non-idempotent upload is no longer retried** on transient network errors (a blind retry of the POST could create a duplicate workout).
- **Post-upload verification can't fail a successful create** — a malformed `stepOrder` in the re-fetched JSON is parsed defensively, and verification errors are caught rather than masking the created workout.

### Notes

- Garmin's workout-service silently blanks bare category-name exercises (e.g. `BENCH_PRESS`), so the resolver always prefers a specific leaf (e.g. `BARBELL_BENCH_PRESS`), and `create_strength_workout` re-fetches the upload to verify no exercise came back blank.
- Created workouts land in the Connect **library**; reaching the watch needs a manual **Send to Device** + sync. Deleting/scheduling are intentionally not exposed.

## [0.2.3] — 2026-05-10

### Docs

- Added this `CHANGELOG.md`.
- Added a "Data availability" section to the README explaining when Garmin returns null fields or status sentinels like `NO_STATUS_2` / `NONE`.

## [0.2.2] — 2026-05-10

### Fixed

- **`get_resting_heart_rate`** silently returned `days_with_data=0` even when Garmin had a valid reading every day. The RHR value is nested at `allMetrics.metricsMap.WELLNESS_RESTING_HEART_RATE[*].value`, two levels deeper than the parser was looking.
- **`get_weekly_summary(metric="steps")`** returned an empty average. `weekly_steps` is the one variant where Garmin wraps the per-week metrics under a nested `values` dict; the other two metrics (`stress`, `intensity_minutes`) keep them at the top level. Parser now handles both shapes.
- **`get_respiration`** reported `avg_breaths_per_min=null` because Garmin doesn't populate `avgRespirationValue` consistently. Parser now synthesises the daily average from `avgSleepRespirationValue` and `avgWakingRespirationValue` when both are present.

### Tests

- Regression tests using the real Garmin response shapes (rather than the inferred-from-docstring shapes we had been using).

## [0.2.1] — 2026-05-10

### Fixed

- **`get_personal_records`** previously returned `type_<N>` labels because Garmin doesn't populate `prTypeLabelKey` for many records. Added an explicit `typeId` → (label, unit) map for the IDs observed in the wild (1–10, 12–16), covering running time PRs, distance PRs, daily/weekly/monthly step bests, and goal streaks.
- `PersonalRecord` now carries an explicit `unit` field (`seconds`, `meters`, `count`, `days`) so downstream LLMs can interpret `raw_value` without guessing.

## [0.2.0] — 2026-05-10

### Added

Six new tools, taking the total from 9 to 15:

- `get_training_readiness` — daily 0–100 readiness score with contributing factors (sleep, HRV, recovery, stress).
- `get_fitness_metrics` — VO2 max (running + cycling), fitness age, and race-time predictions for 5K / 10K / half / marathon. Walks back up to 7 days to surface the most recent VO2 max reading rather than returning null on rest days.
- `get_personal_records` — PRs across activity types.
- `get_body_composition` — weight / body fat / muscle mass trend over a configurable window. Converts Garmin's gram-based weights to kilograms.
- `get_respiration` — daily breaths-per-minute with sleep vs waking averages.
- `get_weekly_summary(metric, weeks)` — unified aggregator for steps, stress, or intensity minutes.

## [0.1.1] — 2026-05-10

### Docs

- Simplified the default Claude Desktop config snippet (saved tokens alone are enough; credentials moved to an optional "unattended re-auth" section).
- Added "Where session tokens are stored" table per OS.
- Added PyPI / Python / license badges.
- Added an actual MIT `LICENSE` file to match the pyproject metadata.

## [0.1.0] — 2026-05-10

### Added

- Initial PyPI release.
- Stdio CLI with subcommands: `garmin-mcp` (default stdio), `garmin-mcp serve` (streamable-HTTP), `garmin-mcp login` (interactive Garmin login with MFA support).
- Nine MCP tools: `get_sleep`, `get_recent_activities`, `get_activity_details`, `get_training_load`, `get_hrv_status`, `get_body_battery`, `get_steps_and_calories`, `get_resting_heart_rate`, `get_stress`.
- OAuth 2.1 with PKCE and Dynamic Client Registration for the HTTP transport.
- TTL cache with per-tool TTLs.
- `garminconnect` wrapper with persistent session tokens, transparent re-auth, exponential-backoff retry, and 1-req/2s rate limiting.
- Saved tokens default to the user's platform cache directory (`~/.cache/garmin-mcp/garth/` on Linux, equivalent on macOS / Windows). `GARMIN_TOKEN_DIR` env var overrides for Cloud Run.

[Unreleased]: https://github.com/Tyler-Irving/garmin-mcp/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/Tyler-Irving/garmin-mcp/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Tyler-Irving/garmin-mcp/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/Tyler-Irving/garmin-mcp/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Tyler-Irving/garmin-mcp/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Tyler-Irving/garmin-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Tyler-Irving/garmin-mcp/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Tyler-Irving/garmin-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Tyler-Irving/garmin-mcp/releases/tag/v0.1.0
