"""Pydantic response models for each tool.

Every tool returns one of these models. Fields are optional because Garmin
sometimes has partial data (e.g. no sleep recorded, no HRV reading taken).
Tools should return the model with whatever they could populate; absent data
is signalled by ``None`` rather than missing keys.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _StrictBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SleepSummary(_StrictBase):
    """One night of sleep, summarised."""

    date: str = Field(description="Calendar date for the sleep period, YYYY-MM-DD.")
    sleep_score: int | None = Field(default=None, description="Garmin sleep score, 0 to 100.")
    sleep_quality: str | None = Field(
        default=None, description="Qualitative label such as GOOD or POOR."
    )
    total_sleep_seconds: int | None = None
    deep_sleep_seconds: int | None = None
    light_sleep_seconds: int | None = None
    rem_sleep_seconds: int | None = None
    awake_seconds: int | None = None
    avg_overnight_hrv_ms: float | None = Field(
        default=None, description="Average overnight HRV in milliseconds."
    )
    avg_respiration: float | None = None
    avg_spo2: float | None = None
    note: str | None = Field(
        default=None, description="Set when no sleep data was found for the date."
    )


class Activity(_StrictBase):
    activity_id: str
    name: str | None = None
    activity_type: str | None = None
    start_time: str | None = Field(default=None, description="Local start time as ISO-8601.")
    duration_seconds: float | None = None
    distance_meters: float | None = None
    avg_heart_rate: float | None = None
    max_heart_rate: float | None = None
    calories: float | None = None


class ActivityList(_StrictBase):
    activities: list[Activity]
    count: int


class ActivitySplit(_StrictBase):
    split_index: int | None = None
    distance_meters: float | None = None
    duration_seconds: float | None = None
    avg_heart_rate: float | None = None
    avg_speed_mps: float | None = None
    elevation_gain_meters: float | None = None


class HRZone(_StrictBase):
    zone_number: int | None = None
    seconds_in_zone: float | None = None
    zone_low_boundary: int | None = None


class ActivityDetail(_StrictBase):
    activity_id: str
    name: str | None = None
    activity_type: str | None = None
    start_time: str | None = None
    duration_seconds: float | None = None
    distance_meters: float | None = None
    avg_heart_rate: float | None = None
    max_heart_rate: float | None = None
    avg_power: float | None = None
    max_power: float | None = None
    normalised_power: float | None = None
    calories: float | None = None
    elevation_gain_meters: float | None = None
    elevation_loss_meters: float | None = None
    avg_speed_mps: float | None = None
    max_speed_mps: float | None = None
    training_effect_aerobic: float | None = None
    training_effect_anaerobic: float | None = None
    splits: list[ActivitySplit] = Field(default_factory=list)
    hr_zones: list[HRZone] = Field(default_factory=list)


class TrainingLoadDay(_StrictBase):
    date: str
    daily_training_load: float | None = Field(
        default=None, description="Training load contribution from this day's activities."
    )
    acute_training_load: float | None = Field(
        default=None, description="Acute training load (ATL), 7-day weighted average."
    )
    chronic_training_load: float | None = Field(
        default=None, description="Chronic training load (CTL), 28-day weighted average."
    )
    training_status: str | None = Field(
        default=None,
        description="Garmin training status such as PRODUCTIVE, MAINTAINING, OVERREACHING.",
    )


class TrainingLoadSummary(_StrictBase):
    days: list[TrainingLoadDay]
    current_status: str | None = None
    current_atl: float | None = None
    current_ctl: float | None = None


class HRVDayReading(_StrictBase):
    date: str
    last_night_avg_ms: float | None = None
    last_night_5min_high_ms: float | None = None
    status: str | None = None


class HRVStatus(_StrictBase):
    status: str | None = Field(
        default=None, description="Overall HRV status, such as BALANCED or LOW."
    )
    last_night_avg_ms: float | None = None
    weekly_avg_ms: float | None = None
    baseline_low_ms: float | None = None
    baseline_high_ms: float | None = None
    feedback: str | None = None
    last_7_days: list[HRVDayReading] = Field(default_factory=list)


class BodyBatteryReading(_StrictBase):
    timestamp: str
    value: int | None = None
    status: str | None = None


class BodyBatterySummary(_StrictBase):
    date: str
    max_value: int | None = None
    min_value: int | None = None
    charged: int | None = None
    drained: int | None = None
    current_value: int | None = None
    timeline: list[BodyBatteryReading] = Field(default_factory=list)


class StepsAndCalories(_StrictBase):
    date: str
    total_steps: int | None = None
    step_goal: int | None = None
    total_distance_meters: float | None = None
    total_calories: int | None = None
    active_calories: int | None = None
    bmr_calories: int | None = None
    floors_climbed: int | None = None
    moderate_intensity_minutes: int | None = None
    vigorous_intensity_minutes: int | None = None


class RHRDay(_StrictBase):
    date: str
    rhr_bpm: int | None = None


class RestingHeartRateTrend(_StrictBase):
    days: list[RHRDay]
    avg_rhr_bpm: float | None = None


class StressBucket(_StrictBase):
    timestamp: str
    stress_level: int | None = Field(
        default=None, description="0-100 stress level, or -1 for not measured, -2 for resting."
    )


class StressSummary(_StrictBase):
    date: str
    avg_stress: int | None = None
    max_stress: int | None = None
    rest_minutes: int | None = None
    low_minutes: int | None = None
    medium_minutes: int | None = None
    high_minutes: int | None = None
    timeline: list[StressBucket] = Field(default_factory=list)


class TrainingReadinessFactor(_StrictBase):
    name: str
    feedback: str | None = None
    level: str | None = None


class TrainingReadiness(_StrictBase):
    date: str
    score: int | None = Field(default=None, description="0-100 training readiness score.")
    level: str | None = Field(
        default=None, description="Garmin readiness level, e.g. LOW, MODERATE, HIGH, PRIME."
    )
    feedback_short: str | None = None
    feedback_long: str | None = None
    sleep_score: int | None = None
    sleep_history_score: int | None = None
    recovery_time_hours: int | None = None
    acute_load: float | None = None
    hrv_status: str | None = None
    stress_history: int | None = None
    factors: list[TrainingReadinessFactor] = Field(default_factory=list)
    note: str | None = None


class RacePrediction(_StrictBase):
    distance: str = Field(description="5k, 10k, halfMarathon, or marathon.")
    seconds: int | None = None


class FitnessMetrics(_StrictBase):
    date: str
    vo2_max_running: float | None = Field(default=None, description="ml/kg/min for running.")
    vo2_max_cycling: float | None = Field(default=None, description="ml/kg/min for cycling.")
    fitness_age: float | None = None
    race_predictions: list[RacePrediction] = Field(default_factory=list)
    note: str | None = None


class PersonalRecord(_StrictBase):
    record_type: str = Field(
        description="Human-readable label like 'fastest_5k', 'longest_run', 'most_steps_in_a_day'."
    )
    unit: str | None = Field(
        default=None,
        description="What `raw_value` represents: 'seconds', 'meters', 'count', or 'days'.",
    )
    raw_value: float | None = Field(default=None, description="Raw value Garmin reported.")
    value_seconds: float | None = Field(
        default=None, description="Same as raw_value when unit='seconds', else null."
    )
    value_meters: float | None = Field(
        default=None, description="Same as raw_value when unit='meters', else null."
    )
    type_id: int | None = Field(default=None, description="Garmin's internal record type id.")
    activity_type: str | None = None
    record_date: str | None = None
    activity_id: str | None = None


class PersonalRecords(_StrictBase):
    records: list[PersonalRecord] = Field(default_factory=list)
    count: int


class BodyCompositionDay(_StrictBase):
    date: str
    weight_kg: float | None = None
    body_fat_percent: float | None = None
    body_water_percent: float | None = None
    muscle_mass_kg: float | None = None
    bone_mass_kg: float | None = None
    bmi: float | None = None


class BodyCompositionTrend(_StrictBase):
    days: list[BodyCompositionDay] = Field(default_factory=list)
    latest_weight_kg: float | None = None
    avg_weight_kg: float | None = None
    note: str | None = None


class RespirationSummary(_StrictBase):
    date: str
    avg_breaths_per_min: float | None = None
    lowest_breaths_per_min: float | None = None
    highest_breaths_per_min: float | None = None
    avg_sleep_breaths_per_min: float | None = None
    avg_waking_breaths_per_min: float | None = None
    note: str | None = None


class WeeklyBucket(_StrictBase):
    week_start: str
    value: float | None = None


class WeeklySummary(_StrictBase):
    metric: str = Field(description="steps, stress, or intensity_minutes.")
    weeks: list[WeeklyBucket] = Field(default_factory=list)
    avg_value: float | None = None
    note: str | None = None


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
