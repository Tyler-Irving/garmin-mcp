# TASK: v1 Workout Builder Tool — preview-then-confirm Garmin workout creation

## Description

Add the server's **first write capability**: an MCP tool pair that turns a workout
description into a structured Garmin workout, lets the user/agent preview exactly what
will be created, and on explicit confirmation uploads it to the user's Garmin Connect
**Workouts library** (where it becomes available on the watch after the next sync).

Scope is deliberately narrow for v1: **running and cycling only**, **create only**
(no delete, no calendar scheduling), **preview-then-confirm** enforced, and writes
**disabled by default** behind an env flag. The hard problem is *semantic correctness*
(pace→m/s, distance-vs-time step ends, repeat nesting, target encoding) — the upload
plumbing already exists in the vendored `garminconnect` library.

> **Honest scope note baked into the tool's own output:** uploading does NOT put the
> workout on the watch instantly. It lands in the Garmin Connect library and reaches the
> watch only on the next sync (open Garmin Connect on the phone near the watch, ~30s–few
> min). The tool's success string must say this; it must never claim "on your watch now."

## Context

### Current architecture (read-only today)
- `src/garmin_mcp/server.py` — FastMCP server. 15 `@mcp.tool()` async functions, each
  returns a Pydantic model from `models.py`, fetches via `_get_garmin().call("get_*", …)`,
  and caches through `_cache.get_or_compute(key, ttl, fetch)`. Date inputs go through
  `_normalise_date(value, default)` (`server.py:204`). Test hook:
  `set_garmin_client_for_testing()` (`server.py:181`).
- `src/garmin_mcp/garmin_client.py` — `GarminClient.call(method_name, *args, **kwargs)`
  (`:124`) is a **generic** `getattr(client, method_name)` dispatcher (`:134`) with **no
  allowlist**. The server is read-only purely by call-site convention (all 20 sites pass
  hardcoded `get_*` literals). This task changes that.
- `src/garmin_mcp/models.py` — Pydantic response models + `_opt_str/_opt_int/_opt_float`
  coercion helpers. Tool I/O models live here.
