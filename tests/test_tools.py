"""Tests for tool handlers with a fake Garmin client."""

from __future__ import annotations

from typing import Any

import pytest

from garmin_mcp import server as server_module
from garmin_mcp.garmin_client import GarminClient
from garmin_mcp.models import SleepSummary

SLEEP_PAYLOAD: dict[str, Any] = {
    "dailySleepDTO": {
        "calendarDate": "2026-05-07",
        "sleepTimeSeconds": 28800,
        "deepSleepSeconds": 5400,
        "lightSleepSeconds": 14400,
        "remSleepSeconds": 7200,
        "awakeSleepSeconds": 1800,
        "averageRespirationValue": 14.2,
        "averageSpO2Value": 96.0,
        "sleepScores": {
            "overall": {"value": 82, "qualifierKey": "GOOD"},
        },
    },
    "hrvSummary": {
        "lastNightAvg": 48.0,
        "weeklyAvg": 50.0,
    },
}


ACTIVITIES_PAYLOAD: list[dict[str, Any]] = [
    {
        "activityId": 12345,
        "activityName": "Morning Run",
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-05-07 06:30:00",
        "duration": 2400.0,
        "distance": 6500.0,
        "averageHR": 152.0,
        "maxHR": 178.0,
        "calories": 480.0,
    }
]


class FakeGarminClient(GarminClient):
    """Test double that bypasses login and returns canned payloads."""

    def __init__(self, responses: dict[str, Any]) -> None:
        # Deliberately skip super().__init__ so we don't need credentials.
        self._responses = responses
        self.call_log: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        self.call_log.append((method_name, args, kwargs))
        if method_name not in self._responses:
            raise AssertionError(f"Unexpected Garmin call: {method_name}")
        value = self._responses[method_name]
        if callable(value):
            return value(*args, **kwargs)
        return value


@pytest.fixture(autouse=True)
async def _reset_state() -> Any:
    """Each test gets a fresh cache and fake client slot."""
    cache = server_module.get_cache_for_testing()
    await cache.clear()
    server_module.set_garmin_client_for_testing(None)
    yield
    server_module.set_garmin_client_for_testing(None)


@pytest.mark.asyncio
async def test_get_sleep_parses_full_payload() -> None:
    fake = FakeGarminClient({"get_sleep_data": SLEEP_PAYLOAD})
    server_module.set_garmin_client_for_testing(fake)

    result = await server_module.get_sleep(date="2026-05-07")
    assert isinstance(result, SleepSummary)
    assert result.date == "2026-05-07"
    assert result.sleep_score == 82
    assert result.sleep_quality == "GOOD"
    assert result.total_sleep_seconds == 28800
    assert result.deep_sleep_seconds == 5400
    assert result.avg_overnight_hrv_ms == 48.0
    assert result.note is None


@pytest.mark.asyncio
async def test_get_sleep_handles_missing_data() -> None:
    fake = FakeGarminClient({"get_sleep_data": {}})
    server_module.set_garmin_client_for_testing(fake)

    result = await server_module.get_sleep(date="2026-05-07")
    assert result.note is not None
    assert result.total_sleep_seconds is None


@pytest.mark.asyncio
async def test_get_sleep_uses_cache() -> None:
    fake = FakeGarminClient({"get_sleep_data": SLEEP_PAYLOAD})
    server_module.set_garmin_client_for_testing(fake)

    await server_module.get_sleep(date="2026-05-07")
    await server_module.get_sleep(date="2026-05-07")
    assert len(fake.call_log) == 1


@pytest.mark.asyncio
async def test_get_recent_activities_caps_limit() -> None:
    fake = FakeGarminClient({"get_activities": ACTIVITIES_PAYLOAD})
    server_module.set_garmin_client_for_testing(fake)

    await server_module.get_recent_activities(limit=500)
    method, args, _ = fake.call_log[0]
    assert method == "get_activities"
    assert args == (0, 50)


@pytest.mark.asyncio
async def test_get_recent_activities_parses_payload() -> None:
    fake = FakeGarminClient({"get_activities": ACTIVITIES_PAYLOAD})
    server_module.set_garmin_client_for_testing(fake)

    result = await server_module.get_recent_activities(limit=5)
    assert result.count == 1
    activity = result.activities[0]
    assert activity.activity_id == "12345"
    assert activity.name == "Morning Run"
    assert activity.activity_type == "running"
    assert activity.distance_meters == 6500.0
    assert activity.avg_heart_rate == 152.0


@pytest.mark.asyncio
async def test_invalid_date_raises() -> None:
    fake = FakeGarminClient({"get_sleep_data": SLEEP_PAYLOAD})
    server_module.set_garmin_client_for_testing(fake)

    with pytest.raises(ValueError, match="Invalid date"):
        await server_module.get_sleep(date="not-a-date")
