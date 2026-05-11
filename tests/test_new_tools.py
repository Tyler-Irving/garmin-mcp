"""Tests for the v0.2.0 tools.

We don't have real Garmin payloads checked in, so each test uses the
upstream field names documented in `garminconnect/__init__.py`. The parsers
are intentionally defensive — if a field name shifts upstream, the model
field becomes `None` rather than the test failing in surprising ways.
"""

from __future__ import annotations

from typing import Any

import pytest

from garmin_mcp import server as server_module
from garmin_mcp.models import (
    BodyCompositionTrend,
    FitnessMetrics,
    PersonalRecords,
    RespirationSummary,
    TrainingReadiness,
    WeeklySummary,
)
from tests.test_tools import FakeGarminClient

READINESS_PAYLOAD: list[dict[str, Any]] = [
    {
        "calendarDate": "2026-05-10",
        "score": 78,
        "level": "HIGH",
        "feedbackShort": "READY_TO_TRAIN",
        "feedbackLong": "You're ready for a hard session.",
        "sleepScore": 82,
        "sleepHistoryFactorPercent": 70,
        "recoveryTime": 12,
        "acuteLoad": 320.0,
        "hrvStatus": "BALANCED",
        "stressHistoryFactorPercent": 65,
        "sleepScoreFactorFeedback": "GOOD",
        "recoveryTimeFactorFeedback": "RECOVERED",
        "hrvFactorFeedback": "BALANCED",
    }
]

MAX_METRICS_PAYLOAD: list[dict[str, Any]] = [
    {
        "generic": {"vo2MaxPreciseValue": 48.2, "fitnessAge": 32.0},
        "cycling": {"vo2MaxPreciseValue": 44.1},
    }
]

RACE_PREDICTIONS_PAYLOAD: list[dict[str, Any]] = [
    {
        "time5K": 1320,
        "time10K": 2780,
        "timeHalfMarathon": 6240,
        "timeMarathon": 13200,
    }
]

PERSONAL_RECORDS_PAYLOAD: list[dict[str, Any]] = [
    {
        "typeId": 2,
        "typeLabel": "5k",
        "value": 1320.0,
        "activityType": "running",
        "prStartTimeGmtFormatted": "2026-04-15 06:30:00",
        "activityId": 9988776655,
    },
    {
        "typeId": 7,
        "typeLabel": "longestRun",
        "value": 21500.0,
        "activityType": "running",
        "prStartTimeGmtFormatted": "2026-03-02 07:15:00",
        "activityId": 8877665544,
    },
]

BODY_COMPOSITION_PAYLOAD: dict[str, Any] = {
    "dateWeightList": [
        {
            "calendarDate": "2026-05-09",
            "weight": 78400.0,  # grams
            "bodyFat": 18.3,
            "bodyWater": 56.1,
            "muscleMass": 62100.0,
            "boneMass": 3200.0,
            "bmi": 23.4,
        },
        {
            "calendarDate": "2026-05-10",
            "weight": 78200.0,
            "bodyFat": 18.1,
            "bmi": 23.3,
        },
    ]
}

RESPIRATION_PAYLOAD: dict[str, Any] = {
    "calendarDate": "2026-05-10",
    "avgRespirationValue": 14.5,
    "lowestRespirationValue": 11.0,
    "highestRespirationValue": 18.0,
    "avgSleepRespirationValue": 13.2,
    "avgWakingRespirationValue": 15.4,
}

WEEKLY_STEPS_PAYLOAD: list[dict[str, Any]] = [
    {"calendarDate": "2026-04-13", "totalSteps": 52000, "averageSteps": 7428},
    {"calendarDate": "2026-04-20", "totalSteps": 60100, "averageSteps": 8585},
    {"calendarDate": "2026-04-27", "totalSteps": 47800, "averageSteps": 6828},
    {"calendarDate": "2026-05-04", "totalSteps": 55500, "averageSteps": 7928},
]


