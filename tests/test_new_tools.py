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
    DailyBriefing,
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
        "typeId": 3,
        "value": 1320.0,
        "activityType": "running",
        "prStartTimeGmtFormatted": "2026-04-15 06:30:00",
        "activityId": 9988776655,
    },
    {
        "typeId": 7,
        "value": 21500.0,
        "activityType": "running",
        "prStartTimeGmtFormatted": "2026-03-02 07:15:00",
        "activityId": 8877665544,
    },
    {
        "typeId": 12,
        "value": 20178.0,
        "activityType": None,
        "prStartTimeGmtFormatted": "2025-09-13 05:00:00",
        "activityId": 0,
    },
    {
        "typeId": 99,
        "value": 42.0,
        "activityType": None,
        "prStartTimeGmtFormatted": "2026-05-01 05:00:00",
        "activityId": 0,
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

# Real Garmin shape: weekly_steps nests its metrics under a "values" dict.
WEEKLY_STEPS_PAYLOAD: list[dict[str, Any]] = [
    {"calendarDate": "2026-04-13", "values": {"totalSteps": 52000.0, "averageSteps": 7428.5}},
    {"calendarDate": "2026-04-20", "values": {"totalSteps": 60100.0, "averageSteps": 8585.7}},
    {"calendarDate": "2026-04-27", "values": {"totalSteps": 47800.0, "averageSteps": 6828.5}},
    {"calendarDate": "2026-05-04", "values": {"totalSteps": 55500.0, "averageSteps": 7928.5}},
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
    assert result.count == 4

    five_k = next(r for r in result.records if r.record_type == "fastest_5k")
    assert five_k.unit == "seconds"
    assert five_k.value_seconds == 1320.0
    assert five_k.value_meters is None

    longest = next(r for r in result.records if r.record_type == "longest_run")
    assert longest.unit == "meters"
    assert longest.value_meters == 21500.0
    assert longest.value_seconds is None

    steps = next(r for r in result.records if r.record_type == "most_steps_in_a_day")
    assert steps.unit == "count"
    assert steps.raw_value == 20178.0
    assert steps.value_seconds is None and steps.value_meters is None

    # Unknown typeId falls back to `type_<id>` with no unit.
    unknown = next(r for r in result.records if r.type_id == 99)
    assert unknown.record_type == "type_99"
    assert unknown.unit is None


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


# ----- regression tests for bugs uncovered by the v0.2.1 real-Garmin smoke -----


@pytest.mark.asyncio
async def test_rhr_parses_real_garmin_shape() -> None:
    """Garmin nests RHR as allMetrics.metricsMap.WELLNESS_RESTING_HEART_RATE[].value."""
    payload = {
        "allMetrics": {
            "metricsMap": {
                "WELLNESS_RESTING_HEART_RATE": [{"value": 57.0, "calendarDate": "2026-05-09"}]
            }
        }
    }
    server_module.set_garmin_client_for_testing(FakeGarminClient({"get_rhr_day": payload}))
    result = await server_module.get_resting_heart_rate(days=1)
    assert result.days[0].rhr_bpm == 57
    assert result.avg_rhr_bpm == 57.0


@pytest.mark.asyncio
async def test_respiration_synthesises_avg_when_missing() -> None:
    """Real Garmin payloads often omit avgRespirationValue but have sleep+waking."""
    payload = {
        "calendarDate": "2026-05-09",
        "lowestRespirationValue": 9.0,
        "highestRespirationValue": 24.0,
        "avgWakingRespirationValue": 16.0,
        "avgSleepRespirationValue": 14.0,
    }
    server_module.set_garmin_client_for_testing(FakeGarminClient({"get_respiration_data": payload}))
    result = await server_module.get_respiration(date="2026-05-09")
    assert result.avg_breaths_per_min == pytest.approx(15.0)
    assert result.note is None


# ---------------------------------------------------------------------------
# get_daily_briefing — composite fusion tool
# ---------------------------------------------------------------------------

HRV_RAW: dict[str, Any] = {
    "hrvSummary": {
        "calendarDate": "2026-05-10",
        "lastNightAvg": 55.0,
        "weeklyAvg": 54.0,
        "status": "BALANCED",
        "baseline": {"lowUpper": 40.0, "balancedUpper": 70.0},
        "feedbackPhrase": "Your HRV is balanced.",
    }
}

BODY_BATTERY_RAW: list[dict[str, Any]] = [
    {
        "date": "2026-05-10",
        "bodyBatteryValuesArray": [
            [1715300000000, "CHARGED", 60],
            [1715303600000, "DRAINED", 72],
        ],
    }
]

# Real Garmin shape: the load numbers are nested per-device under
# mostRecentTrainingStatus.latestTrainingStatusData, NOT at the top level.
TRAINING_STATUS_RAW: dict[str, Any] = {
    "mostRecentTrainingStatus": {
        "latestTrainingStatusData": {
            "3626073413": {
                "trainingStatusFeedbackPhrase": "PRODUCTIVE_1",
                "weeklyTrainingLoad": 410,
                "primaryTrainingDevice": True,
                "acuteTrainingLoadDTO": {
                    "dailyTrainingLoadAcute": 300,
                    "dailyTrainingLoadChronic": 280,
                    "acwrPercent": 107,
                    "acwrStatus": "OPTIMAL",
                },
            }
        }
    },
}


def _rhr_ramp() -> Any:
    """A get_rhr_day double returning 50, 51, 52, ... across the 7-day loop.

    The briefing's RHR section is fetched oldest-day-first, so the final
    (newest) reading is 56 against a prior-day baseline of 52.5 → +3.5 bpm.
    """
    counter = {"n": 0}

    def _respond(_date: str) -> dict[str, Any]:
        value = 50 + counter["n"]
        counter["n"] += 1
        return {
            "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": value}]}}
        }

    return _respond


