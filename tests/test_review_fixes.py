"""Tests for the parsing-bug fixes and the new tools added in this review.

Payload shapes here are taken from real Garmin responses captured against a
live account (fenix 8 Pro), trimmed to the fields the parsers read.
"""

from __future__ import annotations

from typing import Any

import pytest

from garmin_mcp import server as server_module
from garmin_mcp.models import (
    ActivityWeather,
    EnduranceScore,
    HillScore,
    StrengthSession,
    StressSummary,
    TrainingLoadSummary,
)
from tests.test_tools import FakeGarminClient


@pytest.fixture(autouse=True)
async def _reset_state() -> Any:
    cache = server_module.get_cache_for_testing()
    await cache.clear()
    server_module.set_garmin_client_for_testing(None)
    yield
    server_module.set_garmin_client_for_testing(None)


# ----- bug fix: training load ATL/CTL live nested, with different field names -----

_TRAINING_STATUS: dict[str, Any] = {
    "mostRecentTrainingStatus": {
        "latestTrainingStatusData": {
            "3626073413": {
                "trainingStatusFeedbackPhrase": "STRAINED_4",
                "weeklyTrainingLoad": None,
                "primaryTrainingDevice": True,
                "acuteTrainingLoadDTO": {
                    "dailyTrainingLoadAcute": 203,
                    "dailyTrainingLoadChronic": 201,
                    "acwrPercent": 42,
                    "acwrStatus": "OPTIMAL",
                },
            }
        }
    },
}


@pytest.mark.asyncio
async def test_training_load_reads_nested_acute_chronic() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_training_status": _TRAINING_STATUS})
    )
    result = await server_module.get_training_load(days=3)
    assert isinstance(result, TrainingLoadSummary)
    assert result.current_atl == 203.0
    assert result.current_ctl == 201.0
    assert result.acwr_percent == 42.0
    assert result.acwr_status == "OPTIMAL"
    assert result.current_status == "STRAINED_4"
    # Every day populates from the same per-device record.
    assert all(d.acute_training_load == 203.0 for d in result.days)


@pytest.mark.asyncio
async def test_training_load_prefers_primary_device() -> None:
    payload = {
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "111": {
                    "primaryTrainingDevice": False,
                    "trainingStatusFeedbackPhrase": "WRONG",
                    "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 1},
                },
                "222": {
                    "primaryTrainingDevice": True,
                    "trainingStatusFeedbackPhrase": "RIGHT_1",
                    "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 250},
                },
            }
        }
    }
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_training_status": payload})
    )
    result = await server_module.get_training_load(days=1)
    assert result.current_atl == 250.0
    assert result.current_status == "RIGHT_1"


# ----- bug fix: stress time-in-zone minutes come from the daily user summary -----

_STRESS_DATA: dict[str, Any] = {
    "calendarDate": "2026-06-11",
    "avgStressLevel": 47,
    "maxStressLevel": 95,
    "stressValuesArray": [[1718064000000, 34], [1718064180000, 25]],
}

_USER_SUMMARY: dict[str, Any] = {
    "restStressDuration": 10200,  # seconds -> 170 min
    "lowStressDuration": 26940,  # -> 449 min
    "mediumStressDuration": 20100,  # -> 335 min
    "highStressDuration": 6060,  # -> 101 min
}


@pytest.mark.asyncio
async def test_stress_minutes_from_user_summary() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient(
            {"get_stress_data": _STRESS_DATA, "get_user_summary": _USER_SUMMARY}
        )
    )
    result = await server_module.get_stress(date="2026-06-11")
    assert isinstance(result, StressSummary)
    assert result.avg_stress == 47
    assert result.rest_minutes == 170
    assert result.low_minutes == 449
    assert result.medium_minutes == 335
    assert result.high_minutes == 101
    assert len(result.timeline) == 2


@pytest.mark.asyncio
async def test_stress_survives_missing_user_summary() -> None:
    # If the user-summary call fails, stress still returns with null minutes.
    from garmin_mcp.garmin_client import GarminClientError

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise GarminClientError("boom")

    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_stress_data": _STRESS_DATA, "get_user_summary": _raise})
    )
    result = await server_module.get_stress(date="2026-06-11")
    assert result.avg_stress == 47
    assert result.rest_minutes is None


# ----- bug fix: readiness hrv_status maps from hrvFactorFeedback -----


@pytest.mark.asyncio
async def test_readiness_hrv_status_from_factor_feedback() -> None:
    payload = [{"calendarDate": "2026-06-11", "score": 37, "hrvFactorFeedback": "POOR"}]
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_training_readiness": payload})
    )
    result = await server_module.get_training_readiness(date="2026-06-11")
    assert result.hrv_status == "POOR"


# ----- bug fix: weekly intensity over-fetches one week; trim to the request -----


@pytest.mark.asyncio
async def test_weekly_intensity_trimmed_to_requested_weeks() -> None:
    # Garmin returns weeks+1 because both endpoints are inclusive.
    payload = [
        {"calendarDate": "2026-05-11", "weeklyModerate": 100, "weeklyVigorous": 0},
        {"calendarDate": "2026-05-18", "weeklyModerate": 200, "weeklyVigorous": 0},
        {"calendarDate": "2026-05-25", "weeklyModerate": 300, "weeklyVigorous": 0},
        {"calendarDate": "2026-06-01", "weeklyModerate": 400, "weeklyVigorous": 0},
        {"calendarDate": "2026-06-08", "weeklyModerate": 500, "weeklyVigorous": 0},
    ]
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_weekly_intensity_minutes": payload})
    )
    result = await server_module.get_weekly_summary(
        metric="intensity_minutes", weeks=4, end_date="2026-06-11"
    )
    assert len(result.weeks) == 4
    # Keeps the four most recent, drops the oldest (2026-05-11).
    assert result.weeks[0].week_start == "2026-05-18"
    assert result.weeks[-1].week_start == "2026-06-08"


