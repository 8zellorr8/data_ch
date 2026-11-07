import asyncio
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate

import httpx
import pytest

from ai.rate_errors import retry_hint
from backend.config import Settings
from backend.db import Repository
from backend.evaluator import ProviderDeferred
from backend.rate_limits import MemoryRateStore, RequestLimiter, SQLiteRateStore


def refusal(*, delay=None, quota=None):
    details = []
    if delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    if quota is not None:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": quota}],
            }
        )
    return {"error": {"message": "not for public output", "details": details}}


@pytest.mark.parametrize("header", ["12", formatdate(1012, usegmt=True)])
def test_retry_hints_respect_largest_header_or_body(header):
    response = httpx.Response(429, headers={"Retry-After": header}, json=refusal(delay="5.5s"))
    assert retry_hint(response, 1000) == (1012, False)
    response = httpx.Response(429, headers={"Retry-After": "1"}, json=refusal(delay="12.5s"))
    assert retry_hint(response, 1000) == (1012.5, False)


def test_daily_quota_preserves_long_delay_and_missing_hint_is_conservative():
    body = refusal(delay="53045s", quota="GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    assert retry_hint(httpx.Response(429, json=body), 1000) == (54045, True)
    body = refusal(quota="GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    assert retry_hint(httpx.Response(429, json=body), 1000) == (87400, True)


@pytest.mark.parametrize("value", ["NaN", "inf", "-20", "1e300", "garbage"])
def test_bad_delays_never_create_an_immediate_or_infinite_retry(value):
    response = httpx.Response(429, headers={"Retry-After": value}, json=refusal(delay=value + "s"))
    assert retry_hint(response, 1000) == (1060, False)


@pytest.mark.parametrize("payload", [None, [], {"error": []}, {"error": {"details": None}}])
def test_malformed_error_payload_has_safe_default(payload):
    assert retry_hint(httpx.Response(429, json=payload), 1000) == (1060, False)
    assert retry_hint(httpx.Response(429, text="not json"), 1000) == (1060, False)


def test_pacing_covers_repeated_calls_and_models_without_wall_clock_sleep():
    now = 1000.0
    starts = []

    async def sleep(seconds):
        nonlocal now
        now += seconds

    async def run():
        limiter = RequestLimiter(requests_per_minute=3, clock=lambda: now, sleep=sleep)
        for _ in range(3):
            await limiter.wait("model-a")
            starts.append(now)
        await limiter.wait("model-b")
        assert now == 1040  # Models have independent counters.

    asyncio.run(run())
    assert starts == [1000, 1020, 1040]


@pytest.mark.parametrize("persistent", [False, True])
def test_concurrent_reservations_cannot_burst(persistent, tmp_path):
    repo = Repository(tmp_path / "rate.sqlite3")
    repo.initialize()
    memory = MemoryRateStore()
    stores = [SQLiteRateStore(repo), SQLiteRateStore(repo)] if persistent else [memory, memory]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda store: store.acquire("model", lambda: 1000, 20), stores))
    assert sorted(r.delay for r in results) == [0, 20]
    # Fresh instances cannot reset the rate slot or a persistent cooldown.
    stores[0].defer("model", 54045, True)
    stores[1].defer("model", 1020, False)
    actual = SQLiteRateStore(repo) if persistent else memory
    result = actual.acquire("model", lambda: 1001, 20)
    assert result.retry_at == 54045 and result.quota_exhausted
    assert actual.acquire("model", lambda: 54045, 20).delay == 0


def test_cancelled_wait_does_not_consume_a_future_slot():
    now = 1000.0

    async def run():
        entered = asyncio.Event()

        async def sleep(seconds):
            entered.set()
            await asyncio.Event().wait()

        limiter = RequestLimiter(requests_per_minute=3, clock=lambda: now, sleep=sleep)
        await limiter.wait("model")
        pending = asyncio.create_task(limiter.wait("model"))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert limiter.store.acquire("model", lambda: 1020, 20).delay == 0
        await limiter.defer("model", 54045, True)
        with pytest.raises(ProviderDeferred) as caught:
            await limiter.wait("model")
        assert caught.value.provider_not_called and caught.value.usage is None

    asyncio.run(run())


@pytest.mark.parametrize("rpm", [0, -1, float("nan"), float("inf")])
def test_invalid_rate_fails_configuration(rpm):
    with pytest.raises(ValueError):
        RequestLimiter(requests_per_minute=rpm)


@pytest.mark.parametrize("retries", [-1, 11, 0.5, True])
def test_invalid_retry_budget_fails_configuration(retries):
    with pytest.raises(ValueError):
        Settings(batch_rate_retries=retries)