@pytest.fixture(autouse=True)
async def _reset_state() -> Any:
    cache = server_module.get_cache_for_testing()
    await cache.clear()
    server_module.set_garmin_client_for_testing(None)
    yield
    server_module.set_garmin_client_for_testing(None)


@pytest.mark.asyncio
async def test_training_readiness_parses() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_training_readiness": READINESS_PAYLOAD})
    )
    result = await server_module.get_training_readiness(date="2026-05-10")
    assert isinstance(result, TrainingReadiness)
    assert result.score == 78
    assert result.level == "HIGH"
    assert result.sleep_score == 82
    assert result.hrv_status == "BALANCED"
    assert any(f.name == "Sleep" for f in result.factors)
    assert result.note is None


@pytest.mark.asyncio
async def test_training_readiness_missing_data() -> None:
    server_module.set_garmin_client_for_testing(FakeGarminClient({"get_training_readiness": None}))
    result = await server_module.get_training_readiness(date="2026-05-10")
    assert result.score is None
    assert result.note is not None


@pytest.mark.asyncio
async def test_fitness_metrics_combines_payloads() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient(
            {
                "get_max_metrics": MAX_METRICS_PAYLOAD,
                "get_race_predictions": RACE_PREDICTIONS_PAYLOAD,
            }
        )
    )
    result = await server_module.get_fitness_metrics(date="2026-05-10")
    assert isinstance(result, FitnessMetrics)
    assert result.vo2_max_running == 48.2
    assert result.vo2_max_cycling == 44.1
    assert result.fitness_age == 32.0
    distances = {p.distance for p in result.race_predictions}
    assert {"5k", "10k", "halfMarathon", "marathon"} == distances


@pytest.mark.asyncio
async def test_personal_records_parses() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_personal_record": PERSONAL_RECORDS_PAYLOAD})
    )
    result = await server_module.get_personal_records()
    assert isinstance(result, PersonalRecords)
    assert result.count == 2
    five_k = next(r for r in result.records if r.record_type == "5k")
    assert five_k.value_seconds == 1320.0
    longest = next(r for r in result.records if r.record_type == "longestRun")
    assert longest.value_meters == 21500.0


@pytest.mark.asyncio
async def test_body_composition_converts_grams_to_kg() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_body_composition": BODY_COMPOSITION_PAYLOAD})
    )
    result = await server_module.get_body_composition(end_date="2026-05-10", days=2)
    assert isinstance(result, BodyCompositionTrend)
    assert len(result.days) == 2
    assert result.days[0].weight_kg == pytest.approx(78.4)
    assert result.days[0].muscle_mass_kg == pytest.approx(62.1)
    assert result.latest_weight_kg == pytest.approx(78.2)
    assert result.avg_weight_kg == pytest.approx(78.3)


@pytest.mark.asyncio
async def test_respiration_parses() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_respiration_data": RESPIRATION_PAYLOAD})
    )
    result = await server_module.get_respiration(date="2026-05-10")
    assert isinstance(result, RespirationSummary)
    assert result.avg_breaths_per_min == 14.5
    assert result.avg_sleep_breaths_per_min == 13.2
    assert result.note is None


@pytest.mark.asyncio
async def test_weekly_summary_steps_aggregates() -> None:
    server_module.set_garmin_client_for_testing(
        FakeGarminClient({"get_weekly_steps": WEEKLY_STEPS_PAYLOAD})
    )
    result = await server_module.get_weekly_summary(metric="steps", weeks=4, end_date="2026-05-10")
    assert isinstance(result, WeeklySummary)
    assert result.metric == "steps"
    assert len(result.weeks) == 4
    assert result.avg_value == pytest.approx(53850.0)


@pytest.mark.asyncio
async def test_weekly_summary_rejects_bad_metric() -> None:
    server_module.set_garmin_client_for_testing(FakeGarminClient({}))
    with pytest.raises(ValueError, match="metric must be one of"):
        await server_module.get_weekly_summary(metric="naps")
