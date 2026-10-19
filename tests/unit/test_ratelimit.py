import fakeredis

from app.services.ratelimit import check_rate_limit


async def test_blocks_after_limit_and_resets_next_window() -> None:
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    t = 1_000_000.0
    results = [await check_rate_limit(redis, "k1", 3, now=t) for _ in range(5)]
    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0, 0]
    assert 1 <= results[0].reset_seconds <= 60

    assert (await check_rate_limit(redis, "k1", 3, now=t + 60)).allowed


async def test_limits_are_per_identity() -> None:
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    assert (await check_rate_limit(redis, "a", 1, now=5.0)).allowed
    assert not (await check_rate_limit(redis, "a", 1, now=5.0)).allowed
    assert (await check_rate_limit(redis, "b", 1, now=5.0)).allowed


async def test_fails_open_when_redis_is_down() -> None:
    redis = fakeredis.FakeAsyncRedis(decode_responses=True, server=fakeredis.FakeServer())
    redis.server_connected = False  # type: ignore[attr-defined]
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def boom(*a: object, **k: object) -> None:
        raise RedisConnectionError("down")

    redis.pipeline = lambda *a, **k: _BrokenPipe()  # type: ignore[method-assign, assignment]
    assert (await check_rate_limit(redis, "x", 1)).allowed


class _BrokenPipe:
    def incr(self, *a: object) -> None: ...
    def expire(self, *a: object) -> None: ...

    async def execute(self) -> None:
        from redis.exceptions import ConnectionError as RedisConnectionError

        raise RedisConnectionError("down")
