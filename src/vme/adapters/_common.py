"""Shared helpers for synchronous provider SDKs."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any, TypeVar

from vme.errors import (
    AdapterError,
    FatalAdapterError,
    ThrottledAdapterError,
    TransientAdapterError,
    redact_text,
)

T = TypeVar("T")


async def sdk_call(function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    try:
        return await asyncio.to_thread(functools.partial(function, *args, **kwargs))
    except AdapterError:
        raise
    except Exception as error:
        raise classify_sdk_error(error) from error


def classify_sdk_error(error: Exception) -> AdapterError:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    text = str(error).lower()
    if status == 429 or "too many requests" in text or "rate limit" in text:
        retry_after = getattr(error, "retry_after", None)
        return ThrottledAdapterError(
            redact_text(str(error)), retry_after_seconds=retry_after
        )
    if status in {408, 425, 500, 502, 503, 504} or any(
        marker in text
        for marker in ("timed out", "timeout", "connection reset", "temporarily unavailable")
    ):
        return TransientAdapterError(redact_text(str(error)))
    return FatalAdapterError(redact_text(str(error)))
