"""Tests for the strength workout builder and exercise resolver.

The builder's payload shape is asserted against the schema captured from real
Garmin workouts (see docs/strength_workout_schema.md): repeat group =
numberOfIterations sets, working set = interval step with reps end condition
(id 10), rest = lap.button (id 1), iterations end condition id 7, sport id 5.
"""

from __future__ import annotations

import asyncio

import pytest

from garmin_mcp.exercise_resolver import resolve_exercise
from garmin_mcp.garmin_client import GarminAuthError, GarminClient, GarminClientError
from garmin_mcp.strength_builder import (
    BlockSpec,
    SetSpec,
    StrengthWorkoutSpec,
    build_strength_workout,
)


def _steps(payload: dict) -> list[dict]:
    return payload["workoutSegments"][0]["workoutSteps"]


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "query,category,exercise_name",
    [
        # Bench Press must resolve to a SPECIFIC leaf, not the bare BENCH_PRESS
        # category (which Garmin blanks on upload).
        ("Bench Press", "BENCH_PRESS", "BARBELL_BENCH_PRESS"),
        ("Lat Pulldown", "PULL_UP", "LAT_PULLDOWN"),  # the alias-bug regression
        ("Tricep Pushdown", "TRICEPS_EXTENSION", "TRICEPS_PRESSDOWN"),
        ("Romanian Deadlift", "DEADLIFT", "ROMANIAN_DEADLIFT"),
        ("Seated Machine Overhead Press", "SHOULDER_PRESS", "SMITH_MACHINE_OVERHEAD_PRESS"),
    ],
)
def test_resolver_known_exercises(query: str, category: str, exercise_name: str) -> None:
    r = resolve_exercise(query)
    assert r.category == category
    assert r.exercise_name == exercise_name
    assert r.is_usable


def test_resolver_avoids_bare_category_name() -> None:
    # Garmin blanks bare category-name exercises (BENCH_PRESS, LEG_RAISE...);
    # the resolver must pick a specific leaf instead.
    for q in ["Bench Press", "Captains Chair Leg Raise", "Lateral Raise"]:
        r = resolve_exercise(q)
        assert r.exercise_name != r.category, f"{q} resolved to bare category {r.category}"


def test_resolver_override() -> None:
    r = resolve_exercise("Rear Delt Fly")
    assert (r.category, r.exercise_name) == ("LATERAL_RAISE", "BENT_OVER_LATERAL_RAISE")


def test_resolver_exact_name_beats_wrong_variant() -> None:
    # "Leg Extension" must map to LEG_EXTENSION, not the higher-scoring but
    # semantically wrong BRIDGE_WITH_LEG_EXTENSION (a glute bridge).
    r = resolve_exercise("Leg Extension")
    assert r.exercise_name == "LEG_EXTENSION"
    assert r.confidence == "exact"


def test_resolver_unmappable_is_flagged_not_guessed() -> None:
    # Pallof / anti-rotation has no honest Garmin equivalent: must not be
    # silently mapped with high confidence.
    r = resolve_exercise("Pallof Press Iso-Hold")
    assert not r.is_usable


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


def test_sport_type_is_strength() -> None:
    spec = StrengthWorkoutSpec(
        name="t",
        blocks=[
            BlockSpec(sets=3, exercises=[SetSpec("BENCH_PRESS", "BARBELL_BENCH_PRESS", reps=8)])
        ],
        include_warmup=False,
    )
    payload = build_strength_workout(spec)
    assert payload["sportType"]["sportTypeId"] == 5
    assert payload["sportType"]["sportTypeKey"] == "strength_training"


def test_multiset_builds_repeat_group_with_reps_and_rest() -> None:
    spec = StrengthWorkoutSpec(
        name="t",
        blocks=[
            BlockSpec(sets=4, exercises=[SetSpec("BENCH_PRESS", "BARBELL_BENCH_PRESS", reps=6)])
        ],
        include_warmup=False,
    )
    group = _steps(build_strength_workout(spec))[0]
    assert group["type"] == "RepeatGroupDTO"
    assert group["numberOfIterations"] == 4
    assert group["endCondition"]["conditionTypeId"] == 7  # iterations
    assert group["endConditionValue"] == 4.0

    work, rest = group["workoutSteps"]
    assert work["stepType"]["stepTypeKey"] == "interval"
    assert work["endCondition"]["conditionTypeId"] == 10  # reps
    assert work["endConditionValue"] == 6.0
    assert work["exerciseName"] == "BARBELL_BENCH_PRESS"
    assert work["category"] == "BENCH_PRESS"
    assert rest["stepType"]["stepTypeKey"] == "rest"
    assert rest["endCondition"]["conditionTypeId"] == 1  # lap.button


