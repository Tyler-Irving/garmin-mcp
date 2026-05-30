#!/usr/bin/env python3
"""Read-only diagnostic spike: learn Garmin's real strength-workout schema.

This does NOT write anything to Garmin. It reuses the same saved session tokens
as the MCP server, lists your workouts, and (given an id or name) dumps a
workout's JSON and FIT to ``tmp/capture/`` so we can reverse-engineer the exact
encoding of sets / reps / weight / exercise category before building anything.

Usage:
    python scripts/capture_workout.py                 # list workouts (id, name, sport)
    python scripts/capture_workout.py <workout_id>    # dump JSON + FIT for that id
    python scripts/capture_workout.py "capture test"  # find by name substring, dump first match

If the saved session has expired it will try GARMIN_EMAIL / GARMIN_PASSWORD from
the environment / .env (cannot handle MFA non-interactively).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Make the project's token-dir helper importable (src layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from garmin_mcp.paths import default_token_dir  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "tmp" / "capture"


def login():
    from garminconnect import Garmin

    load_dotenv()
    token_dir = os.getenv("GARMIN_TOKEN_DIR") or default_token_dir()
    email = os.getenv("GARMIN_EMAIL", "")
    password = os.getenv("GARMIN_PASSWORD", "")

    g = Garmin(email=email, password=password, return_on_mfa=False)
    try:
        g.login(token_dir)
        print(f"[ok] resumed saved session from {token_dir}", file=sys.stderr)
        return g
    except Exception as exc:  # noqa: BLE001 - garminconnect raises a mix of types
        print(
            f"[warn] could not resume saved session ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        if not email or not password:
            print(
                "[fail] No valid saved session and GARMIN_EMAIL/PASSWORD not set.\n"
                "       Re-authenticate (e.g. `garmin-mcp login`) and retry.",
                file=sys.stderr,
            )
            raise
        g = Garmin(email=email, password=password, return_on_mfa=False)
        g.login()
        try:
            g.garth.dump(token_dir)
        except Exception:  # noqa: BLE001
            pass
        print("[ok] fresh login succeeded", file=sys.stderr)
        return g


def list_workouts(g) -> None:
    workouts = g.get_workouts(0, 100)
    if not workouts:
        print("(no workouts found in this account)")
        return
    print(f"{'id':>12}  {'sportType':<22}  name")
    print("-" * 70)
    for w in workouts:
        wid = w.get("workoutId", "?")
        sport = (w.get("sportType") or {}).get("sportTypeKey", "?")
        name = w.get("workoutName", "")
        print(f"{wid:>12}  {sport:<22}  {name}")
    print(f"\n{len(workouts)} workout(s). Re-run with an id or name substring to dump one.")


def summarize_strength(wj: dict) -> None:
    """Print the strength-relevant fields so we can eyeball the encoding."""
    print("\n=== STRENGTH FIELD SUMMARY ===")
    sport = (wj.get("sportType") or {})
    print(f"sportType: {sport.get('sportTypeId')} / {sport.get('sportTypeKey')}")
    segments = wj.get("workoutSegments") or []
    for seg in segments:
        for step in seg.get("workoutSteps", []):
            _print_step(step, indent=2)


def _print_step(step: dict, indent: int) -> None:
    pad = " " * indent
    stype = (step.get("stepType") or {}).get("stepTypeKey")
    if step.get("type") == "RepeatGroupDTO":
        print(f"{pad}REPEAT x{step.get('numberOfIterations')} (stepType={stype})")
        for child in step.get("workoutSteps", []):
            _print_step(child, indent + 4)
        return
    end = (step.get("endCondition") or {}).get("conditionTypeKey")
    fields = {
        k: step.get(k)
        for k in (
            "exerciseCategory",
            "exerciseName",
            "weightValue",
            "weightDisplayUnit",
            "weightUnit",
            "endConditionValue",
            "description",
        )
        if step.get(k) is not None
    }
    print(f"{pad}step[{step.get('stepOrder')}] type={stype} end={end} {fields}")


def dump_one(g, selector: str) -> None:
    workouts = g.get_workouts(0, 100)
    match = None
    if selector.isdigit():
        match = next((w for w in workouts if str(w.get("workoutId")) == selector), None)
    if match is None:
        match = next(
            (w for w in workouts if selector.lower() in (w.get("workoutName") or "").lower()),
            None,
        )
    if match is None:
        print(f"[fail] no workout matching '{selector}'. Run with no args to list.", file=sys.stderr)
        sys.exit(1)

    wid = match["workoutId"]
    name = match.get("workoutName", "workout")
    safe = "".join(c if c.isalnum() else "_" for c in str(name))[:40]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    detail = g.get_workout_by_id(wid)
    json_path = OUT_DIR / f"{wid}_{safe}.json"
    json_path.write_text(json.dumps(detail, indent=2))
    print(f"[ok] wrote workout JSON -> {json_path}")

    try:
        fit = g.download_workout(wid)
        fit_path = OUT_DIR / f"{wid}_{safe}.fit"
        fit_path.write_bytes(fit)
        print(f"[ok] wrote workout FIT  -> {fit_path} ({len(fit)} bytes)")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] FIT download failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    summarize_strength(detail)


def main() -> None:
    g = login()
    if len(sys.argv) < 2:
        list_workouts(g)
    else:
        dump_one(g, sys.argv[1])


if __name__ == "__main__":
    main()
