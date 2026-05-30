"""Tests for the MCP strength write tools (preview / create).

All offline: preview makes no network call; create uses a fake Garmin client
injected via ``set_garmin_client_for_testing``. Exercises the safety spine —
write-flag gate, content-bound confirmation token, and post-upload blank check.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from garmin_mcp import server
from garmin_mcp.models import (
    StrengthBlockInput,
    StrengthExerciseInput,
    StrengthWorkoutInput,
)


def _workout() -> StrengthWorkoutInput:
    return StrengthWorkoutInput(
        name="Test Day",
        blocks=[
            StrengthBlockInput(
                sets=3,
                exercises=[StrengthExerciseInput(name="Bench Press", reps=8, note="6-8, RPE 8")],
            ),
            StrengthBlockInput(
                sets=3,
                exercises=[
                    StrengthExerciseInput(name="Tricep Pushdown", reps=12),
                    StrengthExerciseInput(name="Hammer Curl", reps=12),
                ],
            ),
        ],
    )


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(method)
        if method == "upload_workout":
            return {"workoutId": 424242}
        if method == "get_workout_by_id":
            return {
                "workoutSegments": [
                    {
                        "workoutSteps": [
                            {
                                "type": "RepeatGroupDTO",
                                "workoutSteps": [
                                    {
                                        "stepType": {"stepTypeKey": "interval"},
                                        "exerciseName": "BARBELL_BENCH_PRESS",
                                        "stepOrder": 3,
                                    },
                                    {"stepType": {"stepTypeKey": "rest"}, "stepOrder": 4},
                                ],
                            }
                        ]
                    }
                ]
            }
        raise AssertionError(f"unexpected method {method}")


def test_preview_is_offline_and_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    server.set_garmin_client_for_testing(None)
    prev = asyncio.run(server.preview_strength_workout(_workout()))
    assert prev.confirmation_token
    # Bench Press resolves to a specific leaf (not the bare category).
    bench = next(e for e in prev.exercises if e.query == "Bench Press")
    assert bench.exercise_name == "BARBELL_BENCH_PRESS"
    # The superset block contributed both exercises.
    assert {"Tricep Pushdown", "Hammer Curl"} <= {e.query for e in prev.exercises}


def test_create_blocked_when_writes_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", False)
    with pytest.raises(ValueError, match="Writes are disabled"):
        asyncio.run(server.create_strength_workout(_workout(), confirmation_token="x"))


def test_create_rejects_bad_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    with pytest.raises(ValueError, match="confirmation_token does not match"):
        asyncio.run(server.create_strength_workout(_workout(), confirmation_token="wrong"))


def test_create_happy_path_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    fake = _FakeClient()
    server.set_garmin_client_for_testing(fake)
    try:
        prev = asyncio.run(server.preview_strength_workout(_workout()))
        created = asyncio.run(
            server.create_strength_workout(_workout(), confirmation_token=prev.confirmation_token)
        )
    finally:
        server.set_garmin_client_for_testing(None)
    assert created.workout_id == "424242"
    assert created.verified is True
    assert created.blank_steps == []
    assert "Send to Device" in created.status
    assert "upload_workout" in fake.calls


def test_confirm_true_rejected_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    # The confirm=True dev bypass must never work over HTTP, even with no JWT
    # secret (which makes the content-bound token empty/vacuous).
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "")
    monkeypatch.setattr(server, "_TRANSPORT", "http")
    server.set_garmin_client_for_testing(None)
    with pytest.raises(ValueError, match="only on local stdio"):
        asyncio.run(server.create_strength_workout(_workout(), confirm=True))


def test_confirm_true_allowed_on_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same dev bypass IS allowed on local stdio (no JWT secret to bind).
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "")
    monkeypatch.setattr(server, "_TRANSPORT", "stdio")
    fake = _FakeClient()
    server.set_garmin_client_for_testing(fake)
    try:
        created = asyncio.run(server.create_strength_workout(_workout(), confirm=True))
    finally:
        server.set_garmin_client_for_testing(None)
    assert created.workout_id == "424242"
    assert "upload_workout" in fake.calls


def test_run_http_refuses_writes_without_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    # A write-enabled HTTP server with auth disabled must refuse to start.
    monkeypatch.setattr(server, "_TRANSPORT", "stdio")  # restored after the test
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "AUTH_ENABLED", False)
    with pytest.raises(SystemExit, match="Refusing to start"):
        server.run_http()


def test_exercise_name_override_alone_derives_category(monkeypatch: pytest.MonkeyPatch) -> None:
    # exercise_name on its own bypasses the resolver; the category is derived,
    # not silently discarded (which used to require BOTH fields).
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    server.set_garmin_client_for_testing(None)
    workout = StrengthWorkoutInput(
        name="t",
        blocks=[
            StrengthBlockInput(
                sets=1,
                exercises=[
                    StrengthExerciseInput(
                        name="My Special Curl", reps=10, exercise_name="WEIGHTED_LEG_CURL"
                    )
                ],
            )
        ],
    )
    prev = asyncio.run(server.preview_strength_workout(workout))
    info = prev.exercises[0]
    assert info.exercise_name == "WEIGHTED_LEG_CURL"
    assert info.category == "LEG_CURL"
    assert info.confidence == "explicit"


def test_category_without_exercise_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    server.set_garmin_client_for_testing(None)
    workout = StrengthWorkoutInput(
        name="t",
        blocks=[
            StrengthBlockInput(
                sets=1,
                exercises=[StrengthExerciseInput(name="x", reps=10, category="LEG_CURL")],
            )
        ],
    )
    with pytest.raises(ValueError, match="without 'exercise_name'"):
        asyncio.run(server.preview_strength_workout(workout))