def _briefing_responses() -> dict[str, Any]:
    return {
        "get_sleep_data": {
            "dailySleepDTO": {
                "calendarDate": "2026-05-10",
                "sleepScores": {"overall": {"value": 80}},
            }
        },
        "get_hrv_data": HRV_RAW,
        "get_body_battery": BODY_BATTERY_RAW,
        "get_training_readiness": READINESS_PAYLOAD,
        "get_training_status": TRAINING_STATUS_RAW,
        "get_rhr_day": _rhr_ramp(),
    }


@pytest.mark.asyncio
async def test_daily_briefing_fuses_all_sections() -> None:
    server_module.set_garmin_client_for_testing(FakeGarminClient(_briefing_responses()))
    result = await server_module.get_daily_briefing(date="2026-05-10")

    assert isinstance(result, DailyBriefing)
    assert result.date == "2026-05-10"
    # Every section resolved, none dropped out.
    assert result.sleep is not None
    assert result.hrv is not None and result.hrv.status == "BALANCED"
    assert result.body_battery is not None
    assert result.training_readiness is not None and result.training_readiness.score == 78
    assert result.training_load is not None
    assert result.resting_heart_rate is not None
    assert result.sections_unavailable == []
    # Newest RHR (56) vs the trailing average of the prior six days (52.5).
    assert result.rhr_vs_baseline_bpm == pytest.approx(3.5)


@pytest.mark.asyncio
async def test_daily_briefing_degrades_when_a_section_fails() -> None:
    # Omit get_body_battery so the fake client raises for it; the briefing must
    # null that one section and carry on rather than failing wholesale.
    responses = _briefing_responses()
    del responses["get_body_battery"]
    server_module.set_garmin_client_for_testing(FakeGarminClient(responses))

    result = await server_module.get_daily_briefing(date="2026-05-10")
    assert result.body_battery is None
    assert "body_battery" in result.sections_unavailable
    # The other sections are unaffected.
    assert result.sleep is not None
    assert result.training_readiness is not None
