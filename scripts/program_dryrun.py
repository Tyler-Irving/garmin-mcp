#!/usr/bin/env python3
"""Offline dry-run: map Training_Program_May_2026.md onto Garmin strength workouts.

No network, no writes. Encodes the 5 training days as structured specs, resolves
every exercise name against Garmin's catalog, builds the payloads, and prints a
resolution report so we can see which exercises map cleanly vs. need attention
BEFORE building the write path. Rep ranges use the LOWER bound as the target
(double-progression); time-based holds/carries use seconds; weights are omitted
(this program is RPE/auto-regulated — log load on the watch).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from garmin_mcp.exercise_resolver import resolve_exercise  # noqa: E402
from garmin_mcp.strength_builder import (  # noqa: E402
    BlockSpec,
    SetSpec,
    StrengthWorkoutSpec,
    build_strength_workout,
    summarize,
)

# Each block: (sets, [ (free_text, reps|None, seconds|None), ... ]).
# >1 item in a block == superset. Rep ranges -> lower bound (note kept separately).
PROGRAM: list[tuple[str, list[tuple[int, list[tuple[str, int | None, int | None]]]]]] = [
    ("Upper A (chest)", [
        (4, [("Bench Press", 6, None)]),
        (3, [("Chest-Supported Row", 8, None)]),
        (3, [("Incline DB Press", 8, None)]),
        (3, [("Lat Pulldown", 10, None)]),
        (3, [("Tricep Pushdown", 12, None), ("Hammer Curl", 12, None)]),  # superset
        (3, [("Ab Wheel Rollout", 8, None)]),
        (2, [("Dead Bug", 8, None)]),
    ]),
    ("Lower A (quad)", [
        (4, [("Leg Press", 6, None)]),
        (3, [("Romanian Deadlift", 8, None)]),
        (3, [("Smith Machine Bulgarian Split Squat", 10, None)]),
        (3, [("Leg Extension", 12, None)]),
        (3, [("Standing Calf Raise", 12, None)]),
        (3, [("Suitcase Carry", None, 40)]),
        (2, [("Side Plank", None, 30)]),
    ]),
    ("Upper B (back)", [
        (4, [("Cable Row", 6, None)]),
        (3, [("Seated Machine Overhead Press", 8, None)]),
        (3, [("Lat Pulldown", 8, None)]),
        (3, [("Incline DB Press", 10, None)]),
        (3, [("EZ Bar Curl", 10, None), ("Overhead Tricep Extension", 10, None)]),  # superset
        (3, [("Half-Kneeling Pallof Press", 8, None)]),
        (2, [("Standing Cable Anti-Rotation Press", 10, None)]),
    ]),
    ("Lower B (posterior)", [
        (4, [("Trap Bar Deadlift", 6, None)]),
        (3, [("Hack Squat", 10, None)]),
        (3, [("Seated Leg Curl", 10, None)]),
        (3, [("Hip Thrust", 10, None)]),
        (3, [("Seated Calf Raise", 12, None)]),
        (3, [("Captains Chair Leg Raise", 8, None)]),
        (2, [("Reverse Crunch", 12, None)]),
    ]),
    ("Arms/Shoulders/Core", [
        (4, [("Seated DB Shoulder Press", 8, None)]),
        (3, [("EZ Bar Curl", 8, None), ("Close-Grip Bench Press", 8, None)]),  # superset
        (3, [("Lateral Raise", 12, None), ("Tricep Rope Pushdown", 12, None)]),  # superset
        (3, [("Hammer Curl", 10, None), ("Overhead Tricep Extension", 10, None)]),  # superset
        (3, [("Rear Delt Fly", 15, None)]),
        (3, [("Cable Crunch", 10, None)]),
        (2, [("Pallof Press Iso-Hold", None, 30)]),
    ]),
]


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "tmp" / "dryrun"
    out_dir.mkdir(parents=True, exist_ok=True)

    flagged: list[str] = []
    seen: set[str] = set()
    total = 0

    for day_name, blocks in PROGRAM:
        spec_blocks: list[BlockSpec] = []
        print(f"\n{'=' * 72}\nDAY: {day_name}\n{'=' * 72}")
        for sets, items in blocks:
            set_specs: list[SetSpec] = []
            for text, reps, seconds in items:
                total += 1
                r = resolve_exercise(text)
                marker = {"exact": "✓", "high": "✓", "medium": "~", "low": "!", "none": "✗"}[r.confidence]
                detail = (
                    f"{r.category} / {r.exercise_name}" if r.exercise_name else "(no match)"
                )
                print(f"  {marker} [{r.confidence:<6} {r.score:>4}] {text:<38} → {detail}")
                if r.confidence not in ("exact", "high") and text not in seen:
                    flagged.append(f"{text}  →  {detail} ({r.confidence})")
                    seen.add(text)
                set_specs.append(
                    SetSpec(
                        category=r.category or "UNKNOWN",
                        exercise_name=r.exercise_name or text.upper().replace(" ", "_"),
                        reps=reps,
                        seconds=seconds,
                        label=r.matched_label or text,
                    )
                )
            spec_blocks.append(BlockSpec(sets=sets, exercises=set_specs))

        spec = StrengthWorkoutSpec(name=day_name, blocks=spec_blocks)
        payload = build_strength_workout(spec)
        slug = "".join(c if c.isalnum() else "_" for c in day_name.lower()).strip("_")
        (out_dir / f"{slug}.json").write_text(json.dumps(payload, indent=2))

    print(f"\n{'=' * 72}\nRESOLUTION SUMMARY\n{'=' * 72}")
    print(f"{total} exercise references across {len(PROGRAM)} days.")
    if flagged:
        print(f"\n{len(flagged)} need review (medium/low/none confidence):")
        for f in flagged:
            print(f"  • {f}")
    else:
        print("All exercises resolved with high confidence.")
    print(f"\nBuilt payloads written to {out_dir}")


if __name__ == "__main__":
    main()
