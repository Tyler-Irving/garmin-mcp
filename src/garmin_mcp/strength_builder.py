"""Build Garmin strength-workout JSON from a structured spec.

The vendored ``garminconnect`` library has no strength model, so we hand-build
the payload that ``upload_workout(dict)`` POSTs to ``/workout-service/workout``.
Every magic number here is documented in ``docs/strength_workout_schema.md`` and
was captured from real workouts on the account owner's watch.

Design choices for v1 (an RPE / auto-regulated program with no prescribed loads):
* Weight is optional and **omitted** when absent — the lifter logs actual load
  per set on the watch (also dodges the known lb↔kg display bug).
* Reps use the native ``reps`` end condition (id 10) — confirmed working via JSON.
* "Sets" are a repeat group; a **superset** is >1 exercise inside one group.
* Rest ends on ``lap.button`` (user-controlled), matching the captured workouts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- Garmin enum constants (see docs/strength_workout_schema.md) --------------
_SPORT_STRENGTH = {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5}

_STEP_WARMUP = {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1}
_STEP_INTERVAL = {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3}
_STEP_REST = {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5}
_STEP_REPEAT = {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6}

_END_LAP = {
    "conditionTypeId": 1,
    "conditionTypeKey": "lap.button",
    "displayOrder": 1,
    "displayable": True,
}
_END_TIME = {
    "conditionTypeId": 2,
    "conditionTypeKey": "time",
    "displayOrder": 2,
    "displayable": True,
}
_END_ITER = {
    "conditionTypeId": 7,
    "conditionTypeKey": "iterations",
    "displayOrder": 7,
    "displayable": False,
}
_END_REPS = {
    "conditionTypeId": 10,
    "conditionTypeKey": "reps",
    "displayOrder": 10,
    "displayable": True,
}

_TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
_UNIT_POUND = {"unitId": 9, "unitKey": "pound", "factor": 453.59237}
_UNIT_KILOGRAM = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}


@dataclass
class SetSpec:
    """One exercise performed inside a block.

    ``reps`` uses the native reps end condition. If ``seconds`` is given instead
    (e.g. a 30s plank), a time end condition is used. ``weight``/``weight_unit``
    are optional — omitted from the payload when ``weight`` is None.
    """

    category: str
    exercise_name: str
    reps: int | None = None
    seconds: int | None = None
    weight: float | None = None
    weight_unit: str = "pound"  # "pound" | "kilogram"
    label: str | None = None  # human-readable, for the preview summary
    note: str | None = None  # -> step description; carries rep range/RPE and,
    # for imperfect matches, the original exercise name so the watch shows it.


@dataclass
class BlockSpec:
    """A repeat group: ``sets`` rounds of ``exercises`` (>1 = superset), then rest."""

    sets: int
    exercises: list[SetSpec]
    rest_after_set: bool = True  # add a lap.button rest step inside the group


@dataclass
class StrengthWorkoutSpec:
    name: str
    blocks: list[BlockSpec]
    include_warmup: bool = True
    notes: str | None = None  # written to the workout description


@dataclass
class _Counter:
    value: int = 0

    def next(self) -> int:
        self.value += 1
        return self.value


def _weight_unit_obj(unit: str) -> dict[str, Any]:
    return _UNIT_KILOGRAM if unit == "kilogram" else _UNIT_POUND


def _interval_step(s: SetSpec, order: int) -> dict[str, Any]:
    if s.reps is not None:
        end_cond, end_val = _END_REPS, float(s.reps)
    elif s.seconds is not None:
        end_cond, end_val = _END_TIME, float(s.seconds)
    else:
        raise ValueError(f"{s.exercise_name}: a set needs either reps or seconds")

    step: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": _STEP_INTERVAL,
        "endCondition": end_cond,
        "endConditionValue": end_val,
        "targetType": _TARGET_NONE,
        "category": s.category,
        "exerciseName": s.exercise_name,
    }
    if s.note:
        step["description"] = s.note
    if s.weight is not None:
        step["weightValue"] = float(s.weight)
        step["weightUnit"] = _weight_unit_obj(s.weight_unit)
    return step


def _rest_step(order: int) -> dict[str, Any]:
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": _STEP_REST,
        "endCondition": _END_LAP,
        "endConditionValue": 0.0,
        "targetType": _TARGET_NONE,
    }


def _warmup_step(order: int) -> dict[str, Any]:
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": _STEP_WARMUP,
        "endCondition": _END_LAP,
        "endConditionValue": 0.0,
        "targetType": _TARGET_NONE,
    }


def build_strength_workout(spec: StrengthWorkoutSpec) -> dict[str, Any]:
    """Return the Garmin Connect JSON payload for a strength workout."""
    if not spec.blocks:
        raise ValueError("workout has no blocks")

    counter = _Counter()
    steps: list[dict[str, Any]] = []

    if spec.include_warmup:
        steps.append(_warmup_step(counter.next()))

    for block in spec.blocks:
        if block.sets < 1:
            raise ValueError("a block needs at least 1 set")
        if not block.exercises:
            raise ValueError("a block needs at least 1 exercise")

        if block.sets == 1:
            # Single set: emit the exercise step(s) inline, no repeat wrapper.
            for s in block.exercises:
                steps.append(_interval_step(s, counter.next()))
            continue

        group_order = counter.next()
        children: list[dict[str, Any]] = [
            _interval_step(s, counter.next()) for s in block.exercises
        ]
        if block.rest_after_set:
            children.append(_rest_step(counter.next()))
        steps.append(
            {
                "type": "RepeatGroupDTO",
                "stepOrder": group_order,
                "stepType": _STEP_REPEAT,
                "numberOfIterations": block.sets,
                "endCondition": _END_ITER,
                "endConditionValue": float(block.sets),
                "smartRepeat": False,
                "skipLastRestStep": False,
                "workoutSteps": children,
            }
        )

    payload: dict[str, Any] = {
        "workoutName": spec.name,
        "sportType": _SPORT_STRENGTH,
        "estimatedDurationInSecs": 0,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": _SPORT_STRENGTH,
                "workoutSteps": steps,
            }
        ],
    }
    if spec.notes:
        payload["description"] = spec.notes
    return payload


def summarize(spec: StrengthWorkoutSpec) -> str:
    """Human-readable one-block-per-line summary for previews."""
    lines = [f"# {spec.name}"]
    if spec.include_warmup:
        lines.append("  warmup (press lap to start)")
    for block in spec.blocks:
        names = " + ".join(
            (s.label or s.exercise_name)
            + (f" {s.reps} reps" if s.reps is not None else f" {s.seconds}s")
            + (f" @ {s.weight:g}{s.weight_unit[:2]}" if s.weight is not None else "")
            for s in block.exercises
        )
        tag = "superset" if len(block.exercises) > 1 else "exercise"
        lines.append(f"  {block.sets}x {names}   ({tag})")
    if spec.notes:
        lines.append(f"  notes: {spec.notes}")
    return "\n".join(lines)
