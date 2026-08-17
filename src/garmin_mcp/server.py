"""FastMCP server exposing Garmin Connect data as tools.

Two run modes:

* stdio (for ``mcp dev src/garmin_mcp/server.py``): no auth. Useful while
  developing tools and inspecting their schema in the MCP inspector.
* streamable-http (production): wraps the server in an OAuth 2.1 layer with
  PKCE and Dynamic Client Registration so Claude.ai can connect to it as a
  custom connector.

Set the relevant environment variables in a ``.env`` file or via the host
process. See ``.env.example`` for the full list.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import (
    date as Date,  # noqa: N812 (capitalised to avoid shadowing the `date` parameter on tools)
)
from datetime import datetime, timedelta
from typing import Any

import structlog
from dotenv import load_dotenv

# Critical: in stdio transport, stdout carries the MCP JSON-RPC stream. Any
# log line written to stdout corrupts the protocol and the client disconnects.
# Force every log byte to stderr regardless of transport.
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from garmin_mcp.auth import InvalidLoginError, SimpleOAuthProvider
from garmin_mcp.cache import TTLCache
from garmin_mcp.exercise_resolver import (
    category_for_exercise,
    resolve_exercise,
    valid_exercise_names,
)
from garmin_mcp.garmin_client import GarminClient, GarminClientError
from garmin_mcp.models import (
    Activity,
    ActivityDetail,
    ActivityList,
    ActivitySplit,
    BodyBatteryReading,
    BodyBatterySummary,
    BodyCompositionDay,
    BodyCompositionTrend,
    DailyBriefing,
    FitnessMetrics,
    HRVDayReading,
    HRVStatus,
    HRZone,
    PersonalRecord,
    PersonalRecords,
    RacePrediction,
    ResolvedExerciseInfo,
    RespirationSummary,
    RestingHeartRateTrend,
    RHRDay,
    SleepSummary,
    StepsAndCalories,
    StrengthWorkoutCreated,
    StrengthWorkoutInput,
    StrengthWorkoutPreview,
    StressBucket,
    StressSummary,
    TrainingLoadDay,
    TrainingLoadSummary,
    TrainingReadiness,
    TrainingReadinessFactor,
    WeeklyBucket,
    WeeklySummary,
    WorkoutDeleted,
    WorkoutList,
    WorkoutSummary,
    _opt_float,
    _opt_int,
    _opt_str,
)
from garmin_mcp.paths import default_token_dir
from garmin_mcp.strength_builder import (
    BlockSpec,
    SetSpec,
    StrengthWorkoutSpec,
    build_strength_workout,
    summarize,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.WriteLoggerFactory(file=sys.stderr),
    cache_logger_on_first_use=True,
)
# Redirect the stdlib root logger (used by uvicorn / starlette) to stderr too.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)
log = structlog.get_logger("garmin_mcp")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ISSUER_URL = os.getenv("MCP_ISSUER_URL", "http://localhost:8080").rstrip("/")
MCP_PASSWORD = os.getenv("MCP_AUTH_PASSWORD", "")
JWT_SECRET = os.getenv("JWT_SECRET", "")
GARMIN_EMAIL = os.getenv("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD", "")
GARMIN_TOKEN_DIR = default_token_dir()
# Writes (creating workouts) are OFF unless explicitly enabled.
WRITE_ENABLED = os.getenv("GARMIN_WRITE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

AUTH_ENABLED = bool(MCP_PASSWORD and JWT_SECRET)

# Active transport, set by run_stdio()/run_http(). The confirm=True dev bypass on
# create_strength_workout (used when no JWT secret is configured) is honored only
# on local stdio — never over HTTP, where it could let a caller skip the
# content-bound preview token.
_TRANSPORT: str = "stdio"

# ---------------------------------------------------------------------------
# FastMCP setup
# ---------------------------------------------------------------------------

_oauth_provider: SimpleOAuthProvider | None = None

if AUTH_ENABLED:
    _oauth_provider = SimpleOAuthProvider(
        mcp_password=MCP_PASSWORD,
        jwt_secret=JWT_SECRET,
        issuer_url=ISSUER_URL,
    )
    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(ISSUER_URL),
        resource_server_url=AnyHttpUrl(ISSUER_URL),
        revocation_options=RevocationOptions(enabled=True),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
        required_scopes=["mcp"],
    )
    mcp = FastMCP(
        "garmin-mcp",
        auth_server_provider=_oauth_provider,
        auth=auth_settings,
        host=HOST,
        port=PORT,
        stateless_http=True,
        json_response=False,
    )
else:
    mcp = FastMCP(
        "garmin-mcp",
        host=HOST,
        port=PORT,
        stateless_http=True,
        json_response=False,
    )
    log.warning(
        "auth.disabled",
        reason="MCP_AUTH_PASSWORD or JWT_SECRET not set; HTTP transport will reject unsafely. Use only with stdio.",
    )

# ---------------------------------------------------------------------------
# Shared resources
# ---------------------------------------------------------------------------

_cache = TTLCache()
_garmin: GarminClient | None = None


def _get_garmin() -> GarminClient:
    global _garmin
    if _garmin is None:
        _garmin = GarminClient(
            email=GARMIN_EMAIL,
            password=GARMIN_PASSWORD,
            token_dir=GARMIN_TOKEN_DIR,
            allow_writes=WRITE_ENABLED,
        )
    return _garmin


def set_garmin_client_for_testing(client: GarminClient | None) -> None:
    """Test hook: substitute a fake Garmin client and reset the cache."""
    global _garmin
    _garmin = client


def get_cache_for_testing() -> TTLCache:
    return _cache


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return Date.today().isoformat()


def _yesterday_iso() -> str:
    return (Date.today() - timedelta(days=1)).isoformat()


def _normalise_date(value: str | None, default: str) -> str:
    if value is None or value == "":
        return default
    try:
        return Date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD format.") from exc


def _seconds_to_iso_local(epoch_ms: int | None) -> str | None:
    if epoch_ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_ms) / 1000.0).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _rhr_deviation(trend: RestingHeartRateTrend) -> float | None:
    """Most recent RHR minus the average of the prior days in the window.

    The trailing average excludes the latest reading so a single elevated day
    stands out against its own baseline rather than being diluted by it.
    Returns None when fewer than two days of data are present.
    """
    values = [d.rhr_bpm for d in trend.days if d.rhr_bpm is not None]
    if len(values) < 2:
        return None
    latest = values[-1]
    prior = values[:-1]
    baseline = sum(prior) / len(prior)
    return round(latest - baseline, 1)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_sleep(date: str | None = None) -> SleepSummary:
    """Sleep duration, sleep stages, sleep score, and overnight HRV.

    Args:
        date: Calendar date in YYYY-MM-DD format. Defaults to last night
            (yesterday). Pass today's date to get the most recent recorded
            sleep when you wake up.
    """
    target_date = _normalise_date(date, _yesterday_iso())
    cache_key = TTLCache.make_key("get_sleep", {"date": target_date})

    async def fetch() -> SleepSummary:
        client = _get_garmin()
        raw = await client.call("get_sleep_data", target_date)
        return _parse_sleep(raw, target_date)

    result: SleepSummary = await _cache.get_or_compute(cache_key, 3600, fetch)
    return result


@mcp.tool()
async def get_recent_activities(limit: int = 10) -> ActivityList:
    """List recent activities with type, duration, distance, and average heart rate.

    Args:
        limit: How many of the most recent activities to return. Capped at 50.
    """
    limit = max(1, min(50, limit))
    cache_key = TTLCache.make_key("get_recent_activities", {"limit": limit})

    async def fetch() -> ActivityList:
        client = _get_garmin()
        raw = await client.call("get_activities", 0, limit)
        return _parse_activity_list(raw)

    result: ActivityList = await _cache.get_or_compute(cache_key, 300, fetch)
    return result


@mcp.tool()
async def get_activity_details(activity_id: str) -> ActivityDetail:
    """Detailed metrics for one activity, including splits and HR zones.

    Args:
        activity_id: The activity's numeric Garmin ID, as returned by
            ``get_recent_activities``.
    """
    if not activity_id or not str(activity_id).strip():
        raise ValueError("activity_id is required.")

    cache_key = TTLCache.make_key("get_activity_details", {"id": activity_id})

    async def fetch() -> ActivityDetail:
        client = _get_garmin()
        summary = await client.call("get_activity", activity_id)
        try:
            splits = await client.call("get_activity_splits", activity_id)
        except GarminClientError:
            splits = None
        try:
            zones = await client.call("get_activity_hr_in_timezones", activity_id)
        except GarminClientError:
            zones = None
        return _parse_activity_detail(activity_id, summary, splits, zones)

    result: ActivityDetail = await _cache.get_or_compute(cache_key, 86400, fetch)
    return result


@mcp.tool()
async def get_training_load(days: int = 7) -> TrainingLoadSummary:
    """Daily training load with acute and chronic load and current status.

    Args:
        days: Number of recent days to summarise. Capped at 28.
    """
    days = max(1, min(28, days))
    end = Date.today()
    dates = [(end - timedelta(days=i)).isoformat() for i in range(days)]
    dates.reverse()
    cache_key = TTLCache.make_key("get_training_load", {"days": days, "end": end.isoformat()})

    async def fetch() -> TrainingLoadSummary:
        client = _get_garmin()
        per_day: list[TrainingLoadDay] = []
        latest_status: dict[str, Any] | None = None
        for d in dates:
            try:
                status = await client.call("get_training_status", d)
            except GarminClientError as exc:
                log.warning("training_load.day.failed", date=d, error=str(exc)[:200])
                status = None
            per_day.append(_parse_training_load_day(d, status))
            if status:
                latest_status = status
        return _build_training_load_summary(per_day, latest_status)

    result: TrainingLoadSummary = await _cache.get_or_compute(cache_key, 3600, fetch)
    return result


@mcp.tool()
async def get_hrv_status() -> HRVStatus:
    """Current HRV status, baseline range, and the last 7 nights of readings."""
    end = Date.today()
    cache_key = TTLCache.make_key("get_hrv_status", {"end": end.isoformat()})

    async def fetch() -> HRVStatus:
        client = _get_garmin()
        # Newest first; the first day with data drives the headline status.
        nights = [(end - timedelta(days=i)).isoformat() for i in range(7)]
        readings: list[HRVDayReading] = []
        headline: dict[str, Any] | None = None
        for d in nights:
            try:
                raw = await client.call("get_hrv_data", d)
            except GarminClientError as exc:
                log.warning("hrv.day.failed", date=d, error=str(exc)[:200])
                raw = None
            readings.append(_parse_hrv_day(d, raw))
            if headline is None and raw is not None:
                headline = raw
        readings.reverse()  # oldest first for the trend
        return _build_hrv_status(headline, readings)

    result: HRVStatus = await _cache.get_or_compute(cache_key, 3600, fetch)
    return result


@mcp.tool()
async def get_body_battery(date: str | None = None) -> BodyBatterySummary:
    """Body battery values across the day, plus min, max, charged, and drained totals.

    Args:
        date: Calendar date in YYYY-MM-DD format. Defaults to today.
    """
    target_date = _normalise_date(date, _today_iso())
    cache_key = TTLCache.make_key("get_body_battery", {"date": target_date})

    async def fetch() -> BodyBatterySummary:
        client = _get_garmin()
        raw = await client.call("get_body_battery", target_date, target_date)
        return _parse_body_battery(raw, target_date)

    result: BodyBatterySummary = await _cache.get_or_compute(cache_key, 3600, fetch)
    return result


@mcp.tool()
async def get_steps_and_calories(date: str | None = None) -> StepsAndCalories:
    """Daily step total, distance, calories, floors, and intensity minutes.

    Args:
        date: Calendar date in YYYY-MM-DD format. Defaults to today.
    """
    target_date = _normalise_date(date, _today_iso())
    cache_key = TTLCache.make_key("get_steps_and_calories", {"date": target_date})

    async def fetch() -> StepsAndCalories:
        client = _get_garmin()
        raw = await client.call("get_user_summary", target_date)
        return _parse_steps_and_calories(raw, target_date)

    result: StepsAndCalories = await _cache.get_or_compute(cache_key, 1800, fetch)
    return result


@mcp.tool()
async def get_resting_heart_rate(days: int = 7) -> RestingHeartRateTrend:
    """Resting heart rate trend over the last ``days`` days.

    Args:
        days: How many recent days to include. Capped at 28.
    """
    days = max(1, min(28, days))
    end = Date.today()
    cache_key = TTLCache.make_key("get_resting_heart_rate", {"days": days, "end": end.isoformat()})

    async def fetch() -> RestingHeartRateTrend:
        client = _get_garmin()
        rows: list[RHRDay] = []
        dates = [(end - timedelta(days=i)).isoformat() for i in range(days)]
        dates.reverse()
        for d in dates:
            try:
                raw = await client.call("get_rhr_day", d)
            except GarminClientError as exc:
                log.warning("rhr.day.failed", date=d, error=str(exc)[:200])
                raw = None
            rows.append(_parse_rhr_day(d, raw))
        valid = [r.rhr_bpm for r in rows if r.rhr_bpm is not None]
        avg = sum(valid) / len(valid) if valid else None
        return RestingHeartRateTrend(days=rows, avg_rhr_bpm=avg)

    result: RestingHeartRateTrend = await _cache.get_or_compute(cache_key, 3600, fetch)
    return result


@mcp.tool()
async def get_stress(date: str | None = None) -> StressSummary:
    """Stress levels across the day with average, max, and time-in-zone breakdown.

    Args:
        date: Calendar date in YYYY-MM-DD format. Defaults to today.
    """
    target_date = _normalise_date(date, _today_iso())
    cache_key = TTLCache.make_key("get_stress", {"date": target_date})

    async def fetch() -> StressSummary:
        client = _get_garmin()
        raw = await client.call("get_stress_data", target_date)
        return _parse_stress(raw, target_date)

    result: StressSummary = await _cache.get_or_compute(cache_key, 3600, fetch)
    return result


@mcp.tool()
async def get_training_readiness(date: str | None = None) -> TrainingReadiness:
    """Daily training readiness score (0-100) with contributing factors.

    The score reflects how prepared the user is to train, drawing on sleep,
    HRV status, recovery time, and recent training load.

    Args:
        date: Calendar date in YYYY-MM-DD format. Defaults to today.
    """
    target_date = _normalise_date(date, _today_iso())
    cache_key = TTLCache.make_key("get_training_readiness", {"date": target_date})

    async def fetch() -> TrainingReadiness:
        client = _get_garmin()
        raw = await client.call("get_training_readiness", target_date)
        return _parse_training_readiness(raw, target_date)

    result: TrainingReadiness = await _cache.get_or_compute(cache_key, 3600, fetch)
    return result


@mcp.tool()
async def get_daily_briefing(date: str | None = None) -> DailyBriefing:
    """One-call morning snapshot: sleep, HRV, Body Battery, readiness, load, and RHR.

    Fuses the individual recovery and training-load tools into a single payload so
    you can reason across them in one shot instead of making six separate calls.
    Each section is fetched independently and concurrently; a section that fails
    comes back ``null`` and is named in ``sections_unavailable`` rather than
    failing the whole briefing. Also returns ``rhr_vs_baseline_bpm``, the most
    recent resting HR relative to its trailing average.

    The server returns facts only and computes no training advice — interpret the
    numbers yourself (e.g. weigh readiness, HRV status, and Body Battery together).

    Args:
        date: Calendar date in YYYY-MM-DD format. Defaults to today. The sleep,
            Body Battery, and training-readiness sections honour this date; the
            HRV, training-load, and resting-HR sections always report their own
            most-recent trailing window. For the default (this-morning) call
            everything lines up; passing a past date yields a mixed snapshot.
    """
    target_date = _normalise_date(date, _today_iso())

    # Each child tool is already individually cached, so the briefing is cheap on
    # repeat calls. return_exceptions=True keeps one failing section from sinking
    # the rest — we record which sections dropped out instead.
    section_names = (
        "sleep",
        "hrv",
        "body_battery",
        "training_readiness",
        "training_load",
        "resting_heart_rate",
    )
    coros = (
        get_sleep(date),
        get_hrv_status(),
        get_body_battery(date),
        get_training_readiness(date),
        get_training_load(),
        get_resting_heart_rate(),
    )
    results = await asyncio.gather(*coros, return_exceptions=True)

    fields: dict[str, Any] = {"date": target_date}
    unavailable: list[str] = []
    for name, result in zip(section_names, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("daily_briefing.section.failed", section=name, error=str(result)[:200])
            fields[name] = None
            unavailable.append(name)
        else:
            fields[name] = result

    rhr = fields.get("resting_heart_rate")
    fields["rhr_vs_baseline_bpm"] = _rhr_deviation(rhr) if rhr is not None else None
    fields["sections_unavailable"] = unavailable
    return DailyBriefing(**fields)


@mcp.tool()
async def get_fitness_metrics(date: str | None = None) -> FitnessMetrics:
    """VO2 max (running and cycling), fitness age, and predicted race times.

    Combines Garmin's "max metrics" tile (VO2 max, fitness age) with current
    race-time predictions for 5K, 10K, half marathon, and marathon.

    Args:
        date: Calendar date for the VO2 max snapshot in YYYY-MM-DD format.
            Defaults to today.
    """
    target_date = _normalise_date(date, _today_iso())
    cache_key = TTLCache.make_key("get_fitness_metrics", {"date": target_date})

    async def fetch() -> FitnessMetrics:
        client = _get_garmin()
        max_metrics: Any = None
        max_date = target_date
        # VO2 max only updates after qualifying activities, so today is often
        # empty. Walk back up to a week to find the most recent reading.
        for offset in (0, 1, 3, 7):
            probe_date = (Date.fromisoformat(target_date) - timedelta(days=offset)).isoformat()
            try:
                payload = await client.call("get_max_metrics", probe_date)
            except GarminClientError as exc:
                log.warning(
                    "fitness.max_metrics.failed",
                    date=probe_date,
                    error=str(exc)[:200],
                )
                continue
            if payload:
                max_metrics = payload
                max_date = probe_date
                break
        try:
            race = await client.call("get_race_predictions")
        except GarminClientError as exc:
            log.warning("fitness.race_predictions.failed", error=str(exc)[:200])
            race = None
        return _parse_fitness_metrics(max_date, max_metrics, race)

    result: FitnessMetrics = await _cache.get_or_compute(cache_key, 86400, fetch)
    return result


@mcp.tool()
async def get_personal_records() -> PersonalRecords:
    """Personal records across activity types (fastest 1K/5K/10K, longest run, etc.)."""
    cache_key = TTLCache.make_key("get_personal_records", None)

    async def fetch() -> PersonalRecords:
        client = _get_garmin()
        raw = await client.call("get_personal_record")
        return _parse_personal_records(raw)

    result: PersonalRecords = await _cache.get_or_compute(cache_key, 86400, fetch)
    return result


@mcp.tool()
async def get_body_composition(
    end_date: str | None = None,
    days: int = 30,
) -> BodyCompositionTrend:
    """Weight, body fat, and muscle-mass trend over recent days.

    Returns one row per day Garmin has a reading for, plus the latest weight
    and average over the window.

    Args:
        end_date: End of the window in YYYY-MM-DD format. Defaults to today.
        days: Window size in days. Capped at 90.
    """
    days = max(1, min(90, days))
    end = _normalise_date(end_date, _today_iso())
    start = (Date.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    cache_key = TTLCache.make_key("get_body_composition", {"start": start, "end": end})

    async def fetch() -> BodyCompositionTrend:
        client = _get_garmin()
        raw = await client.call("get_body_composition", start, end)
        return _parse_body_composition(raw)

    result: BodyCompositionTrend = await _cache.get_or_compute(cache_key, 3600, fetch)
    return result


@mcp.tool()
async def get_respiration(date: str | None = None) -> RespirationSummary:
    """Daily respiration rate: average, min, max, and waking vs. sleeping averages.

    Args:
        date: Calendar date in YYYY-MM-DD format. Defaults to today.
    """
    target_date = _normalise_date(date, _today_iso())
    cache_key = TTLCache.make_key("get_respiration", {"date": target_date})

    async def fetch() -> RespirationSummary:
        client = _get_garmin()
        raw = await client.call("get_respiration_data", target_date)
        return _parse_respiration(raw, target_date)

    result: RespirationSummary = await _cache.get_or_compute(cache_key, 3600, fetch)
    return result


@mcp.tool()
async def get_weekly_summary(
    metric: str,
    weeks: int = 4,
    end_date: str | None = None,
) -> WeeklySummary:
    """Weekly aggregates for a single metric.

    Args:
        metric: One of "steps", "stress", or "intensity_minutes".
        weeks: How many recent weeks to include. Capped at 12.
        end_date: End of the window in YYYY-MM-DD format. Defaults to today.
    """
    metric_norm = metric.strip().lower()
    if metric_norm not in {"steps", "stress", "intensity_minutes"}:
        raise ValueError("metric must be one of: 'steps', 'stress', 'intensity_minutes'")
    weeks = max(1, min(12, weeks))
    end = _normalise_date(end_date, _today_iso())
    cache_key = TTLCache.make_key(
        "get_weekly_summary", {"metric": metric_norm, "weeks": weeks, "end": end}
    )

    async def fetch() -> WeeklySummary:
        client = _get_garmin()
        if metric_norm == "intensity_minutes":
            start = (Date.fromisoformat(end) - timedelta(weeks=weeks)).isoformat()
            raw = await client.call("get_weekly_intensity_minutes", start, end)
        elif metric_norm == "steps":
            raw = await client.call("get_weekly_steps", end, weeks)
        else:  # stress
            raw = await client.call("get_weekly_stress", end, weeks)
        return _parse_weekly_summary(metric_norm, raw)

    result: WeeklySummary = await _cache.get_or_compute(cache_key, 3600, fetch)
    return result


# ---------------------------------------------------------------------------
# Strength workout creation (write path)
# ---------------------------------------------------------------------------


def _strength_token(payload: dict[str, Any]) -> str:
    """Content-bound confirmation token (HMAC of the canonical payload).

    Deterministic: the same workout produces the same token, so a token from
    ``preview`` only validates the exact workout it previewed. Empty when no
    JWT secret is configured (stdio/dev), in which case ``confirm=True`` is used.
    """
    if not JWT_SECRET:
        return ""
    # Domain-separate from access-token signing that also uses JWT_SECRET.
    canonical = "strength-workout-v1:" + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(JWT_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()[:16]


def _spec_from_input(
    workout: StrengthWorkoutInput,
) -> tuple[StrengthWorkoutSpec, list[ResolvedExerciseInfo], list[str]]:
    """Resolve free-text exercises to Garmin enums and build a builder spec."""
    if not workout.blocks:
        raise ValueError("workout has no blocks")
    blocks: list[BlockSpec] = []
    infos: list[ResolvedExerciseInfo] = []
    warnings: list[str] = []
    for block in workout.blocks:
        set_specs: list[SetSpec] = []
        for ex in block.exercises:
            if ex.exercise_name:
                # Explicit override. category is optional — derive it from the
                # catalog when omitted rather than silently ignoring the override.
                name = ex.exercise_name
                category = ex.category or category_for_exercise(name)
                if category is None:
                    raise ValueError(
                        f"'{ex.name}': exercise_name '{name}' is not in Garmin's "
                        "catalog and no 'category' was given — pass an explicit "
                        "'category' too, or use a name the resolver knows."
                    )
                confidence, label = "explicit", name.replace("_", " ").title()
            elif ex.category:
                # A category alone cannot identify an exercise (bare category
                # names blank on upload); don't silently fall back to the resolver.
                raise ValueError(
                    f"'{ex.name}': 'category' was given without 'exercise_name'. "
                    "Also pass 'exercise_name', or omit 'category' to use the "
                    "name resolver."
                )
            else:
                r = resolve_exercise(ex.name)
                category = r.category or "UNKNOWN"
                name = r.exercise_name or ex.name.upper().replace(" ", "_")
                confidence, label = r.confidence, r.matched_label or ex.name
                if not r.is_usable:
                    warnings.append(f"'{ex.name}' -> {category}/{name} ({confidence})")
            infos.append(
                ResolvedExerciseInfo(
                    query=ex.name,
                    category=category,
                    exercise_name=name,
                    confidence=confidence,
                    matched_label=label,
                )
            )
            set_specs.append(
                SetSpec(
                    category=category,
                    exercise_name=name,
                    reps=ex.reps,
                    seconds=ex.seconds,
                    weight=ex.weight,
                    weight_unit=ex.weight_unit,
                    label=ex.name,
                    note=ex.note,
                )
            )
        blocks.append(BlockSpec(sets=block.sets, exercises=set_specs))
    spec = StrengthWorkoutSpec(
        name=workout.name, blocks=blocks, include_warmup=workout.include_warmup
    )
    return spec, infos, warnings


def _blank_interval_steps(wj: dict[str, Any]) -> list[int]:
    """Step orders of interval steps whose exerciseName came back blank.

    Garmin returns HTTP 200 but silently blanks unknown/category-name exercises;
    re-fetching after create and checking this is how we catch that.
    """
    blanks: list[int] = []

    def walk(steps: list[dict[str, Any]]) -> None:
        for s in steps:
            if s.get("type") == "RepeatGroupDTO":
                walk(s.get("workoutSteps", []))
            elif (s.get("stepType") or {}).get("stepTypeKey") == "interval" and not s.get(
                "exerciseName"
            ):
                # Defensive: stepOrder may be absent, null, or non-numeric in
                # Garmin's re-fetched JSON; int(None) would crash the verify walk.
                order = _opt_int(s.get("stepOrder"))
                blanks.append(order if order is not None else -1)

    for seg in wj.get("workoutSegments", []):
        walk(seg.get("workoutSteps", []))
    return blanks


@mcp.tool()
async def preview_strength_workout(workout: StrengthWorkoutInput) -> StrengthWorkoutPreview:
    """Assemble a strength workout and show what WOULD be created — no network call.

    Resolves each exercise name to Garmin's catalog and returns a readable
    summary, the resolved Garmin name + confidence per exercise, warnings for
    anything that did not map cleanly, and a ``confirmation_token`` to pass to
    ``create_strength_workout``. Always call this first and review the result.

    Args:
        workout: the workout definition (name, blocks of sets/exercises).
    """
    spec, infos, warnings = _spec_from_input(workout)
    payload = build_strength_workout(spec)
    return StrengthWorkoutPreview(
        name=workout.name,
        summary=summarize(spec),
        exercises=infos,
        warnings=warnings,
        confirmation_token=_strength_token(payload),
    )


@mcp.tool()
async def create_strength_workout(
    workout: StrengthWorkoutInput,
    confirmation_token: str = "",
    confirm: bool = False,
) -> StrengthWorkoutCreated:
    """Create a strength workout in your Garmin Connect library (a WRITE).

    Requires writes to be enabled (GARMIN_WRITE_ENABLED) and a matching
    ``confirmation_token`` from ``preview_strength_workout`` (it binds to the
    exact workout previewed). The workout lands in your library — it reaches the
    watch only after you "Send to Device" in Garmin Connect and sync.

    Args:
        workout: the workout definition (same shape as preview).
        confirmation_token: token returned by ``preview_strength_workout``.
        confirm: dev/stdio only (no JWT secret) — set True to confirm.
    """
    if not WRITE_ENABLED:
        log.warning("workout.create.denied", name=workout.name, reason="writes_disabled")
        raise ValueError(
            "Writes are disabled. Set GARMIN_WRITE_ENABLED=1 to allow creating workouts."
        )

    spec, infos, _warnings = _spec_from_input(workout)
    payload = build_strength_workout(spec)

    expected = _strength_token(payload)
    if expected:
        # Constant-time compare: confirmation_token is HMAC-derived from the
        # previewed payload, so a plain != would leak it byte-by-byte via timing.
        if not hmac.compare_digest(confirmation_token, expected):
            raise ValueError(
                "confirmation_token does not match this workout — call "
                "preview_strength_workout again and pass its token."
            )
    elif confirm and _TRANSPORT == "stdio":
        # Dev/stdio only: no JWT secret to bind a token, so an explicit confirm
        # stands in. Never reachable over HTTP (see run_http's start-up guard).
        log.info("workout.create.dev_confirm", name=workout.name)
    else:
        raise ValueError(
            "Cannot confirm this write: set JWT_SECRET and pass the token from "
            "preview_strength_workout. (confirm=True is honored only on local stdio.)"
        )

    valid = valid_exercise_names()
    bad = sorted(
        {s.exercise_name for b in spec.blocks for s in b.exercises if s.exercise_name not in valid}
    )
    if bad:
        raise ValueError(f"Unknown Garmin exercise name(s): {', '.join(bad)}")

    client = _get_garmin()
    log.info("workout.create.attempt", name=workout.name, exercises=len(infos))
    result = await client.call("upload_workout", payload)
    workout_id = (
        str(result["workoutId"])
        if isinstance(result, dict) and result.get("workoutId") is not None
        else None
    )

    blanks: list[int] = []
    verified = False
    if workout_id is not None:
        try:
            check = await client.call("get_workout_by_id", workout_id)
            blanks = _blank_interval_steps(check) if isinstance(check, dict) else []
            verified = not blanks
        except Exception as exc:
            # The upload already succeeded; a verify failure (network or a
            # malformed re-fetch) must not turn a created workout into an error.
            log.warning("workout.create.verify_failed", workout_id=workout_id, error=str(exc)[:200])
    log.info("workout.create.success", workout_id=workout_id, name=workout.name, verified=verified)

    status = (
        f"Created '{workout.name}'"
        + (f" (id {workout_id})" if workout_id else "")
        + " in your Garmin Connect library. It is NOT on the watch yet — open it in "
        "Garmin Connect, tap 'Send to Device', then sync your watch (Training > Workouts)."
    )
    if blanks:
        status += f" WARNING: Garmin blanked step(s) {blanks} — review those exercises."
    return StrengthWorkoutCreated(
        workout_id=workout_id,
        name=workout.name,
        status=status,
        verified=verified,
        blank_steps=blanks,
    )


def _workout_summary(raw: dict[str, Any]) -> WorkoutSummary | None:
    workout_id = _opt_str(raw.get("workoutId"))
    if workout_id is None:
        return None
    sport = raw.get("sportType")
    return WorkoutSummary(
        workout_id=workout_id,
        name=_opt_str(raw.get("workoutName")),
        sport=_opt_str(sport.get("sportTypeKey")) if isinstance(sport, dict) else None,
        updated=_opt_str(raw.get("updatedDate") or raw.get("updateDate")),
    )


@mcp.tool()
async def list_workouts(limit: int = 50) -> WorkoutList:
    """List workouts saved in your Garmin Connect library (id, name, sport).

    These are workout *templates* in the library — the source the watch syncs
    from — not logged activities. Use the ids with ``delete_workout``.

    Args:
        limit: maximum number of workouts to return (default 50).
    """
    # Deliberately uncached: create_strength_workout / delete_workout change
    # this list within a session, and a stale listing here could aim a delete
    # at the wrong target.
    client = _get_garmin()
    raw = await client.call("get_workouts", 0, max(1, min(limit, 200)))
    items = raw if isinstance(raw, list) else []
    workouts = [w for w in (_workout_summary(i) for i in items if isinstance(i, dict)) if w]
    return WorkoutList(workouts=workouts, count=len(workouts))


def _delete_token(workout_id: str) -> str:
    """Confirmation token for deleting one specific workout.

    Bound to the workout id, so a token previewed for one workout can never
    confirm the deletion of another. Empty when no JWT secret is configured
    (stdio/dev), in which case ``confirm=True`` is used.
    """
    if not JWT_SECRET:
        return ""
    canonical = f"delete-workout-v1:{workout_id}"
    return hmac.new(JWT_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()[:16]


@mcp.tool()
async def delete_workout(
    workout_id: str,
    confirmation_token: str = "",
    confirm: bool = False,
) -> WorkoutDeleted:
    """Delete ONE workout from your Garmin Connect library (a WRITE, irreversible).

    Preview-then-confirm: call without a token first — that call deletes
    nothing and returns the workout's name plus a ``confirmation_token``.
    Review the name, then call again with the token to actually delete.
    Deleting from the library also removes the workout from the watch on its
    next sync. There is no bulk delete; confirm each workout individually.

    Args:
        workout_id: id from ``list_workouts``.
        confirmation_token: token from the preview call for this same id.
        confirm: dev/stdio only (no JWT secret) — set True to confirm.
    """
    if not WRITE_ENABLED:
        log.warning("workout.delete.denied", workout_id=workout_id, reason="writes_disabled")
        raise ValueError(
            "Writes are disabled. Set GARMIN_WRITE_ENABLED=1 to allow deleting workouts."
        )

    client = _get_garmin()
    # Always fetch first: proves the id exists and gives the user the name they
    # are about to delete. A typo'd id fails here, before any token is issued.
    raw = await client.call("get_workout_by_id", workout_id)
    name = _opt_str(raw.get("workoutName")) if isinstance(raw, dict) else None

    expected = _delete_token(workout_id)
    if expected:
        if not confirmation_token:
            return WorkoutDeleted(
                workout_id=workout_id,
                name=name,
                deleted=False,
                status=(
                    f"NOT deleted. This would delete '{name or workout_id}'. Pass "
                    "confirmation_token back to delete_workout to confirm."
                ),
                confirmation_token=expected,
            )
        # Constant-time compare: same rationale as create_strength_workout.
        if not hmac.compare_digest(confirmation_token, expected):
            raise ValueError(
                "confirmation_token does not match this workout_id — call "
                "delete_workout without a token first and use the token it returns."
            )
    elif confirm and _TRANSPORT == "stdio":
        # Dev/stdio only: no JWT secret to bind a token, so an explicit confirm
        # stands in. Never reachable over HTTP (see run_http's start-up guard).
        log.info("workout.delete.dev_confirm", workout_id=workout_id)
    else:
        return WorkoutDeleted(
            workout_id=workout_id,
            name=name,
            deleted=False,
            status=(
                f"NOT deleted. This would delete '{name or workout_id}'. "
                "Call again with confirm=True to confirm (local stdio only)."
            ),
        )

    log.info("workout.delete.attempt", workout_id=workout_id, name=name)
    await client.call("delete_workout", workout_id)
    log.info("workout.delete.success", workout_id=workout_id, name=name)
    return WorkoutDeleted(
        workout_id=workout_id,
        name=name,
        deleted=True,
        status=f"Deleted '{name or workout_id}' from your Garmin Connect library. "
        "It will disappear from the watch on its next sync.",
    )


# ---------------------------------------------------------------------------
# Garmin response parsers
#
# Garmin's payload shapes are not formally documented and shift over time.
# Each parser is defensive: it pulls fields when present and returns a model
# with ``None`` values otherwise.
# ---------------------------------------------------------------------------


def _parse_sleep(raw: Any, date: str) -> SleepSummary:
    if not raw or not isinstance(raw, dict):
        return SleepSummary(date=date, note="No sleep data was recorded for this date.")

    daily = raw.get("dailySleepDTO") or {}
    sleep_score: int | None = None
    sleep_quality: str | None = None
    scores = daily.get("sleepScores") or {}
    overall = scores.get("overall") if isinstance(scores, dict) else None
    if isinstance(overall, dict):
        sleep_score = _opt_int(overall.get("value"))
        sleep_quality = _opt_str(overall.get("qualifierKey"))

    hrv = raw.get("hrvSummary") or {}
    avg_hrv = _opt_float(hrv.get("lastNightAvg")) if isinstance(hrv, dict) else None

    summary = SleepSummary(
        date=daily.get("calendarDate") or date,
        sleep_score=sleep_score,
        sleep_quality=sleep_quality,
        total_sleep_seconds=_opt_int(daily.get("sleepTimeSeconds")),
        deep_sleep_seconds=_opt_int(daily.get("deepSleepSeconds")),
        light_sleep_seconds=_opt_int(daily.get("lightSleepSeconds")),
        rem_sleep_seconds=_opt_int(daily.get("remSleepSeconds")),
        awake_seconds=_opt_int(daily.get("awakeSleepSeconds")),
        avg_overnight_hrv_ms=avg_hrv,
        avg_respiration=_opt_float(daily.get("averageRespirationValue")),
        avg_spo2=_opt_float(daily.get("averageSpO2Value")),
    )
    if summary.total_sleep_seconds in (None, 0):
        summary = summary.model_copy(update={"note": "No sleep data was recorded for this date."})
    return summary


def _parse_activity_list(raw: Any) -> ActivityList:
    if not isinstance(raw, list):
        return ActivityList(activities=[], count=0)
    out: list[Activity] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        activity_type_dto = item.get("activityType") or {}
        type_key = activity_type_dto.get("typeKey") if isinstance(activity_type_dto, dict) else None
        out.append(
            Activity(
                activity_id=str(item.get("activityId")),
                name=_opt_str(item.get("activityName")),
                activity_type=_opt_str(type_key),
                start_time=_opt_str(item.get("startTimeLocal")),
                duration_seconds=_opt_float(item.get("duration")),
                distance_meters=_opt_float(item.get("distance")),
                avg_heart_rate=_opt_float(item.get("averageHR")),
                max_heart_rate=_opt_float(item.get("maxHR")),
                calories=_opt_float(item.get("calories")),
            )
        )
    return ActivityList(activities=out, count=len(out))


def _parse_activity_detail(
    activity_id: str,
    summary: Any,
    splits: Any,
    zones: Any,
) -> ActivityDetail:
    if not isinstance(summary, dict):
        return ActivityDetail(activity_id=str(activity_id))

    summary_dto = summary.get("summaryDTO") or summary
    activity_type_dto = summary.get("activityTypeDTO") or summary.get("activityType") or {}
    type_key = activity_type_dto.get("typeKey") if isinstance(activity_type_dto, dict) else None

    detail = ActivityDetail(
        activity_id=str(activity_id),
        name=_opt_str(summary.get("activityName")),
        activity_type=_opt_str(type_key),
        start_time=_opt_str(summary_dto.get("startTimeLocal") or summary.get("startTimeLocal")),
        duration_seconds=_opt_float(summary_dto.get("duration") or summary.get("duration")),
        distance_meters=_opt_float(summary_dto.get("distance") or summary.get("distance")),
        avg_heart_rate=_opt_float(summary_dto.get("averageHR") or summary.get("averageHR")),
        max_heart_rate=_opt_float(summary_dto.get("maxHR") or summary.get("maxHR")),
        avg_power=_opt_float(summary_dto.get("averagePower") or summary.get("avgPower")),
        max_power=_opt_float(summary_dto.get("maxPower") or summary.get("maxPower")),
        normalised_power=_opt_float(summary_dto.get("normalizedPower") or summary.get("normPower")),
        calories=_opt_float(summary_dto.get("calories") or summary.get("calories")),
        elevation_gain_meters=_opt_float(
            summary_dto.get("elevationGain") or summary.get("elevationGain")
        ),
        elevation_loss_meters=_opt_float(
            summary_dto.get("elevationLoss") or summary.get("elevationLoss")
        ),
        avg_speed_mps=_opt_float(summary_dto.get("averageSpeed") or summary.get("averageSpeed")),
        max_speed_mps=_opt_float(summary_dto.get("maxSpeed") or summary.get("maxSpeed")),
        training_effect_aerobic=_opt_float(
            summary_dto.get("trainingEffect") or summary.get("aerobicTrainingEffect")
        ),
        training_effect_anaerobic=_opt_float(
            summary_dto.get("anaerobicTrainingEffect") or summary.get("anaerobicTrainingEffect")
        ),
    )

    if isinstance(splits, dict):
        lap_dtos = splits.get("lapDTOs") or splits.get("splits") or []
    elif isinstance(splits, list):
        lap_dtos = splits
    else:
        lap_dtos = []
    parsed_splits: list[ActivitySplit] = []
    for idx, lap in enumerate(lap_dtos):
        if not isinstance(lap, dict):
            continue
        parsed_splits.append(
            ActivitySplit(
                split_index=idx + 1,
                distance_meters=_opt_float(lap.get("distance")),
                duration_seconds=_opt_float(lap.get("duration")),
                avg_heart_rate=_opt_float(lap.get("averageHR")),
                avg_speed_mps=_opt_float(lap.get("averageSpeed")),
                elevation_gain_meters=_opt_float(lap.get("elevationGain")),
            )
        )
    detail = detail.model_copy(update={"splits": parsed_splits})

    if isinstance(zones, list):
        parsed_zones: list[HRZone] = []
        for z in zones:
            if not isinstance(z, dict):
                continue
            parsed_zones.append(
                HRZone(
                    zone_number=_opt_int(z.get("zoneNumber")),
                    seconds_in_zone=_opt_float(z.get("secsInZone")),
                    zone_low_boundary=_opt_int(z.get("zoneLowBoundary")),
                )
            )
        detail = detail.model_copy(update={"hr_zones": parsed_zones})

    return detail


def _parse_training_load_day(date: str, raw: Any) -> TrainingLoadDay:
    if not isinstance(raw, dict):
        return TrainingLoadDay(date=date)
    atl_dto = raw.get("acuteTrainingLoadDTO") or {}
    if not isinstance(atl_dto, dict):
        atl_dto = {}
    most_recent = raw.get("mostRecentTrainingStatus") or {}
    status_str: str | None = None
    if isinstance(most_recent, dict):
        latest = most_recent.get("latestTrainingStatusData") or {}
        if isinstance(latest, dict):
            for entry in latest.values():
                if isinstance(entry, dict) and entry.get("trainingStatusFeedbackPhrase"):
                    status_str = _opt_str(entry.get("trainingStatusFeedbackPhrase"))
                    break
                if isinstance(entry, dict) and entry.get("trainingStatus"):
                    status_str = _opt_str(entry.get("trainingStatus"))
                    break
    return TrainingLoadDay(
        date=date,
        daily_training_load=_opt_float(atl_dto.get("dailyTrainingLoadAcute")),
        acute_training_load=_opt_float(atl_dto.get("acuteTrainingLoad")),
        chronic_training_load=_opt_float(atl_dto.get("chronicTrainingLoad")),
        training_status=status_str,
    )


def _build_training_load_summary(
    per_day: list[TrainingLoadDay],
    latest_status: dict[str, Any] | None,
) -> TrainingLoadSummary:
    current_status = None
    current_atl = None
    current_ctl = None
    if latest_status is not None:
        atl_dto = latest_status.get("acuteTrainingLoadDTO") or {}
        if isinstance(atl_dto, dict):
            current_atl = _opt_float(atl_dto.get("acuteTrainingLoad"))
            current_ctl = _opt_float(atl_dto.get("chronicTrainingLoad"))
    for day in reversed(per_day):
        if day.training_status is not None:
            current_status = day.training_status
            break
    return TrainingLoadSummary(
        days=per_day,
        current_status=current_status,
        current_atl=current_atl,
        current_ctl=current_ctl,
    )


def _parse_hrv_day(date: str, raw: Any) -> HRVDayReading:
    if not isinstance(raw, dict):
        return HRVDayReading(date=date)
    summary = raw.get("hrvSummary") or {}
    if not isinstance(summary, dict):
        return HRVDayReading(date=date)
    return HRVDayReading(
        date=_opt_str(summary.get("calendarDate")) or date,
        last_night_avg_ms=_opt_float(summary.get("lastNightAvg")),
        last_night_5min_high_ms=_opt_float(summary.get("lastNight5MinHigh")),
        status=_opt_str(summary.get("status")),
    )


def _build_hrv_status(
    headline: dict[str, Any] | None,
    readings: list[HRVDayReading],
) -> HRVStatus:
    if headline is None or not isinstance(headline, dict):
        return HRVStatus(last_7_days=readings)
    summary = headline.get("hrvSummary") or {}
    if not isinstance(summary, dict):
        return HRVStatus(last_7_days=readings)
    baseline = summary.get("baseline") or {}
    return HRVStatus(
        status=_opt_str(summary.get("status")),
        last_night_avg_ms=_opt_float(summary.get("lastNightAvg")),
        weekly_avg_ms=_opt_float(summary.get("weeklyAvg")),
        baseline_low_ms=_opt_float(baseline.get("lowUpper"))
        if isinstance(baseline, dict)
        else None,
        baseline_high_ms=_opt_float(baseline.get("balancedUpper"))
        if isinstance(baseline, dict)
        else None,
        feedback=_opt_str(summary.get("feedbackPhrase")),
        last_7_days=readings,
    )


def _parse_body_battery(raw: Any, date: str) -> BodyBatterySummary:
    if isinstance(raw, list):
        if not raw:
            return BodyBatterySummary(date=date)
        first = raw[0]
        if not isinstance(first, dict):
            return BodyBatterySummary(date=date)
    elif isinstance(raw, dict):
        first = raw
    else:
        return BodyBatterySummary(date=date)

    timeline_raw = first.get("bodyBatteryValuesArray") or []
    timeline: list[BodyBatteryReading] = []
    for entry in timeline_raw:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        ts_iso = _seconds_to_iso_local(_opt_int(entry[0]))
        if ts_iso is None:
            continue
        value: int | None = None
        status: str | None = None
        # Garmin returns either [ts, value] or [ts, status, value, version].
        if len(entry) == 2:
            value = _opt_int(entry[1])
        else:
            status = _opt_str(entry[1])
            value = _opt_int(entry[2]) if len(entry) > 2 else None
        timeline.append(BodyBatteryReading(timestamp=ts_iso, value=value, status=status))

    values = [r.value for r in timeline if r.value is not None]
    current_value = next(
        (r.value for r in reversed(timeline) if r.value is not None),
        None,
    )

    return BodyBatterySummary(
        date=_opt_str(first.get("date")) or date,
        max_value=max(values) if values else None,
        min_value=min(values) if values else None,
        charged=_opt_int(first.get("charged")),
        drained=_opt_int(first.get("drained")),
        current_value=current_value,
        timeline=timeline,
    )


def _parse_steps_and_calories(raw: Any, date: str) -> StepsAndCalories:
    if not isinstance(raw, dict):
        return StepsAndCalories(date=date)
    return StepsAndCalories(
        date=_opt_str(raw.get("calendarDate")) or date,
        total_steps=_opt_int(raw.get("totalSteps")),
        step_goal=_opt_int(raw.get("dailyStepGoal") or raw.get("stepGoal")),
        total_distance_meters=_opt_float(raw.get("totalDistanceMeters")),
        total_calories=_opt_int(raw.get("totalKilocalories") or raw.get("totalCalories")),
        active_calories=_opt_int(raw.get("activeKilocalories") or raw.get("activeCalories")),
        bmr_calories=_opt_int(raw.get("bmrKilocalories") or raw.get("bmrCalories")),
        floors_climbed=_opt_int(raw.get("floorsAscended") or raw.get("floorsClimbed")),
        moderate_intensity_minutes=_opt_int(raw.get("moderateIntensityMinutes")),
        vigorous_intensity_minutes=_opt_int(raw.get("vigorousIntensityMinutes")),
    )


def _parse_rhr_day(date: str, raw: Any) -> RHRDay:
    if not isinstance(raw, dict):
        return RHRDay(date=date)
    rhr: int | None = None
    # Garmin nests RHR as allMetrics.metricsMap.WELLNESS_RESTING_HEART_RATE[*].value
    metrics_map = (raw.get("allMetrics") or {}).get("metricsMap")
    if isinstance(metrics_map, dict):
        for entries in metrics_map.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("value") is not None:
                    rhr = _opt_int(entry.get("value"))
                    break
            if rhr is not None:
                break
    if rhr is None:
        rhr = _opt_int(raw.get("restingHeartRate") or raw.get("currentDayRestingHeartRate"))
    return RHRDay(date=date, rhr_bpm=rhr)


def _parse_stress(raw: Any, date: str) -> StressSummary:
    if not isinstance(raw, dict):
        return StressSummary(date=date)
    timeline_raw = raw.get("stressValuesArray") or []
    buckets: list[StressBucket] = []
    for entry in timeline_raw:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        ts_iso = _seconds_to_iso_local(_opt_int(entry[0]))
        if ts_iso is None:
            continue
        buckets.append(StressBucket(timestamp=ts_iso, stress_level=_opt_int(entry[1])))

    return StressSummary(
        date=_opt_str(raw.get("calendarDate")) or date,
        avg_stress=_opt_int(raw.get("avgStressLevel") or raw.get("averageStressLevel")),
        max_stress=_opt_int(raw.get("maxStressLevel")),
        rest_minutes=_minutes_or_none(raw.get("restStressDuration")),
        low_minutes=_minutes_or_none(raw.get("lowStressDuration")),
        medium_minutes=_minutes_or_none(raw.get("mediumStressDuration")),
        high_minutes=_minutes_or_none(raw.get("highStressDuration")),
        timeline=buckets,
    )


def _minutes_or_none(seconds: Any) -> int | None:
    secs = _opt_int(seconds)
    if secs is None:
        return None
    return secs // 60


def _first_dict(raw: Any) -> dict[str, Any] | None:
    """Garmin sometimes returns a single dict and sometimes a 1-element list."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return raw[0]
    return None


