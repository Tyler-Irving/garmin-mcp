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
from garmin_mcp.garmin_client import GarminClient, GarminClientError
from garmin_mcp.models import (
    Activity,
    ActivityDetail,
    ActivityList,
    ActivitySplit,
    BodyBatteryReading,
    BodyBatterySummary,
    HRVDayReading,
    HRVStatus,
    HRZone,
    RestingHeartRateTrend,
    RHRDay,
    SleepSummary,
    StepsAndCalories,
    StressBucket,
    StressSummary,
    TrainingLoadDay,
    TrainingLoadSummary,
    _opt_float,
    _opt_int,
    _opt_str,
)
from garmin_mcp.paths import default_token_dir

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
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

AUTH_ENABLED = bool(MCP_PASSWORD and JWT_SECRET)

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
    metrics = raw.get("allMetrics") or {}
    if isinstance(metrics, dict):
        for entries in metrics.values():
            if isinstance(entries, list):
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
    mcp.run(transport="stdio")


def run_http() -> None:
    """Run with streamable-http transport. Used in production."""
    if not AUTH_ENABLED:
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
