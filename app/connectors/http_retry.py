import asyncio
import email.utils
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from app.observability.metrics import HTTP_RETRIES, HTTP_TIMEOUTS

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
MAX_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 5.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None

    stripped = value.strip()
    if not stripped:
        return None

    try:
        seconds = float(stripped)
        if seconds >= 0:
            return min(seconds, MAX_RETRY_DELAY_SECONDS)
    except ValueError:
        pass

    try:
        parsed = email.utils.parsedate_to_datetime(stripped)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    delta = (parsed - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(delta, MAX_RETRY_DELAY_SECONDS))


def _retry_delay_for(attempt: int, response: httpx.Response | None = None) -> float:
    if response is not None:
        header_value = response.headers.get("Retry-After")
        retry_after = _parse_retry_after(header_value)
        if retry_after is not None:
            return retry_after

    return min(2**attempt, MAX_RETRY_DELAY_SECONDS)


def _status_reason(status_code: int) -> str:
    return "status_429" if status_code == 429 else "status_5xx"


async def request_with_retry(
    request_factory: Callable[[], Awaitable[httpx.Response]],
    *,
    retry_status_codes: set[int] | None = None,
    max_retries: int = MAX_RETRIES,
    max_retry_delay: float = MAX_RETRY_DELAY_SECONDS,
    source: str = "unknown",
) -> httpx.Response:
    """Execute an HTTP request with short, bounded retries for transient failures.

    Retries are limited to network errors, timeouts, HTTP 429, and HTTP 5xx.
    Ordinary 4xx responses are raised immediately without retries.
    `source` is only a metrics label (github, jira or confluence).
    """
    retry_codes = retry_status_codes or RETRYABLE_STATUS_CODES

    for attempt in range(max_retries + 1):
        try:
            response = await request_factory()
            status_code = response.status_code

            if status_code in retry_codes and attempt < max_retries:
                HTTP_RETRIES.labels(source, _status_reason(status_code)).inc()
                delay = min(
                    _retry_delay_for(attempt, response=response),
                    max_retry_delay,
                )
                await asyncio.sleep(delay)
                continue

            response.raise_for_status()
            return response
        except httpx.TimeoutException:
            HTTP_TIMEOUTS.labels(source).inc()
            if attempt >= max_retries:
                raise
            HTTP_RETRIES.labels(source, "timeout").inc()
            await asyncio.sleep(min(2**attempt, max_retry_delay))
        except httpx.NetworkError:
            if attempt >= max_retries:
                raise
            HTTP_RETRIES.labels(source, "network").inc()
            await asyncio.sleep(min(2**attempt, max_retry_delay))
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code in retry_codes and attempt < max_retries:
                HTTP_RETRIES.labels(source, _status_reason(status_code)).inc()
                delay = min(
                    _retry_delay_for(attempt, response=exc.response),
                    max_retry_delay,
                )
                await asyncio.sleep(delay)
                continue
            raise

    raise RuntimeError("request_with_retry exhausted all attempts without returning")
