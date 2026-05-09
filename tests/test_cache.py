"""Tests for the TTL cache."""

from __future__ import annotations

import asyncio

import pytest

from garmin_mcp.cache import TTLCache


@pytest.mark.asyncio
async def test_set_and_get_returns_value() -> None:
    cache = TTLCache()
    await cache.set("key", {"hello": "world"}, ttl_seconds=60)
    assert await cache.get("key") == {"hello": "world"}


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    cache = TTLCache()
    assert await cache.get("missing") is None


@pytest.mark.asyncio
async def test_expired_entry_is_evicted() -> None:
    cache = TTLCache()
    await cache.set("key", "value", ttl_seconds=0.01)
    await asyncio.sleep(0.05)
    assert await cache.get("key") is None
    assert await cache.size() == 0


@pytest.mark.asyncio
async def test_get_or_compute_caches_result() -> None:
    cache = TTLCache()
    calls = 0

    async def compute() -> int:
        nonlocal calls
        calls += 1
        return 42

    first = await cache.get_or_compute("k", 60, compute)
    second = await cache.get_or_compute("k", 60, compute)
    assert first == 42
    assert second == 42
    assert calls == 1


@pytest.mark.asyncio
async def test_get_or_compute_recomputes_after_expiry() -> None:
    cache = TTLCache()
    calls = 0

    async def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    await cache.get_or_compute("k", 0.01, compute)
    await asyncio.sleep(0.05)
    second = await cache.get_or_compute("k", 0.01, compute)
    assert second == 2


def test_make_key_is_stable() -> None:
    a = TTLCache.make_key("tool", {"date": "2026-01-01", "limit": 5})
    b = TTLCache.make_key("tool", {"limit": 5, "date": "2026-01-01"})
    assert a == b


def test_make_key_differs_on_args() -> None:
    a = TTLCache.make_key("tool", {"date": "2026-01-01"})
    b = TTLCache.make_key("tool", {"date": "2026-01-02"})
    assert a != b


@pytest.mark.asyncio
async def test_clear_removes_all_entries() -> None:
    cache = TTLCache()
    await cache.set("a", 1, 60)
    await cache.set("b", 2, 60)
    await cache.clear()
    assert await cache.size() == 0