# ----- new tool: strength sets breakdown -----

_EXERCISE_SETS: dict[str, Any] = {
    "activityId": 23194603091,
    "exerciseSets": [
        {
            "exercises": [{"category": "UNKNOWN", "name": None, "probability": 99.6}],
            "duration": 1191.6,
            "repetitionCount": 30,
            "weight": None,
            "setType": "ACTIVE",
            "messageIndex": 0,
            "startTime": "2026-06-10T00:52:38.0",
        },
        {
            "exercises": [{"category": "SQUAT", "name": "LEG_PRESS", "probability": 99.6}],
            "duration": 67.3,
            "repetitionCount": 6,
            "weight": 339312.0,  # grams -> 339.31 kg
            "setType": "ACTIVE",
            "messageIndex": 1,
            "startTime": "2026-06-10T01:12:30.0",
        },
        {
            "exercises": [],
            "duration": 136.0,
            "repetitionCount": None,
            "weight": None,
            "setType": "REST",
            "messageIndex": 2,
        },
        {
            "exercises": [{"category": "SQUAT", "name": "LEG_PRESS", "probability": 99.6}],
            "duration": 37.1,
            "repetitionCount": 6,
            "weight": 339312.0,
            "setType": "ACTIVE",
            "messageIndex": 3,
        },
    ],
}


@pytest.mark.asyncio
async def test_strength_sets_aggregates_working_sets() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_activity_exercise_sets": _EXERCISE_SETS})
    )
    result = await server_module.get_strength_sets("23194963091")
    assert isinstance(result, StrengthSession)
    # 3 ACTIVE sets, 1 REST excluded from active totals.
    assert result.total_active_sets == 3
    assert result.total_reps == 42  # 30 + 6 + 6
    assert len(result.sets) == 4  # rest set still listed
    leg_press = next(e for e in result.exercises if e.exercise_name == "LEG_PRESS")
    assert leg_press.sets == 2
    assert leg_press.total_reps == 12
    assert leg_press.max_weight_kg == pytest.approx(339.31, abs=0.02)
    assert leg_press.max_weight_lb == pytest.approx(748.1, abs=0.2)
    # Volume = 6*339.31 + 6*339.31 (the no-weight warmup adds nothing).
    assert result.total_volume_kg == pytest.approx(4071.7, abs=0.5)


@pytest.mark.asyncio
async def test_strength_sets_empty_when_not_strength() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_activity_exercise_sets": {"exerciseSets": []}})
    )
    result = await server_module.get_strength_sets("999")
    assert result.total_active_sets == 0
    assert result.note is not None


# ----- new tool: endurance score -----


@pytest.mark.asyncio
async def test_endurance_score_parses() -> None:
    payload = {
        "calendarDate": "2026-06-11",
        "overallScore": 3460,
        "classification": 1,
        "feedbackPhrase": 30,
        "gaugeLowerLimit": 3570,
        "gaugeUpperLimit": 10560,
        "contributors": [
            {"activityTypeId": 9, "group": None, "contribution": 44.58},
            {"activityTypeId": None, "group": 8, "contribution": 15.43},
        ],
    }
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_endurance_score": payload})
    )
    result = await server_module.get_endurance_score(date="2026-06-11")
    assert isinstance(result, EnduranceScore)
    assert result.overall_score == 3460
    assert result.classification == 1
    assert len(result.contributors) == 2
    assert result.contributors[0].contribution_percent == pytest.approx(44.58)


@pytest.mark.asyncio
async def test_endurance_score_missing() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_endurance_score": None})
    )
    result = await server_module.get_endurance_score(date="2026-06-11")
    assert result.overall_score is None
    assert result.note is not None


# ----- new tool: hill score -----


@pytest.mark.asyncio
async def test_hill_score_empty_notes() -> None:
    payload = {
        "calendarDate": "2026-06-11",
        "overallScore": None,
        "strengthScore": None,
        "enduranceScore": None,
        "hillScoreClassificationId": 0,
        "hillScoreFeedbackPhraseId": 2,
        "vo2Max": 34.0,
        "vo2MaxPreciseValue": 34.1,
    }
    server_module.set_garmin_client_for_testing(FakeGarminClient({"get_hill_score": payload}))
    result = await server_module.get_hill_score(date="2026-06-11")
    assert isinstance(result, HillScore)
    assert result.overall_score is None
    assert result.vo2_max == 34.1
    assert result.note is not None


# ----- new tool: activity weather -----


@pytest.mark.asyncio
async def test_activity_weather_parses() -> None:
    payload = {
        "issueDate": "2026-06-11T01:54:00.000+00:00",
        "temp": 84,
        "apparentTemp": 88,
        "dewPoint": 70,
        "relativeHumidity": 62,
        "windDirection": 200,
        "windDirectionCompassPoint": "ssw",
        "windSpeed": 7,
        "windGust": None,
        "weatherStationDTO": {"id": "KMEM", "name": "Memphis Intl Airport"},
        "weatherTypeDTO": {"desc": "Fair"},
    }
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_activity_weather": payload})
    )
    result = await server_module.get_activity_weather("23206938390")
    assert isinstance(result, ActivityWeather)
    assert result.description == "Fair"
    assert result.temp == 84.0
    assert result.wind_direction_compass == "ssw"
    assert result.station_name == "Memphis Intl Airport"


@pytest.mark.asyncio
async def test_activity_weather_missing() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_activity_weather": None})
    )
    result = await server_module.get_activity_weather("1")
    assert result.note is not None