def _parse_training_readiness(raw: Any, date: str) -> TrainingReadiness:
    payload = _first_dict(raw)
    if payload is None:
        return TrainingReadiness(date=date, note="No training readiness data for this date.")

    factors: list[TrainingReadinessFactor] = []
    for label, key in (
        ("Sleep", "sleepScoreFactorFeedback"),
        ("Recovery", "recoveryTimeFactorFeedback"),
        ("HRV", "hrvFactorFeedback"),
        ("Stress", "stressHistoryFactorFeedback"),
        ("Acute load", "acuteLoadFactorFeedback"),
        ("Sleep history", "sleepHistoryFactorFeedback"),
    ):
        feedback = _opt_str(payload.get(key))
        if feedback:
            factors.append(TrainingReadinessFactor(name=label, feedback=feedback))

    return TrainingReadiness(
        date=_opt_str(payload.get("calendarDate")) or date,
        score=_opt_int(payload.get("score")),
        level=_opt_str(payload.get("level")),
        feedback_short=_opt_str(payload.get("feedbackShort")),
        feedback_long=_opt_str(payload.get("feedbackLong")),
        sleep_score=_opt_int(payload.get("sleepScore")),
        sleep_history_score=_opt_int(payload.get("sleepHistoryFactorPercent")),
        recovery_time_hours=_opt_int(payload.get("recoveryTime")),
        acute_load=_opt_float(payload.get("acuteLoad")),
        hrv_status=_opt_str(payload.get("hrvStatus")),
        stress_history=_opt_int(payload.get("stressHistoryFactorPercent")),
        factors=factors,
    )