- `stateless_http=True` (`server.py:146`) — **no server-side session state survives a
  cold start.** Any confirmation mechanism MUST be stateless (no in-memory "pending
  previews").
- Auth: `JWT_SECRET` env var already exists (`server.py:108`) and is available for signing.

### Vendored library surface (already installed, verified against source)
`.venv/.../garminconnect/workout.py` and `.../__init__.py`:
- Models: `RunningWorkout`, `CyclingWorkout` (sportType auto-defaulted). Each needs
  `workoutName: str`, `estimatedDurationInSecs: int`, `workoutSegments: [WorkoutSegment]`.
  `WorkoutSegment(segmentOrder, sportType, workoutSteps)`. All models are pydantic with
  `extra="allow"` — **extra keys like `targetValueOne` pass straight through**.
- Step helpers (`workout.py:269–407`): `create_warmup_step(duration_seconds, step_order=1,
  target_type=None)`, `create_interval_step(duration_seconds, step_order, target_type=None)`,
  `create_recovery_step(...)`, `create_cooldown_step(...)`, `create_repeat_group(iterations,
  workout_steps, step_order)`. **No `create_rest_step`.**
- **Trap 1:** every step helper hardcodes `endCondition = TIME`. A distance step (e.g. 800 m)
  must have its `.endCondition` dict **replaced** with the DISTANCE condition (id 1) and
  `endConditionValue` set in **meters** — not just a different number.
- **Trap 2:** helpers default `targetType = NO_TARGET` and **never set target values**.
  `targetValueOne`/`targetValueTwo`/`zoneNumber` appear **nowhere** in the library — the
  builder must inject them as extra fields. Garmin pace/speed targets are in **m/s**.
- `upload_workout(payload)` → POSTs to `connectapi.garmin.com/workout-service/workout`,
  returns the **raw** API response dict (contains a top-level `workoutId`, which the library
  does **not** extract). Typed wrappers `upload_running_workout` / `upload_cycling_workout`
  call `.to_dict()` then `upload_workout`.
- `to_dict()` = `model_dump(exclude_none=True, mode="json")`.

### Why two tools + a flag (threat model)
Adding writes to an LLM-driven server creates a confused-deputy / prompt-injection risk
(OWASP LLM01). Safeguards in this spec: writes **off by default** (`GARMIN_WRITE_ENABLED`),
**explicit allowlist** in `call()`, **preview-then-confirm bound to exact content** via an
HMAC token, **no delete**, **no auto-schedule**, and audit logging.

## Requirements

1. **R1 — Structured input model.** Define a `WorkoutSpec` (in `models.py`) the agent fills:
   `name: str`, `sport: "running" | "cycling"`, `steps: list[WorkoutStepSpec]`. Natural
   language is the "front door," but the LLM emits this structured spec; the tool does NOT
   parse free text and does NOT let the LLM hand-assemble raw Garmin target numbers.
2. **R2 — Step spec.** Each `WorkoutStepSpec` is one of: `warmup`, `interval`, `recovery`,
   `cooldown`, `rest`, or `repeat`. Executable steps carry an **end condition**
   (`{by: "time"|"distance", value, unit}` — time units `s|min`, distance units `m|km`) and
   an optional **target** (see R3). `repeat` carries `iterations: int` and nested `steps`.
   Lap-button end is **out of scope** (no library constant) — reject it with a clear error.
3. **R3 — Target encoding (the load-bearing logic, in tested code).** Support, per sport:
   - `none` (default).
   - `pace` (running): input as `"m:ss/km"` or `"m:ss/mi"` (or a low/high band) → convert to
     a **speed.zone** target with `targetValueOne`/`targetValueTwo` in **m/s**, lower speed in
     `One`, higher in `Two`. A single pace becomes a small band (configurable tolerance,
     default ±3 s/km).
   - `hr_zone`: zone `1–5` → `heart.rate.zone` target with `zoneNumber`.
   - `hr_range`: `{low_bpm, high_bpm}` → `heart.rate.zone` target with bpm `targetValueOne/Two`.
   - `power` (cycling): `{low_w, high_w}` → `power.zone` target with watt `targetValueOne/Two`.
   - `cadence` (both): `{low, high}` → `cadence` target with `targetValueOne/Two`.
   All conversions live in `workout_builder.py` and are unit-tested. Units are explicit in
   the input; the builder never guesses.
4. **R4 — Deterministic builder.** A pure function `build_workout(spec) -> (model, summary,
   warnings)` that: assigns monotonic `stepOrder` (including correct nesting inside repeat
   groups), overrides `endCondition` to DISTANCE where the step is distance-based, injects
   target values per R3, and computes `estimatedDurationInSecs` (sum time steps; estimate
   distance steps from the target speed midpoint, else a sport default — running 3.0 m/s,
   cycling 7.0 m/s; flag the estimate in `warnings`). No network, no global state.
5. **R5 — `preview_workout` tool.** Takes a `WorkoutSpec`, returns a `WorkoutPreview`:
   human-readable summary (e.g. "10 min warmup · 4×(800 m @ 4:00–4:06/km + 2 min jog) ·
   10 min cooldown · est. 38 min"), the list of `warnings`, and a `confirmation_token`.
   **Makes no network call.**
6. **R6 — Confirmation token (stateless, content-bound).** `confirmation_token =
   HMAC-SHA256(JWT_SECRET, canonical_json(built_payload))` (hex, truncated to 16 bytes).
   Because the builder is deterministic, the same `WorkoutSpec` reproduces the same token.
   If `JWT_SECRET` is unset (stdio dev), fall back to requiring an explicit `confirm=True`
   argument instead. No server-side pending state (server is `stateless_http`).
7. **R7 — `create_workout` tool.** Takes the same `WorkoutSpec` plus `confirmation_token`,
   **recomputes** the token from the rebuilt payload, and uploads only on an exact match
   (else raise: "spec changed since preview — call preview_workout again"). On success it
   extracts `result["workoutId"]` and returns a `WorkoutCreated` model with the id and an
   **honest** status string (library, not watch; appears after next sync). Not cached.
8. **R8 — Writes off by default.** `create_workout` performs an upload only when
   `GARMIN_WRITE_ENABLED` is truthy (new env var, documented in `.env.example`, default
   off). When disabled, the tool still previews/validates but refuses to upload with a clear
   message. `preview_workout` always works.
9. **R9 — Enforce read-only in the wrapper (defense in depth).** Add an allowlist to
   `GarminClient.call()`: a `_READ_METHODS` frozenset (the existing `get_*` names) and a
   `_WRITE_METHODS` frozenset (`upload_workout`, `upload_running_workout`,
   `upload_cycling_workout`). Reject any method not in the union. Write methods additionally
   require the client to be constructed with `allow_writes=True` (sourced from
   `GARMIN_WRITE_ENABLED`); otherwise raise `GarminClientError`. This makes read-only
   *enforced*, not conventional.
10. **R10 — No delete, no schedule in v1.** Do not expose `delete_workout`,
    `schedule_workout`, or `unschedule_workout`. Keep them out of `_WRITE_METHODS`. Document
    as explicit non-goals.
11. **R11 — Audit logging.** Log a structured event on every upload attempt and result
    (`workout.create.attempt` / `workout.create.success` with `workout_id`, sport, step
    count, name; `workout.create.denied` when writes disabled), via the existing structlog
    logger to **stderr** (never stdout — stdio transport).
12. **R12 — Tests.** Golden-payload tests for representative workouts; unit-conversion tests;
    repeat-nesting/stepOrder tests; token match/mismatch; write-disabled rejection; the
    allowlist rejecting a non-listed method. Use the existing fake-client / test-hook pattern.

## Technical Approach

1. **`src/garmin_mcp/workout_builder.py` (new, pure / no I/O).**
   - Unit converters: `pace_to_mps("4:00/km") -> 4.1667`, `pace_band(...)`, `km_to_m`,
     `min_to_s`. Reject malformed units with `ValueError`.
   - `_target_dict(sport, target_spec) -> (target_type_dict, extra_fields)` implementing R3.
   - `build_workout(spec) -> BuiltWorkout` (dataclass: `model: RunningWorkout|CyclingWorkout`,
     `payload: dict`, `summary: str`, `warnings: list[str]`). Uses the library helpers, then
     mutates `endCondition` for distance steps and attaches target extras. Recursively builds
     repeat groups, threading `stepOrder` through a shared counter so nesting is correct.
   - `canonical_json(payload) -> str` (sorted keys, no whitespace) for token hashing.
2. **`src/garmin_mcp/models.py` — add I/O models.** `TargetSpec`, `EndConditionSpec`,
   `WorkoutStepSpec` (discriminated by `kind`), `WorkoutSpec`, `WorkoutPreview`
   (`summary`, `warnings`, `confirmation_token`, optional `payload_preview`), `WorkoutCreated`
   (`workout_id`, `sport`, `name`, `status`). Validate `sport ∈ {running,cycling}`,
   `iterations ≥ 1`, positive durations/distances; reject lap-button ends.
3. **`src/garmin_mcp/garmin_client.py` — allowlist + write gate.**
   - Add `allow_writes: bool = False` to `__init__`.
   - Module-level `_READ_METHODS` (enumerate the 16 currently-used `get_*` names) and
     `_WRITE_METHODS = {"upload_workout", "upload_running_workout", "upload_cycling_workout"}`.
   - In `call()`, before dispatch: if `method_name not in _READ_METHODS | _WRITE_METHODS` →
     `GarminClientError("method not allowed")`; if in `_WRITE_METHODS` and not
     `self.allow_writes` → `GarminClientError("writes disabled")`. Keep existing retry/auth
     logic. (Confirm the 16 read names against the current call-sites before freezing the set.)
4. **`src/garmin_mcp/server.py` — wire it up.**
   - Read `GARMIN_WRITE_ENABLED` (default off); pass `allow_writes=` into `GarminClient(...)`
     in `_get_garmin()`.
   - `_workout_token(payload) -> str`: HMAC with `JWT_SECRET`; if no secret, signal dev-mode
     (token = `""`, create requires `confirm=True`).
   - `@mcp.tool() async def preview_workout(workout: WorkoutSpec) -> WorkoutPreview`: call
     `build_workout`, return summary/warnings/token. No network.
   - `@mcp.tool() async def create_workout(workout: WorkoutSpec, confirmation_token: str = "",
     confirm: bool = False) -> WorkoutCreated`: rebuild → verify token (or `confirm` in dev)
     → check `GARMIN_WRITE_ENABLED` → `await _get_garmin().call("upload_running_workout"|
     "upload_cycling_workout", model)` → extract `workoutId` → audit-log → return honest status.
5. **Config + docs.** Add `GARMIN_WRITE_ENABLED=false` to `.env.example`; document the two
   tools, the sync caveat, the write flag, and the non-goals (no delete/schedule) in
   `README.md` and `CHANGELOG.md`.
6. **Tests.** `tests/test_workout_builder.py` (conversions, golden payloads incl. the
   `4×800m @ pace + jog` case, repeat nesting, estimatedDuration). `tests/test_workout_tools.py`
   (preview no-network, token match/mismatch, write-disabled refusal, allowlist rejection,
   honest status string) using `set_garmin_client_for_testing` with a fake recording client.
7. **Manual on-watch verification (cannot be unit-tested).** Because "HTTP 200 ≠ correct,"
   one real running interval workout with a pace target must be created against a live account
   and **visually verified on the watch** (correct rep count, distance vs time, pace band).
   Record the outcome in `claude-progress.txt`.

## Acceptance Criteria

- [ ] `WorkoutSpec`/step/target/preview/created models exist in `models.py` and validate sport,
      iterations, positive magnitudes, and reject lap-button ends with a clear error.
- [ ] `build_workout` is pure (no network/global state) and returns model + payload + summary + warnings.
- [ ] Pace converts correctly to m/s (e.g. `4:00/km → 4.167 m/s`), with lower speed in
      `targetValueOne` and higher in `targetValueTwo`; golden test asserts the exact target dict.
- [ ] Distance steps emit a DISTANCE end condition with `endConditionValue` in **meters**
      (an 800 m interval is **not** 800 seconds) — covered by a golden test.
- [ ] A `4×800m @ 5k pace + 2 min jog, 10 min wu/cd` spec produces a single repeat group of 4
      iterations with the recovery **inside** it and a coherent `stepOrder` sequence.
- [ ] `estimatedDurationInSecs` is a plausible int and time-only workouts compute it exactly.
- [ ] `preview_workout` returns a readable summary + warnings + token and makes **no** network call.
- [ ] `create_workout` rejects a tampered/absent token; uploads only on exact match (or `confirm=True`
      in dev mode with no `JWT_SECRET`).
- [ ] With `GARMIN_WRITE_ENABLED` unset/false, `create_workout` refuses to upload with a clear
      message; with it true, it uploads and returns `workout_id` from `result["workoutId"]`.
- [ ] `GarminClient.call()` rejects any method not in the read∪write allowlist, and rejects
      write methods when `allow_writes=False`. Existing read tools are unaffected (all 14/14+
      prior tests still pass).
- [ ] `create_workout`'s status string states the workout is in the Garmin Connect library and
      appears on the watch after the next sync — it does **not** claim "on your watch now."
- [ ] `delete_workout`/`schedule_workout`/`unschedule_workout` are not reachable from any tool.
- [ ] All logging stays on stderr (no stdout pollution; stdio transport still connects in Inspector).
- [ ] One real running interval workout created on a live account and verified correct **on the watch**;
      result recorded in `claude-progress.txt`.

## Definition of Done

- [ ] All acceptance criteria met
- [ ] Lint passes (`ruff check .` and `ruff format --check .`)
- [ ] Type checks pass (`mypy --strict src`)  *(replaces "TypeScript compiles" — this is a Python project)*
- [ ] Tests pass (`pytest` — new builder/tool tests plus all prior tests green)
- [ ] No stdout pollution / stdio mode still connects cleanly in MCP Inspector
- [ ] `claude-progress.txt` updated (incl. the manual on-watch verification result)

## Non-Goals (explicit, for v1)

- Deleting workouts (`delete_workout`) — highest blast radius, least reversible.
- Scheduling to the calendar (`schedule_workout`) — extra write + calendar mutation; the
  library entry is enough to start the workout manually.
- Swimming / multisport / fitness-equipment / walking / hiking sports.
- Lap-button end conditions (no library constant).
- Free-text NL parsing inside the tool, or unattended/automated (non-interactive) creation.
