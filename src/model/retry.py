"""Per-attempt backoff; waiting consumes the parent budget and is cancellable."""
import random
import re
import time
from email.utils import parsedate_to_datetime
from math import isfinite


def RetryAfter(value):
    """Keep only a finite duration, never headers or a provider error body."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        value = value.strip()
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            seconds = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = max(0.0, date.timestamp() - time.time())
        return seconds if isfinite(seconds) and seconds >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def Backoff(retry, retry_after=None):
    base = min(8.0, 0.5 * 2 ** min(retry, 4))
    return max(min(8.0, base + random.uniform(0.0, base * 0.25)), retry_after or 0.0)


def Wait(delay, context):
    context.CheckCancelled()
    # A hint beyond the local waiting ceiling cannot authorize an early retry.
    context.cancel.wait(min(delay, 60.0, context.Remaining()))
    context.CheckCancelled()
    return delay <= 60.0
