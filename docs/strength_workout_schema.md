# Garmin strength-workout JSON schema (reverse-engineered)

Captured from two real strength workouts on the account owner's Fenix-class watch
(`scripts/capture_workout.py`). This is the source of truth for the magic numbers in
`strength_builder.py`. The unofficial library has **no** strength model, so we build
the raw payload and POST it via `upload_workout(dict)`.

## Top level
```jsonc
{
  "workoutName": "Upper body",
  "sportType": { "sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5 },
  "estimatedDurationInSecs": 0,            // 0 is accepted; Garmin recomputes
  "workoutSegments": [ { "segmentOrder": 1, "sportType": {…strength…}, "workoutSteps": [ … ] } ]
}
```

## Step types (`stepType.stepTypeId` / key)
| id | key | use |
|----|-----|-----|
| 1 | warmup | optional lead-in step (ends on lap.button) |
| 3 | interval | a working set (one exercise) |
| 5 | rest | rest between sets (ends on lap.button) |
| 6 | repeat | a "sets" group (RepeatGroupDTO) |

## End conditions (`endCondition.conditionTypeId` / key)
| id | key | use |
|----|-----|-----|
| 1 | lap.button | warmup / rest — user presses lap to advance |
| 7 | iterations | on a repeat group; `endConditionValue` = number of sets |
| 10 | **reps** | on a working set; `endConditionValue` = rep count |

> Note: these ids are the **strength** condition space and differ from the cardio
> `ConditionType` enum in the vendored library (where 1 = distance). Do not mix them.

## "Sets" = repeat group
```jsonc
{
  "type": "RepeatGroupDTO",
  "stepOrder": 2,
  "stepType": { "stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6 },
  "numberOfIterations": 4,                 // ← number of SETS
  "endCondition": { "conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": false },
  "endConditionValue": 4.0,
  "smartRepeat": false,
  "skipLastRestStep": false,
  "workoutSteps": [ <exercise interval(s)>, <rest> ]
}
```
A **superset/circuit** is encoded as **multiple interval steps inside one repeat group**
before the rest step (captured: Bench Press + Hammer Curl in one `REPEAT x4`).

## Working set (interval step)
```jsonc
{
  "type": "ExecutableStepDTO",
  "stepOrder": 3,
  "stepType": { "stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3 },
  "endCondition": { "conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": true },
  "endConditionValue": 6.0,                // ← reps
  "targetType": { "workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1 },
  "category": "BENCH_PRESS",               // ← exercise CATEGORY enum
  "exerciseName": "BARBELL_BENCH_PRESS",   // ← specific exercise enum (must match catalog or shows "Unknown")
  "weightValue": 184.99,                   // optional; OMIT for bodyweight (Garmin UI stores ≈ -1 sentinel)
  "weightUnit": { "unitId": 9, "unitKey": "pound", "factor": 453.59237 }
}
```
- Exercise enums come from `data/exercise_taxonomy.json` (47 categories, 1510 exercises).
- `weightValue` round-trips through internal **kg** storage (185 lb → 184.9987), hence the
  known lb↔kg display bugs. For create we **omit** weight when none is prescribed (this
  program is RPE/auto-regulated), letting the user log actual load per set on the watch.

## Rest step
```jsonc
{
  "type": "ExecutableStepDTO",
  "stepOrder": 5,
  "stepType": { "stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5 },
  "endCondition": { "conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": true },
  "endConditionValue": 0.0
}
```

## stepOrder
A single monotonic counter across the whole workout: the repeat group takes a number and
its children continue from there (group=2 → children 3,4,5; next group=6 → 7,8,9 …).
