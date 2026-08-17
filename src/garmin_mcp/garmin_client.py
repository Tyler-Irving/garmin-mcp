"""Wrapper around the unofficial ``garminconnect`` Python package.

Responsibilities:
    * Lazy login on the first tool call.
    * Persist Garmin OAuth tokens to disk so we do not log in on every cold
      start. Default location is /tmp/garth which is writable on Cloud Run.
    * Re-authenticate transparently when the saved session has expired.
    * Rate limit to one outbound call every ``rate_limit_seconds`` seconds
      so we do not look like a misbehaving client.
    * Retry network blips with exponential backoff.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import structlog
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)


class GarminClientError(RuntimeError):
    """Raised when a Garmin call fails after retries."""


class GarminAuthError(GarminClientError):
    """Raised when re-authentication is required but cannot succeed."""


_RETRYABLE_NETWORK_EXC = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
)

# The wrapper dispatches by method name via getattr, so it can in principle
# invoke ANY method on garminconnect.Garmin (including writes/deletes). To keep
# the server read-only by *enforcement* rather than by call-site convention,
# only methods on these allowlists may be called. Write methods additionally
# require ``allow_writes=True`` (driven by GARMIN_WRITE_ENABLED).
_READ_METHODS: frozenset[str] = frozenset(
    {
        "get_sleep_data",
        "get_activities",
        "get_activity",
        "get_activity_splits",
        "get_activity_hr_in_timezones",
        "get_training_status",
        "get_hrv_data",
        "get_body_battery",
        "get_user_summary",
        "get_rhr_day",
        "get_stress_data",
        "get_training_readiness",
        "get_max_metrics",
        "get_race_predictions",
        "get_personal_record",
        "get_body_composition",
        "get_respiration_data",
        "get_weekly_intensity_minutes",
        "get_weekly_steps",
        "get_weekly_stress",
        "get_workouts",
        "get_workout_by_id",
        "get_activity_exercise_sets",
        "get_endurance_score",
        "get_hill_score",
        "get_activity_weather",
    }
)
# Deliberately minimal: create only. No delete_workout / schedule_workout.
_WRITE_METHODS: frozenset[str] = frozenset({"upload_workout"})


class GarminClient:
    """Async-friendly facade over the synchronous ``garminconnect`` library."""

    def __init__(
        self,
        email: str,
        password: str,
        token_dir: str = "/tmp/garth",
        rate_limit_seconds: float = 2.0,
        allow_writes: bool = False,
    ) -> None:
        # Empty creds are allowed: the server can run on saved tokens alone
        # (seeded by `garmin-mcp login`). They are only required when the saved
        # session expires and a fresh login is needed.
        self._email = email
        self._password = password
        self._token_dir = Path(token_dir)
        self._rate_limit_seconds = rate_limit_seconds
        self._allow_writes = allow_writes
        self._client: Any | None = None
        self._login_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._last_call_at = 0.0

    async def _ensure_logged_in(self, force: bool = False) -> Any:
        async with self._login_lock:
            if self._client is not None and not force:
                return self._client

            from garminconnect import Garmin  # local import keeps tests light

            self._token_dir.mkdir(parents=True, exist_ok=True)
            client = Garmin(email=self._email, password=self._password, return_on_mfa=False)

            try:
                await asyncio.to_thread(client.login, str(self._token_dir))
                log.info("garmin.login.resumed", token_dir=str(self._token_dir))
            except Exception as exc:  # broad, garminconnect raises a mix of types
                log.info(
                    "garmin.login.fresh",
                    reason=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                if not self._email or not self._password:
                    raise GarminAuthError(
                        "Saved Garmin session is invalid and no credentials are set. "
                        "Run `garmin-mcp login` to re-authenticate, or set "
                        "GARMIN_EMAIL and GARMIN_PASSWORD."
                    ) from exc
                client = Garmin(email=self._email, password=self._password, return_on_mfa=False)
                try:
                    await asyncio.to_thread(client.login)
                except Exception as login_exc:
                    raise GarminAuthError(f"Garmin login failed: {login_exc}") from login_exc
                try:
                    await asyncio.to_thread(client.client.dump, str(self._token_dir))
                except Exception as dump_exc:  # non-fatal; we just won't cache tokens
                    log.warning("garmin.token_dump.failed", error=str(dump_exc))
                log.info("garmin.login.success")

            self._client = client
            return client

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_call_at
            if elapsed < self._rate_limit_seconds:
                await asyncio.sleep(self._rate_limit_seconds - elapsed)
            self._last_call_at = time.monotonic()

    @staticmethod
    def _looks_like_auth_error(exc: BaseException) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in ("401", "403", "unauthorized", "forbidden", "expired", "auth", "login")
        )

    async def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Call a method on the underlying Garmin client.

        We retry on transient network errors. On what looks like an auth
        error we drop the cached client, re-login, and try one more time.
        """
        if method_name not in _READ_METHODS and method_name not in _WRITE_METHODS:
            raise GarminClientError(
                f"Method '{method_name}' is not on the allowlist; refusing to call it."
            )
        if method_name in _WRITE_METHODS and not self._allow_writes:
            raise GarminAuthError(
                f"Write method '{method_name}' blocked: writes are disabled "
                "(set GARMIN_WRITE_ENABLED=1 to enable)."
            )
        # Writes are non-idempotent POSTs: a network timeout AFTER the request
        # commits server-side, then a blind retry, would create a duplicate. So
        # do not retry writes on transient network errors. (The auth-error retry
        # below is still safe for writes — a 401/403 means the call was rejected
        # before committing, so re-login + one retry cannot duplicate.)
        is_write = method_name in _WRITE_METHODS
        max_attempts = 1 if is_write else 3

        async def _invoke() -> Any:
            await self._rate_limit()
            client = await self._ensure_logged_in()
            method = getattr(client, method_name, None)
            if method is None:
                raise GarminClientError(
                    f"garminconnect.Garmin has no method named '{method_name}'."
                )
            return await asyncio.to_thread(method, *args, **kwargs)

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                retry=retry_if_exception_type(_RETRYABLE_NETWORK_EXC),
                reraise=True,
            ):
                with attempt:
                    return await _invoke()
        except RetryError as retry_exc:
            raise GarminClientError(
                f"Garmin call '{method_name}' failed after retries: {retry_exc}"
            ) from retry_exc
        except _RETRYABLE_NETWORK_EXC as net_exc:
            raise GarminClientError(f"Garmin call '{method_name}' failed: {net_exc}") from net_exc
        except Exception as exc:
            if self._looks_like_auth_error(exc):
                log.warning("garmin.auth.expired", method=method_name, error=str(exc)[:200])
                self._client = None
                try:
                    return await _invoke()
                except Exception as retry_exc:
                    raise GarminAuthError(
                        f"Garmin re-auth attempt failed for '{method_name}': {retry_exc}"
                    ) from retry_exc
            raise GarminClientError(
                f"Garmin call '{method_name}' raised {type(exc).__name__}: {exc}"
            ) from exc

        # Unreachable; AsyncRetrying either returns or raises.
        raise GarminClientError(
            f"Unreachable: '{method_name}' produced no result."
        )  # pragma: no cover