def test_superset_is_two_intervals_in_one_group() -> None:
    spec = StrengthWorkoutSpec(
        name="t",
        blocks=[
            BlockSpec(
                sets=3,
                exercises=[
                    SetSpec("TRICEPS_EXTENSION", "TRICEPS_PRESSDOWN", reps=12),
                    SetSpec("CURL", "DUMBBELL_HAMMER_CURL", reps=12),
                ],
            )
        ],
        include_warmup=False,
    )
    group = _steps(build_strength_workout(spec))[0]
    kinds = [s["stepType"]["stepTypeKey"] for s in group["workoutSteps"]]
    assert kinds == ["interval", "interval", "rest"]


def test_step_order_is_monotonic_across_nesting() -> None:
    spec = StrengthWorkoutSpec(
        name="t",
        blocks=[
            BlockSpec(sets=2, exercises=[SetSpec("BENCH_PRESS", "BARBELL_BENCH_PRESS", reps=8)]),
            BlockSpec(sets=2, exercises=[SetSpec("ROW", "SEATED_CABLE_ROW", reps=10)]),
        ],
    )
    payload = build_strength_workout(spec)
    orders: list[int] = []

    def walk(steps: list[dict]) -> None:
        for s in steps:
            orders.append(s["stepOrder"])
            if s.get("type") == "RepeatGroupDTO":
                walk(s["workoutSteps"])

    walk(_steps(payload))
    assert orders == sorted(orders)
    assert len(orders) == len(set(orders))  # no duplicates


def test_weight_omitted_when_absent_included_when_present() -> None:
    spec = StrengthWorkoutSpec(
        name="t",
        blocks=[
            BlockSpec(sets=3, exercises=[SetSpec("BENCH_PRESS", "BARBELL_BENCH_PRESS", reps=8)]),
            BlockSpec(
                sets=3,
                exercises=[SetSpec("CURL", "DUMBBELL_HAMMER_CURL", reps=10, weight=45.0)],
            ),
        ],
        include_warmup=False,
    )
    steps = _steps(build_strength_workout(spec))
    bench = steps[0]["workoutSteps"][0]
    curl = steps[1]["workoutSteps"][0]
    assert "weightValue" not in bench
    assert curl["weightValue"] == 45.0
    assert curl["weightUnit"]["unitKey"] == "pound"


def test_seconds_uses_time_end_condition() -> None:
    spec = StrengthWorkoutSpec(
        name="t",
        blocks=[BlockSpec(sets=2, exercises=[SetSpec("PLANK", "SIDE_PLANK", seconds=30)])],
        include_warmup=False,
    )
    work = _steps(build_strength_workout(spec))[0]["workoutSteps"][0]
    assert work["endCondition"]["conditionTypeId"] == 2  # time
    assert work["endConditionValue"] == 30.0


def test_set_without_reps_or_seconds_raises() -> None:
    spec = StrengthWorkoutSpec(
        name="t",
        blocks=[BlockSpec(sets=2, exercises=[SetSpec("BENCH_PRESS", "BARBELL_BENCH_PRESS")])],
    )
    with pytest.raises(ValueError, match="reps or seconds"):
        build_strength_workout(spec)


def test_warmup_step_added_by_default() -> None:
    spec = StrengthWorkoutSpec(
        name="t",
        blocks=[
            BlockSpec(sets=3, exercises=[SetSpec("BENCH_PRESS", "BARBELL_BENCH_PRESS", reps=8)])
        ],
    )
    first = _steps(build_strength_workout(spec))[0]
    assert first["stepType"]["stepTypeKey"] == "warmup"
    assert first["endCondition"]["conditionTypeId"] == 1  # lap.button


def test_note_becomes_step_description() -> None:
    spec = StrengthWorkoutSpec(
        name="t",
        blocks=[
            BlockSpec(
                sets=2,
                exercises=[
                    SetSpec("PLANK", "PLANK", seconds=30, note="Pallof Press (anti-rotation)")
                ],
            )
        ],
        include_warmup=False,
    )
    work = _steps(build_strength_workout(spec))[0]["workoutSteps"][0]
    assert work["description"] == "Pallof Press (anti-rotation)"


# --------------------------------------------------------------------------- #
# Write-gate / allowlist (guard short-circuits before any network call)
# --------------------------------------------------------------------------- #


def test_client_rejects_non_allowlisted_method() -> None:
    client = GarminClient(email="", password="", token_dir="/tmp/x")
    with pytest.raises(GarminClientError, match="allowlist"):
        asyncio.run(client.call("delete_workout", 123))


def test_client_blocks_writes_when_disabled() -> None:
    client = GarminClient(email="", password="", token_dir="/tmp/x", allow_writes=False)
    with pytest.raises(GarminAuthError, match="writes are disabled"):
        asyncio.run(client.call("upload_workout", {}))
