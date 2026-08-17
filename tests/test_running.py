"""Tests for the running workout builder and MCP tools (preview / create /
schedule / unschedule).

Builder assertions follow the payload conventions verified against real Garmin
workouts in strength_builder: repeat group stepOrder BEFORE its children,
iterations end condition id 7, time id 2 / distance id 3, speeds in m/s with
targetValueOne = slower bound.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from garmin_mcp import server
from garmin_mcp.models import RunningRepeatInput, RunningStepInput, RunningWorkoutInput
from garmin_mcp.running_builder import (
    build_running_workout,
    estimate_duration_seconds,
    pace_warnings,
    parse_pace,
)

# --------------------------------------------------------------------------- #
# Pace parsing
# --------------------------------------------------------------------------- #


def test_parse_pace_math() -> None:
    assert parse_pace("3:35") == pytest.approx(1000.0 / 215.0)
    assert parse_pace("5:00") == pytest.approx(1000.0 / 300.0)


@pytest.mark.parametrize("bad", ["", "4", "4:5:6", "4:xx", "-4:30", "0:00", "4:75"])
def test_parse_pace_rejects_bad_formats(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_pace(bad)


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


def _intervals_workout() -> RunningWorkoutInput:
    return RunningWorkoutInput(
        name="Intervals 5x1km",
        steps=[
            RunningStepInput(type="warmup", duration_seconds=600),
            RunningRepeatInput(
                iterations=5,
                steps=[
                    RunningStepInput(
                        type="interval",
                        distance_meters=1000,
                        pace_min_per_km="3:35",
                        pace_max_per_km="3:40",
                    ),
                    RunningStepInput(type="recovery", duration_seconds=90),
                ],
            ),
            RunningStepInput(type="cooldown", duration_seconds=600),
        ],
    )


def _steps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["workoutSegments"][0]["workoutSteps"]


def test_payload_shape_and_sport() -> None:
    payload = build_running_workout(_intervals_workout())
    assert payload["workoutName"] == "Intervals 5x1km"
    assert payload["sportType"]["sportTypeId"] == 1
    assert payload["sportType"]["sportTypeKey"] == "running"
    warmup, group, cooldown = _steps(payload)
    assert warmup["stepType"]["stepTypeKey"] == "warmup"
    assert warmup["endCondition"]["conditionTypeId"] == 2  # time
    assert warmup["endConditionValue"] == 600.0
    assert group["type"] == "RepeatGroupDTO"
    assert group["numberOfIterations"] == 5
    assert group["endCondition"]["conditionTypeId"] == 7  # iterations
    assert cooldown["stepType"]["stepTypeKey"] == "cooldown"


def test_interval_distance_end_condition_and_pace_target() -> None:
    payload = build_running_workout(_intervals_workout())
    interval = _steps(payload)[1]["workoutSteps"][0]
    assert interval["endCondition"]["conditionTypeId"] == 3  # distance, NOT time
    assert interval["endConditionValue"] == 1000.0
    assert interval["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    # m/s bounds: ValueOne = slower (3:40), ValueTwo = faster (3:35)
    assert interval["targetValueOne"] == pytest.approx(1000.0 / 220.0)
    assert interval["targetValueTwo"] == pytest.approx(1000.0 / 215.0)
    assert interval["targetValueOne"] < interval["targetValueTwo"]


def test_step_without_pace_has_no_target() -> None:
    payload = build_running_workout(_intervals_workout())
    warmup = _steps(payload)[0]
    assert warmup["targetType"]["workoutTargetTypeKey"] == "no.target"
    assert "targetValueOne" not in warmup


def test_repeat_group_order_precedes_children() -> None:
    # The field-verified convention (strength_builder / captured workouts):
    # the group takes the next order, THEN its children.
    payload = build_running_workout(_intervals_workout())
    group = _steps(payload)[1]
    child_orders = [s["stepOrder"] for s in group["workoutSteps"]]
    assert group["stepOrder"] < min(child_orders)

    orders: list[int] = []

    def walk(steps: list[dict[str, Any]]) -> None:
        for s in steps:
            orders.append(s["stepOrder"])
            if s.get("type") == "RepeatGroupDTO":
                walk(s["workoutSteps"])

    walk(_steps(payload))
    assert orders == sorted(orders)
    assert len(orders) == len(set(orders))


def test_estimated_duration_includes_paced_distance_steps() -> None:
    # warmup 600 + cooldown 600 + 5 * (recovery 90 + 1km at ~217.5 s/km)
    estimate = estimate_duration_seconds(_intervals_workout())
    assert estimate == 600 + 600 + 5 * (90 + int(1000 / ((1000 / 220 + 1000 / 215) / 2)))


def test_both_end_conditions_rejected() -> None:
    step = RunningStepInput(type="interval", duration_seconds=60, distance_meters=400)
    with pytest.raises(ValueError, match="not both"):
        build_running_workout(RunningWorkoutInput(name="x", steps=[step]))


def test_missing_end_condition_rejected() -> None:
    with pytest.raises(ValueError, match="end condition"):
        build_running_workout(
            RunningWorkoutInput(name="x", steps=[RunningStepInput(type="interval")])
        )


def test_unknown_step_type_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown step type"):
        build_running_workout(
            RunningWorkoutInput(
                name="x", steps=[RunningStepInput(type="sprint", duration_seconds=60)]
            )
        )


def test_lone_pace_bound_rejected() -> None:
    step = RunningStepInput(type="interval", distance_meters=400, pace_min_per_km="4:00")
    with pytest.raises(ValueError, match="both"):
        build_running_workout(RunningWorkoutInput(name="x", steps=[step]))


def test_swapped_pace_bounds_rejected() -> None:
    step = RunningStepInput(
        type="interval", distance_meters=400, pace_min_per_km="4:30", pace_max_per_km="4:00"
    )
    with pytest.raises(ValueError, match="swapped"):
        build_running_workout(RunningWorkoutInput(name="x", steps=[step]))


def test_empty_repeat_group_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1 step"):
        build_running_workout(
            RunningWorkoutInput(name="x", steps=[RunningRepeatInput(iterations=3, steps=[])])
        )


def test_implausible_pace_warns_but_builds() -> None:
    workout = RunningWorkoutInput(
        name="x",
        steps=[
            RunningStepInput(
                type="interval", distance_meters=400, pace_min_per_km="1:30", pace_max_per_km="1:40"
            )
        ],
    )
    build_running_workout(workout)  # no raise
    warnings = pace_warnings(workout)
    assert len(warnings) == 2
    assert "typo" in warnings[0]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((method, args))
        if method == "upload_workout":
            return {"workoutId": 777}
        if method == "schedule_workout":
            return {
                "workoutScheduleId": 555,
                "workout": {"workoutName": "Intervals 5x1km"},
                "calendarDate": args[1],
            }
        if method == "unschedule_workout":
            return None
        raise AssertionError(f"unexpected method {method}")

    def called(self, method: str) -> bool:
        return any(m == method for m, _ in self.calls)


@pytest.fixture()
def fake() -> Any:
    client = _FakeClient()
    server.set_garmin_client_for_testing(client)
    yield client
    server.set_garmin_client_for_testing(None)


def test_preview_is_offline_and_tokenised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    server.set_garmin_client_for_testing(None)
    prev = asyncio.run(server.preview_running_workout(_intervals_workout()))
    assert prev.confirmation_token
    assert "5x [interval 1 km @ 3:35-3:40/km + recovery 1:30]" in prev.summary
    assert prev.estimated_duration_seconds > 0
    assert prev.warnings == []


def test_create_blocked_when_writes_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", False)
    with pytest.raises(ValueError, match="Writes are disabled"):
        asyncio.run(server.create_running_workout(_intervals_workout(), confirmation_token="x"))


def test_create_rejects_bad_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    with pytest.raises(ValueError, match="confirmation_token does not match"):
        asyncio.run(server.create_running_workout(_intervals_workout(), confirmation_token="no"))


def test_create_happy_path(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    prev = asyncio.run(server.preview_running_workout(_intervals_workout()))
    created = asyncio.run(
        server.create_running_workout(
            _intervals_workout(), confirmation_token=prev.confirmation_token
        )
    )
    assert created.workout_id == "777"
    assert "schedule_workout" in created.status
    assert fake.called("upload_workout")


def test_confirm_true_rejected_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "")
    monkeypatch.setattr(server, "_TRANSPORT", "http")
    with pytest.raises(ValueError, match="Cannot confirm"):
        asyncio.run(server.create_running_workout(_intervals_workout(), confirm=True))


def test_schedule_happy_path(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    result = asyncio.run(server.schedule_workout("777", "2026-08-20"))
    assert result.schedule_id == "555"
    assert result.workout_name == "Intervals 5x1km"
    assert result.calendar_date == "2026-08-20"
    assert ("schedule_workout", ("777", "2026-08-20")) in fake.calls


def test_schedule_blocked_when_writes_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", False)
    with pytest.raises(ValueError, match="Writes are disabled"):
        asyncio.run(server.schedule_workout("777", "2026-08-20"))


def test_schedule_rejects_bad_date(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    with pytest.raises(ValueError, match="Invalid date"):
        asyncio.run(server.schedule_workout("777", "20/08/2026"))
    assert not fake.called("schedule_workout")


def test_unschedule_happy_path(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    result = asyncio.run(server.unschedule_workout("555"))
    assert result.schedule_id == "555"
    assert "template is untouched" in result.status
    assert fake.called("unschedule_workout")
