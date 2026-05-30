#!/usr/bin/env python3
"""Build (and optionally create) the 5-day program as Garmin strength workouts.

  python scripts/create_program.py            # PREVIEW only (no network)
  GARMIN_WRITE_ENABLED=1 python scripts/create_program.py --create

Preview prints each day's resolved exercises, notes, and any imperfect matches.
``--create`` uploads all 5 days serially (rate-limited), validates day 1 by
re-fetching it, and stops on the first error. Weights are omitted (the program
is RPE/auto-regulated — log load on the watch). Anti-rotation core work that has
no honest Garmin enum is built as a neutral hold with the real movement in the
step note (per the owner's decision).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from garmin_mcp.exercise_resolver import resolve_exercise  # noqa: E402
from garmin_mcp.paths import default_token_dir  # noqa: E402
from garmin_mcp.strength_builder import (  # noqa: E402
    BlockSpec,
    SetSpec,
    StrengthWorkoutSpec,
    build_strength_workout,
    summarize,
)

_TAXONOMY = json.loads(
    (
        Path(__file__).resolve().parent.parent / "src/garmin_mcp/data/exercise_taxonomy.json"
    ).read_text()
)
_VALID_NAMES = {ex for cat in _TAXONOMY["categories"].values() for ex in cat["exercises"]}


@dataclass
class Ex:
    """One programmed exercise (free text + prescription)."""

    name: str
    sets: int
    reps: int | None = None
    secs: int | None = None
    rng: str = ""
    rpe: str = ""
    force: tuple[str, str] | None = None  # (category, exercise_name) override


def ss(sets: int, *exs: Ex) -> list[Ex]:
    """Mark a superset: same set count, exercises share one repeat group."""
    for e in exs:
        e.sets = sets
    return list(exs)


# Anti-rotation / Pallof: no honest Garmin enum -> neutral hold + real name in note.
_HOLD = ("PLANK", "PLANK")

PROGRAM: list[tuple[str, list[list[Ex]]]] = [
    (
        "Upper A (chest)",
        [
            [Ex("Bench Press", 4, reps=6, rng="6-8 reps", rpe="7-8")],
            [Ex("Chest-Supported Row", 3, reps=8, rng="8-10 reps", rpe="7-8")],
            [Ex("Incline DB Press", 3, reps=8, rng="8-10 reps", rpe="7-8")],
            [Ex("Lat Pulldown", 3, reps=10, rng="10-12 reps", rpe="7-8")],
            ss(
                3,
                Ex("Tricep Pushdown", 3, reps=12, rng="12-15 reps", rpe="8-9"),
                Ex("Hammer Curl", 3, reps=12, rng="12-15 reps", rpe="8-9"),
            ),
            [Ex("Ab Wheel Rollout", 3, reps=8, rng="8-12 reps", rpe="8-9")],
            [Ex("Dead Bug", 2, reps=8, rng="8/side, slow tempo")],
        ],
    ),
    (
        "Lower A (quad)",
        [
            [Ex("Leg Press", 4, reps=6, rng="6-8 reps", rpe="7-8")],
            [Ex("Romanian Deadlift", 3, reps=8, rng="8-10 reps", rpe="7-8")],
            [Ex("Smith Machine Bulgarian Split Squat", 3, reps=10, rng="10-12/leg", rpe="7-8")],
            [Ex("Leg Extension", 3, reps=12, rng="12-15 reps", rpe="8-9")],
            [Ex("Standing Calf Raise", 3, reps=12, rng="12-15 reps", rpe="8-9")],
            [Ex("Suitcase Carry", 3, secs=40, rng="30-40 yd/side")],
            [Ex("Side Plank", 2, secs=30, rng="20-40s/side", rpe="8-9")],
        ],
    ),
    (
        "Upper B (back)",
        [
            [Ex("Cable Row", 4, reps=6, rng="6-8 reps", rpe="7-8")],
            [Ex("Seated Machine Overhead Press", 3, reps=8, rng="8-10 reps", rpe="7-8")],
            [Ex("Lat Pulldown", 3, reps=8, rng="8-10 reps, different grip", rpe="7-8")],
            [Ex("Incline DB Press", 3, reps=10, rng="10-12 reps", rpe="7-8")],
            ss(
                3,
                Ex("EZ Bar Curl", 3, reps=10, rng="10-12 reps", rpe="8-9"),
                Ex("Overhead Tricep Extension", 3, reps=10, rng="10-12 reps", rpe="8-9"),
            ),
            [Ex("Half-Kneeling Pallof Press", 3, secs=30, rng="8-10/side, 2s hold", force=_HOLD)],
            [Ex("Standing Cable Anti-Rotation Press", 2, secs=30, rng="10/side", force=_HOLD)],
        ],
    ),
    (
        "Lower B (posterior)",
        [
            [Ex("Trap Bar Deadlift", 4, reps=6, rng="6-8 reps", rpe="7-8")],
            [Ex("Hack Squat", 3, reps=10, rng="10-12 reps", rpe="7-8")],
            [Ex("Seated Leg Curl", 3, reps=10, rng="10-12 reps", rpe="7-8")],
            [Ex("Hip Thrust", 3, reps=10, rng="10-12 reps", rpe="7-8")],
            [Ex("Seated Calf Raise", 3, reps=12, rng="12-15 reps", rpe="8-9")],
            [Ex("Captains Chair Leg Raise", 3, reps=8, rng="8-15 reps", rpe="8-9")],
            [Ex("Reverse Crunch", 2, reps=12, rng="12-15 reps", rpe="8-9")],
        ],
    ),
    (
        "Arms/Shoulders/Core",
        [
            [Ex("Seated DB Shoulder Press", 4, reps=8, rng="8-10 reps", rpe="7-8")],
            ss(
                3,
                Ex("EZ Bar Curl", 3, reps=8, rng="8-10 reps", rpe="7-8"),
                Ex("Close-Grip Bench Press", 3, reps=8, rng="8-10 reps", rpe="7-8"),
            ),
            ss(
                3,
                Ex("Lateral Raise", 3, reps=12, rng="12-15 reps", rpe="8-9"),
                Ex("Tricep Rope Pushdown", 3, reps=12, rng="12-15 reps", rpe="8-9"),
            ),
            ss(
                3,
                Ex("Hammer Curl", 3, reps=10, rng="10-12 reps", rpe="8-9"),
                Ex("Overhead Tricep Extension", 3, reps=10, rng="10-12 reps", rpe="8-9"),
            ),
            [Ex("Rear Delt Fly", 3, reps=15, rng="15-20 reps", rpe="8-9")],
            [Ex("Cable Crunch", 3, reps=10, rng="10-12 reps", rpe="8-9")],
            [Ex("Pallof Press Iso-Hold", 2, secs=30, rng="30s/side", force=_HOLD)],
        ],
    ),
]


def _note(e: Ex, needs_name: bool) -> str | None:
    parts = ", ".join(p for p in (e.rng, f"RPE {e.rpe}" if e.rpe else "") if p)
    if needs_name:
        return f"{e.name}" + (f" — {parts}" if parts else "")
    return parts or None


def build_specs() -> tuple[list[StrengthWorkoutSpec], list[str]]:
    specs: list[StrengthWorkoutSpec] = []
    warnings: list[str] = []
    for day_name, blocks in PROGRAM:
        spec_blocks: list[BlockSpec] = []
        for group in blocks:
            set_specs: list[SetSpec] = []
            for e in group:
                if e.force is not None:
                    category, exercise_name = e.force
                    needs_name = True
                else:
                    r = resolve_exercise(e.name)
                    category = r.category or "UNKNOWN"
                    exercise_name = r.exercise_name or e.name.upper().replace(" ", "_")
                    needs_name = not r.is_usable
                    if needs_name:
                        warnings.append(
                            f"{day_name}: '{e.name}' -> {category}/{exercise_name} ({r.confidence})"
                        )
                if exercise_name not in _VALID_NAMES:
                    raise SystemExit(
                        f"INVALID enum {exercise_name!r} for '{e.name}' — fix before upload"
                    )
                set_specs.append(
                    SetSpec(
                        category=category,
                        exercise_name=exercise_name,
                        reps=e.reps,
                        seconds=e.secs,
                        label=e.name,
                        note=_note(e, needs_name),
                    )
                )
            spec_blocks.append(BlockSpec(sets=group[0].sets, exercises=set_specs))
        specs.append(StrengthWorkoutSpec(name=day_name, blocks=spec_blocks))
    return specs, warnings


def preview(specs: list[StrengthWorkoutSpec], warnings: list[str]) -> None:
    for spec in specs:
        print("\n" + summarize(spec))
        for block in spec.blocks:
            for s in block.exercises:
                if s.note and (s.label or "").lower() not in s.exercise_name.lower():
                    print(f"      note[{s.exercise_name}]: {s.note}")
    print(f"\n{'=' * 60}\n{len(specs)} workouts ready.")
    if warnings:
        print(f"{len(warnings)} imperfect matches (note carries the real name):")
        for w in warnings:
            print(f"  • {w}")


async def create(specs: list[StrengthWorkoutSpec]) -> None:
    from garmin_mcp.garmin_client import GarminClient

    client = GarminClient(
        email=os.getenv("GARMIN_EMAIL", ""),
        password=os.getenv("GARMIN_PASSWORD", ""),
        token_dir=os.getenv("GARMIN_TOKEN_DIR") or default_token_dir(),
        allow_writes=True,
    )
    created: list[tuple[str, str]] = []
    for i, spec in enumerate(specs):
        payload = build_strength_workout(spec)
        result = await client.call("upload_workout", payload)
        wid = str(result.get("workoutId", "?"))
        print(f"[created] {spec.name}  ->  workoutId {wid}")
        created.append((spec.name, wid))

        if i == 0:  # validate the first round-trips before uploading the rest
            check = await client.call("get_workout_by_id", wid)
            n_steps = sum(len(s.get("workoutSteps", [])) for s in check.get("workoutSegments", []))
            sport = (check.get("sportType") or {}).get("sportTypeKey")
            print(f"[validate] re-fetched {wid}: sport={sport}, {n_steps} top-level steps")
            if sport != "strength_training" or n_steps == 0:
                raise SystemExit("Day-1 validation failed — stopping before the rest.")
        await asyncio.sleep(4)  # be gentle: avoid the rapid-write 429 block

    print(f"\nCreated {len(created)} workouts:")
    for name, wid in created:
        print(f"  {wid}  {name}")
    print(
        "\nThey are in your Garmin Connect library. Open Garmin Connect on your "
        "phone near the watch to sync; they'll appear under Training > Workouts."
    )


def main() -> None:
    specs, warnings = build_specs()
    if "--create" in sys.argv:
        if not os.getenv("GARMIN_WRITE_ENABLED"):
            raise SystemExit("Refusing to create: set GARMIN_WRITE_ENABLED=1 to enable writes.")
        asyncio.run(create(specs))
    else:
        preview(specs, warnings)
        print("\n(preview only — re-run with GARMIN_WRITE_ENABLED=1 ... --create to upload)")


if __name__ == "__main__":
    main()
