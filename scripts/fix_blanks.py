#!/usr/bin/env python3
"""One-off repair: update the 2 workouts whose first build blanked an exercise.

Garmin blanked bare category-name exercises (BENCH_PRESS, LEG_RAISE). The
resolver now prefers specific leaves (BARBELL_BENCH_PRESS, HANGING_LEG_RAISE);
this rebuilds the corrected payloads and PUTs them in place (no delete, no
duplicates), then re-fetches to assert no interval step is blank.

  GARMIN_WRITE_ENABLED=1 python scripts/fix_blanks.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_workout import login  # noqa: E402
from create_program import build_specs  # noqa: E402

from garmin_mcp.strength_builder import build_strength_workout  # noqa: E402

# spec index (order from build_specs) -> existing workoutId to update.
TARGETS = {0: 1583576775, 3: 1583577071}  # Upper A (chest), Lower B (posterior)


def _blank_interval_names(wj: dict) -> list[int]:
    bad: list[int] = []

    def walk(steps: list[dict]) -> None:
        for s in steps:
            if s.get("type") == "RepeatGroupDTO":
                walk(s.get("workoutSteps", []))
            elif (s.get("stepType") or {}).get("stepTypeKey") == "interval" and not s.get(
                "exerciseName"
            ):
                bad.append(s.get("stepOrder", -1))

    for seg in wj.get("workoutSegments", []):
        walk(seg.get("workoutSteps", []))
    return bad


def main() -> None:
    if not os.getenv("GARMIN_WRITE_ENABLED"):
        raise SystemExit("Set GARMIN_WRITE_ENABLED=1 to enable the update.")
    specs, _ = build_specs()
    g = login()
    for idx, wid in TARGETS.items():
        spec = specs[idx]
        payload = build_strength_workout(spec)
        payload["workoutId"] = wid
        g.client.put("connectapi", f"/workout-service/workout/{wid}", json=payload, api=True)
        print(f"[updated] {spec.name} (workoutId {wid})")
        blanks = _blank_interval_names(g.get_workout_by_id(wid))
        if blanks:
            raise SystemExit(f"  STILL BLANK at steps {blanks} — investigate")
        print("  validated: no blank exercise names")
    print("\nDone. Re-sync your watch to pick up the corrected workouts.")


if __name__ == "__main__":
    main()