def _parse_fitness_metrics(
    date: str,
    max_metrics: Any,
    race: Any,
) -> FitnessMetrics:
    vo2_run: float | None = None
    vo2_cycle: float | None = None
    fitness_age: float | None = None

    # get_max_metrics returns either a list of {generic, cycling, ...} or a dict.
    max_payload = _first_dict(max_metrics)
    if max_payload is not None:
        generic = max_payload.get("generic") or {}
        cycling = max_payload.get("cycling") or {}
        heat = max_payload.get("heatAltitudeAcclimation") or {}
        if isinstance(generic, dict):
            vo2_run = _opt_float(generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue"))
            fitness_age = _opt_float(generic.get("fitnessAge"))
        if isinstance(cycling, dict):
            vo2_cycle = _opt_float(cycling.get("vo2MaxPreciseValue") or cycling.get("vo2MaxValue"))
        if fitness_age is None and isinstance(heat, dict):
            fitness_age = _opt_float(heat.get("fitnessAge"))

    predictions: list[RacePrediction] = []
    race_payload: dict[str, Any] | None
    if isinstance(race, list) and race:
        race_payload = race[0] if isinstance(race[0], dict) else None
    elif isinstance(race, dict):
        race_payload = race
    else:
        race_payload = None
    if race_payload is not None:
        for label, key in (
            ("5k", "time5K"),
            ("10k", "time10K"),
            ("halfMarathon", "timeHalfMarathon"),
            ("marathon", "timeMarathon"),
        ):
            value = _opt_int(race_payload.get(key))
            if value is not None:
                predictions.append(RacePrediction(distance=label, seconds=value))

    note: str | None = None
    if max_payload is None and not predictions:
        note = "No fitness metrics recorded yet. Wear your watch for runs/rides."

    return FitnessMetrics(
        date=date,
        vo2_max_running=vo2_run,
        vo2_max_cycling=vo2_cycle,
        fitness_age=fitness_age,
        race_predictions=predictions,
        note=note,
    )


# Garmin's personal-record typeId enum is not documented in the upstream
# library. Mapping observed via real-account inspection. Tuple is (label, unit).
_PR_TYPE_MAP: dict[int, tuple[str, str]] = {
    1: ("fastest_1k", "seconds"),
    2: ("fastest_1mi", "seconds"),
    3: ("fastest_5k", "seconds"),
    4: ("fastest_10k", "seconds"),
    5: ("fastest_half_marathon", "seconds"),
    6: ("fastest_marathon", "seconds"),
    7: ("longest_run", "meters"),
    8: ("longest_ride", "meters"),
    9: ("longest_swim", "meters"),
    10: ("most_ascent", "meters"),
    12: ("most_steps_in_a_day", "count"),
    13: ("most_steps_in_a_week", "count"),
    14: ("most_steps_in_a_month", "count"),
    15: ("longest_goal_streak", "days"),
    16: ("longest_active_streak", "days"),
}


def _parse_personal_records(raw: Any) -> PersonalRecords:
    if not isinstance(raw, list):
        return PersonalRecords(records=[], count=0)
    out: list[PersonalRecord] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        raw_value = _opt_float(entry.get("value"))
        type_id = _opt_int(entry.get("typeId"))

        label: str | None = None
        unit: str | None = None
        if type_id is not None and type_id in _PR_TYPE_MAP:
            label, unit = _PR_TYPE_MAP[type_id]
        if label is None:
            label = (
                _opt_str(entry.get("prTypeLabelKey"))
                or _opt_str(entry.get("activityType"))
                or (f"type_{type_id}" if type_id is not None else "unknown")
            )

        value_seconds = raw_value if unit == "seconds" else None
        value_meters = raw_value if unit == "meters" else None

        out.append(
            PersonalRecord(
                record_type=label,
                unit=unit,
                raw_value=raw_value,
                value_seconds=value_seconds,
                value_meters=value_meters,
                type_id=type_id,
                activity_type=_opt_str(entry.get("activityType")),
                record_date=_opt_str(entry.get("prStartTimeGmtFormatted"))
                or _opt_str(entry.get("startTimeLocal")),
                activity_id=_opt_str(entry.get("activityId")),
            )
        )
    return PersonalRecords(records=out, count=len(out))


def _parse_body_composition(raw: Any) -> BodyCompositionTrend:
    if not isinstance(raw, dict):
        return BodyCompositionTrend(note="No body composition data recorded.")

    daily_list = raw.get("dateWeightList") or raw.get("totalAverage") or []
    if isinstance(daily_list, dict):
        daily_list = [daily_list]
    if not isinstance(daily_list, list):
        daily_list = []

    days: list[BodyCompositionDay] = []
    for entry in daily_list:
        if not isinstance(entry, dict):
            continue
        # Garmin weights are reported in grams; convert to kg.
        weight_g = _opt_float(entry.get("weight"))
        weight_kg = weight_g / 1000.0 if weight_g is not None else None
        muscle_g = _opt_float(entry.get("muscleMass"))
        muscle_kg = muscle_g / 1000.0 if muscle_g is not None else None
        bone_g = _opt_float(entry.get("boneMass"))
        bone_kg = bone_g / 1000.0 if bone_g is not None else None
        days.append(
            BodyCompositionDay(
                date=_opt_str(entry.get("calendarDate"))
                or _seconds_to_iso_local(_opt_int(entry.get("date")))
                or "",
                weight_kg=weight_kg,
                body_fat_percent=_opt_float(entry.get("bodyFat")),
                body_water_percent=_opt_float(entry.get("bodyWater")),
                muscle_mass_kg=muscle_kg,
                bone_mass_kg=bone_kg,
                bmi=_opt_float(entry.get("bmi")),
            )
        )

    weights = [d.weight_kg for d in days if d.weight_kg is not None]
    return BodyCompositionTrend(
        days=days,
        latest_weight_kg=weights[-1] if weights else None,
        avg_weight_kg=sum(weights) / len(weights) if weights else None,
        note=None if days else "No body composition data recorded in this window.",
    )


def _parse_respiration(raw: Any, date: str) -> RespirationSummary:
    if not isinstance(raw, dict):
        return RespirationSummary(date=date, note="No respiration data recorded.")
    avg = _opt_float(raw.get("avgRespirationValue"))
    sleep_v = _opt_float(raw.get("avgSleepRespirationValue"))
    wake_v = _opt_float(raw.get("avgWakingRespirationValue"))
    # Garmin doesn't always populate an overall daily average; synthesise one
    # from sleep + waking when both are present.
    if avg is None and sleep_v is not None and wake_v is not None:
        avg = (sleep_v + wake_v) / 2
    summary = RespirationSummary(
        date=_opt_str(raw.get("calendarDate")) or date,
        avg_breaths_per_min=avg,
        lowest_breaths_per_min=_opt_float(raw.get("lowestRespirationValue")),
        highest_breaths_per_min=_opt_float(raw.get("highestRespirationValue")),
        avg_sleep_breaths_per_min=sleep_v,
        avg_waking_breaths_per_min=wake_v,
    )
    if summary.avg_breaths_per_min is None:
        summary = summary.model_copy(update={"note": "No respiration data recorded for this date."})
    return summary


def _parse_weekly_summary(metric: str, raw: Any) -> WeeklySummary:
    if not isinstance(raw, list) or not raw:
        return WeeklySummary(metric=metric, note="No data recorded for this window.")

    buckets: list[WeeklyBucket] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # Garmin's weekly_steps wraps the metrics in a nested `values` dict;
        # weekly_stress and weekly_intensity_minutes keep them at top level.
        raw_values = entry.get("values")
        nested: dict[str, Any] = raw_values if isinstance(raw_values, dict) else entry
        week_start = (
            _opt_str(entry.get("calendarDate"))
            or _opt_str(entry.get("weekStart"))
            or _opt_str(entry.get("startDate"))
            or ""
        )
        value: float | None
        if metric == "steps":
            value = _opt_float(nested.get("totalSteps") or nested.get("averageSteps"))
        elif metric == "stress":
            value = _opt_float(
                entry.get("value") or nested.get("value") or nested.get("averageStressLevel")
            )
        else:  # intensity_minutes
            mod = _opt_float(nested.get("weeklyModerate") or nested.get("moderateValue")) or 0.0
            vig = _opt_float(nested.get("weeklyVigorous") or nested.get("vigorousValue")) or 0.0
            value = mod + vig * 2  # Garmin convention: vigorous counts double
        buckets.append(WeeklyBucket(week_start=week_start, value=value))

    values = [b.value for b in buckets if b.value is not None]
    avg = sum(values) / len(values) if values else None
    return WeeklySummary(metric=metric, weeks=buckets, avg_value=avg)


# ---------------------------------------------------------------------------
# HTTP-only routes (login UI, health, OAuth resource metadata)
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>garmin-mcp authorize</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #fafafa; color: #222; max-width: 480px; margin: 80px auto; padding: 24px; }}
  h1 {{ margin-top: 0; font-size: 24px; }}
  p {{ line-height: 1.5; }}
  input[type=password] {{ width: 100%; padding: 12px 14px; font-size: 16px;
         border: 1px solid #ccc; border-radius: 8px; margin: 12px 0; box-sizing: border-box; }}
  button {{ width: 100%; padding: 12px; font-size: 16px; background: #111;
         color: white; border: 0; border-radius: 8px; cursor: pointer; font-weight: 600; }}
  button:hover {{ background: #000; }}
  .error {{ color: #b00020; padding: 10px 14px; background: #fdecef;
         border-radius: 8px; margin: 12px 0; font-size: 14px; }}
  .note {{ font-size: 13px; color: #666; margin-top: 24px; }}
</style>
</head>
<body>
<h1>garmin-mcp</h1>
<p>This server connects Claude to your Garmin Connect data. Sign in to authorize the connection.</p>
{error_html}
<form method="POST" action="/login">
  <input type="hidden" name="state" value="{state}">
  <input type="password" name="password" placeholder="Server password" required autofocus autocomplete="current-password">
  <button type="submit">Authorize</button>
</form>
<p class="note">Single-user server. Use the password set as MCP_AUTH_PASSWORD on the server.</p>
</body>
</html>
"""


@mcp.custom_route("/login", methods=["GET"])
async def login_get(request: Request) -> Response:
    if _oauth_provider is None:
        return JSONResponse({"error": "Auth is disabled on this server."}, status_code=404)
    state = request.query_params.get("state", "")
    if not state:
        return HTMLResponse("Missing state parameter.", status_code=400)
    return HTMLResponse(LOGIN_HTML.format(state=state, error_html=""))


@mcp.custom_route("/login", methods=["POST"])
async def login_post(request: Request) -> Response:
    if _oauth_provider is None:
        return JSONResponse({"error": "Auth is disabled on this server."}, status_code=404)
    form = await request.form()
    state = str(form.get("state", ""))
    password = str(form.get("password", ""))
    if not state or not password:
        return HTMLResponse("Missing state or password.", status_code=400)
    try:
        redirect_url = await _oauth_provider.complete_login(state, password)
    except InvalidLoginError as exc:
        body = LOGIN_HTML.format(
            state=state,
            error_html=f'<div class="error">{exc}</div>',
        )
        return HTMLResponse(body, status_code=401)
    log.info("oauth.login.success")
    return RedirectResponse(redirect_url, status_code=302)


@mcp.custom_route("/", methods=["GET"])
async def root(_request: Request) -> Response:
    return JSONResponse(
        {
            "name": "garmin-mcp",
            "mcp_endpoint": f"{ISSUER_URL}/mcp",
            "auth": "oauth2-pkce" if AUTH_ENABLED else "disabled",
        }
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> Response:
    return JSONResponse({"status": "ok", "auth_enabled": AUTH_ENABLED})


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_stdio() -> None:
    """Run with stdio transport. Used by ``mcp dev``."""
    global _TRANSPORT
    _TRANSPORT = "stdio"
    mcp.run(transport="stdio")


def run_http() -> None:
    """Run with streamable-http transport. Used in production."""
    global _TRANSPORT
    _TRANSPORT = "http"
    if not AUTH_ENABLED:
        if WRITE_ENABLED:
            # An unauthenticated HTTP server with writes enabled would let any
            # caller create workouts (and, with no JWT secret, _strength_token
            # is empty so even the preview-token check is vacuous). Refuse.
            raise SystemExit(
                "Refusing to start: GARMIN_WRITE_ENABLED is set but HTTP auth is "
                "disabled. Set MCP_AUTH_PASSWORD and JWT_SECRET, or unset "
                "GARMIN_WRITE_ENABLED."
            )
        log.warning(
            "http.no_auth",
            message=(
                "HTTP transport without auth is unsafe. "
                "Set MCP_AUTH_PASSWORD and JWT_SECRET, then restart."
            ),
        )
    log.info("server.start", host=HOST, port=PORT, issuer=ISSUER_URL, auth=AUTH_ENABLED)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    # ``mcp dev src/garmin_mcp/server.py`` invokes this module via stdio.
    # ``python -m garmin_mcp`` is the production entry point and goes through
    # __main__.py, which calls run_http().
    if "--http" in sys.argv:
        run_http()
    else:
        run_stdio()
