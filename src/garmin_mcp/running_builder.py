"""Build Garmin running-workout JSON from the structured input models.

Like ``strength_builder``, this hand-builds the payload that
``upload_workout(dict)`` POSTs to ``/workout-service/workout``. The enum ids
follow the same numbering captured from real workouts (see
``docs/strength_workout_schema.md``): step types 1..8, end conditions
time=2 / distance=3 / iterations=7, and the repeat group's ``stepOrder``
assigned BEFORE its children.

The semantic hard parts live here so they are deterministic and testable:
pace is entered as human 'M:SS' per km but Garmin stores speed in m/s
(``targetValueOne`` = slower bound = lower m/s, ``targetValueTwo`` = faster
bound = higher m/s), and a step ends on exactly one of time or distance.

Tool design adapted from github.com/Tyler-Irving/garmin-mcp/pull/10 by
David Reina Garcia.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import RunningRepeatInput, RunningStepInput, RunningWorkoutInput

# --- Garmin enum constants ----------------------------------------------------
_SPORT_RUNNING = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}

_STEP_TYPES: dict[str, dict[str, Any]] = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
    "active": {"stepTypeId": 8, "stepTypeKey": "main", "displayOrder": 8},
}
_STEP_REPEAT = {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6}

_END_TIME = {
    "conditionTypeId": 2,
    "conditionTypeKey": "time",
    "displayOrder": 2,
    "displayable": True,
}
_END_DISTANCE = {
    "conditionTypeId": 3,
    "conditionTypeKey": "distance",
    "displayOrder": 3,
    "displayable": True,
}
_END_ITER = {
    "conditionTypeId": 7,
    "conditionTypeKey": "iterations",
    "displayOrder": 7,
    "displayable": False,
}

_TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
_TARGET_PACE = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}

# Sanity bounds for pace warnings (not errors): outside 2:00-15:00 min/km is
# almost certainly a typo (e.g. '3:5' parsed as 3:05 when 3:50 was meant).
_PACE_FAST_BOUND_S = 120
_PACE_SLOW_BOUND_S = 900


def parse_pace(pace: str) -> float:
    """Convert a 'M:SS' per-km pace to speed in m/s ('3:35' -> 1000/215)."""
    parts = pace.strip().split(":")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        raise ValueError(f"Invalid pace {pace!r}: expected 'M:SS' per km, e.g. '4:30'.")
    seconds_per_km = int(parts[0]) * 60 + int(parts[1])
    if int(parts[1]) > 59:
        raise ValueError(f"Invalid pace {pace!r}: seconds must be 00-59.")
    if seconds_per_km <= 0:
        raise ValueError(f"Invalid pace {pace!r}: pace must be positive.")
    return 1000.0 / seconds_per_km


def _pace_bounds(step: RunningStepInput) -> tuple[float, float] | None:
    """Validated (slower m/s, faster m/s) target bounds, or None for no target."""
    if step.pace_min_per_km is None and step.pace_max_per_km is None:
        return None
    if step.pace_min_per_km is None or step.pace_max_per_km is None:
        raise ValueError(
            f"Step '{step.type}': provide both pace_min_per_km and pace_max_per_km, or neither."
        )
    faster = parse_pace(step.pace_min_per_km)
    slower = parse_pace(step.pace_max_per_km)
    if slower > faster:
        raise ValueError(
            f"Step '{step.type}': pace_min_per_km ({step.pace_min_per_km}) must be the "
            f"faster (smaller) pace and pace_max_per_km ({step.pace_max_per_km}) the "
            "slower one — they look swapped."
        )
    return slower, faster


@dataclass
class _Counter:
    value: int = 0

    def next(self) -> int:
        self.value += 1
        return self.value


def _executable_step(step: RunningStepInput, order: int) -> dict[str, Any]:
    type_key = step.type.strip().lower()
    step_type = _STEP_TYPES.get(type_key)
    if step_type is None:
        raise ValueError(
            f"Unknown step type {step.type!r}. Valid types: {', '.join(sorted(_STEP_TYPES))}."
        )

    if step.duration_seconds is not None and step.distance_meters is not None:
        raise ValueError(
            f"Step '{step.type}': specify either duration_seconds or distance_meters, not both."
        )
    if step.duration_seconds is None and step.distance_meters is None:
        raise ValueError(
            f"Step '{step.type}': specify duration_seconds or distance_meters as the end condition."
        )
    if step.duration_seconds is not None and step.duration_seconds <= 0:
        raise ValueError(f"Step '{step.type}': duration_seconds must be positive.")
    if step.distance_meters is not None and step.distance_meters <= 0:
        raise ValueError(f"Step '{step.type}': distance_meters must be positive.")

    if step.distance_meters is not None:
        end_condition, end_value = _END_DISTANCE, float(step.distance_meters)
    else:
        end_condition, end_value = _END_TIME, float(step.duration_seconds or 0)

    out: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": step_type,
        "endCondition": end_condition,
        "endConditionValue": end_value,
        "targetType": _TARGET_NONE,
    }
    bounds = _pace_bounds(step)
    if bounds is not None:
        slower, faster = bounds
        out["targetType"] = _TARGET_PACE
        out["targetValueOne"] = slower
        out["targetValueTwo"] = faster
    if step.note:
        out["description"] = step.note
    return out


def build_running_workout(workout: RunningWorkoutInput) -> dict[str, Any]:
    """Build the full ``upload_workout`` payload; raises ValueError on bad input."""
    if not workout.steps:
        raise ValueError("workout has no steps")

    counter = _Counter()
    steps: list[dict[str, Any]] = []
    for item in workout.steps:
        if isinstance(item, RunningRepeatInput):
            if not item.steps:
                raise ValueError("a repeat group needs at least 1 step")
            group_order = counter.next()
            children = [_executable_step(s, counter.next()) for s in item.steps]
            steps.append(
                {
                    "type": "RepeatGroupDTO",
                    "stepOrder": group_order,
                    "stepType": _STEP_REPEAT,
                    "numberOfIterations": item.iterations,
                    "endCondition": _END_ITER,
                    "endConditionValue": float(item.iterations),
                    "smartRepeat": False,
                    "workoutSteps": children,
                }
            )
        else:
            steps.append(_executable_step(item, counter.next()))

    payload: dict[str, Any] = {
        "workoutName": workout.name,
        "sportType": _SPORT_RUNNING,
        "estimatedDurationInSecs": estimate_duration_seconds(workout),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": _SPORT_RUNNING,
                "workoutSteps": steps,
            }
        ],
    }
    if workout.description:
        payload["description"] = workout.description
    return payload


def _step_duration_estimate(step: RunningStepInput) -> int:
    if step.duration_seconds is not None:
        return step.duration_seconds
    if step.distance_meters is not None:
        bounds = _pace_bounds(step)
        if bounds is not None:
            slower, faster = bounds
            return int(step.distance_meters / ((slower + faster) / 2.0))
    return 0


def estimate_duration_seconds(workout: RunningWorkoutInput) -> int:
    """Total seconds from time steps plus distance steps that carry a pace target.

    Distance steps without a pace target contribute 0 (we cannot know the pace).
    """
    total = 0
    for item in workout.steps:
        if isinstance(item, RunningRepeatInput):
            total += sum(_step_duration_estimate(s) for s in item.steps) * item.iterations
        else:
            total += _step_duration_estimate(item)
    return total


def pace_warnings(workout: RunningWorkoutInput) -> list[str]:
    """Non-fatal sanity warnings for the preview (implausible pace bounds)."""
    warnings: list[str] = []
    for step in _iter_steps(workout):
        for pace in (step.pace_min_per_km, step.pace_max_per_km):
            if pace is None:
                continue
            seconds_per_km = round(1000.0 / parse_pace(pace))
            if not _PACE_FAST_BOUND_S <= seconds_per_km <= _PACE_SLOW_BOUND_S:
                warnings.append(
                    f"Pace '{pace}'/km on a '{step.type}' step is outside 2:00-15:00 "
                    "per km — double-check it is not a typo."
                )
    return warnings


def _iter_steps(workout: RunningWorkoutInput) -> list[RunningStepInput]:
    flat: list[RunningStepInput] = []
    for item in workout.steps:
        if isinstance(item, RunningRepeatInput):
            flat.extend(item.steps)
        else:
            flat.append(item)
    return flat


def _fmt_seconds(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


def _fmt_step(step: RunningStepInput) -> str:
    if step.distance_meters is not None:
        if step.distance_meters >= 1000 and step.distance_meters % 100 == 0:
            end = f"{step.distance_meters / 1000:g} km"
        else:
            end = f"{step.distance_meters:g} m"
    else:
        end = _fmt_seconds(step.duration_seconds or 0)
    text = f"{step.type.strip().lower()} {end}"
    if step.pace_min_per_km and step.pace_max_per_km:
        text += f" @ {step.pace_min_per_km}-{step.pace_max_per_km}/km"
    return text


def summarize(workout: RunningWorkoutInput) -> str:
    """Human-readable step-by-step summary for the preview."""
    lines: list[str] = []
    for i, item in enumerate(workout.steps, start=1):
        if isinstance(item, RunningRepeatInput):
            inner = " + ".join(_fmt_step(s) for s in item.steps)
            lines.append(f"{i}. {item.iterations}x [{inner}]")
        else:
            lines.append(f"{i}. {_fmt_step(item)}")
    estimate = estimate_duration_seconds(workout)
    if estimate:
        lines.append(f"~{_fmt_seconds(estimate)} total (distance steps without a pace excluded)")
    return "\n".join(lines)


__all__ = [
    "build_running_workout",
    "estimate_duration_seconds",
    "pace_warnings",
    "parse_pace",
    "summarize",
]
