"""Tests for the workout library tools (list_workouts / delete_workout).

All offline via a fake Garmin client. Exercises the safety spine of delete:
write-flag gate, preview-first flow, id-bound confirmation token, and the
dev-confirm path being stdio-only.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from garmin_mcp import server

_RAW_WORKOUTS = [
    {
        "workoutId": 111,
        "workoutName": "Upper A",
        "sportType": {"sportTypeKey": "strength_training"},
        "updatedDate": "2026-05-29T10:00:00.0",
    },
    {
        "workoutId": 222,
        "workoutName": "Tempo Run",
        "sportType": {"sportTypeKey": "running"},
    },
    {"noId": True},
]


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((method, args))
        if method == "get_workouts":
            return _RAW_WORKOUTS
        if method == "get_workout_by_id":
            return {"workoutId": args[0], "workoutName": "Upper A"}
        if method == "delete_workout":
            return None
        raise AssertionError(f"unexpected method {method}")

    def called(self, method: str) -> bool:
        return any(m == method for m, _ in self.calls)


@pytest.fixture()
def fake() -> Any:
    client = _FakeClient()
    server.set_garmin_client_for_testing(client)
    yield client
    server.set_garmin_client_for_testing(None)


def test_list_workouts_parses_and_skips_malformed(fake: _FakeClient) -> None:
    result = asyncio.run(server.list_workouts())
    assert result.count == 2
    first = result.workouts[0]
    assert first.workout_id == "111"
    assert first.name == "Upper A"
    assert first.sport == "strength_training"
    assert first.updated == "2026-05-29T10:00:00.0"
    assert result.workouts[1].updated is None


def test_delete_blocked_when_writes_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", False)
    with pytest.raises(ValueError, match="Writes are disabled"):
        asyncio.run(server.delete_workout("111"))


def test_delete_preview_issues_token_without_deleting(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeClient
) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    preview = asyncio.run(server.delete_workout("111"))
    assert preview.deleted is False
    assert preview.confirmation_token
    assert preview.name == "Upper A"
    assert "NOT deleted" in preview.status
    assert not fake.called("delete_workout")


def test_delete_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    with pytest.raises(ValueError, match="confirmation_token does not match"):
        asyncio.run(server.delete_workout("111", confirmation_token="wrong"))
    assert not fake.called("delete_workout")


def test_delete_token_is_bound_to_workout_id(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeClient
) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    token_for_111 = asyncio.run(server.delete_workout("111")).confirmation_token
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(server.delete_workout("222", confirmation_token=token_for_111))
    assert not fake.called("delete_workout")


def test_delete_happy_path(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "testsecret")
    preview = asyncio.run(server.delete_workout("111"))
    result = asyncio.run(
        server.delete_workout("111", confirmation_token=preview.confirmation_token)
    )
    assert result.deleted is True
    assert result.name == "Upper A"
    assert "Deleted" in result.status
    assert fake.called("delete_workout")


def test_dev_confirm_only_on_stdio(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(server, "WRITE_ENABLED", True)
    monkeypatch.setattr(server, "JWT_SECRET", "")
    monkeypatch.setattr(server, "_TRANSPORT", "http")
    result = asyncio.run(server.delete_workout("111", confirm=True))
    assert result.deleted is False
    assert not fake.called("delete_workout")

    monkeypatch.setattr(server, "_TRANSPORT", "stdio")
    result = asyncio.run(server.delete_workout("111", confirm=True))
    assert result.deleted is True
    assert fake.called("delete_workout")
