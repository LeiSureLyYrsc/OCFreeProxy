from __future__ import annotations

from datetime import datetime, timezone


def seconds_until_next_utc_midnight(now: datetime | None = None) -> int:
    """OpenCode free tier resets on UTC calendar day boundaries."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    tomorrow = now.date().toordinal() + 1
    midnight = datetime.fromordinal(tomorrow).replace(tzinfo=timezone.utc)
    return max(1, int((midnight - now).total_seconds()))


def parse_retry_after(value: str | None) -> int | None:
    """Parse Retry-After as delta-seconds or HTTP-date. Returns seconds or None."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = int(value)
        return max(1, seconds)
    except ValueError:
        pass
    # HTTP-date (RFC 7231)
    for fmt in (
        "%a, %d %b %Y %H:%M:%S GMT",
        "%A, %d-%b-%y %H:%M:%S GMT",
        "%a %b %d %H:%M:%S %Y",
    ):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            delta = int((dt - datetime.now(timezone.utc)).total_seconds())
            return max(1, delta) if delta > 0 else 1
        except ValueError:
            continue
    return None


def cooldown_seconds_from_response(
    status_code: int,
    retry_after: str | None,
    body_text: str = "",
) -> int | None:
    """
    Decide cooldown duration for an upstream response.
    Returns seconds to cool, or None if not a rate-limit style failure.
    """
    markers = (
        "freeusagelimit",
        "rate limit",
        "rate_limit",
        "too many requests",
        "quota",
        "usage limit",
    )
    body_l = (body_text or "").lower()
    is_limit = status_code == 429 or any(m in body_l for m in markers)
    if not is_limit:
        return None
    parsed = parse_retry_after(retry_after)
    if parsed is not None:
        return parsed
    return seconds_until_next_utc_midnight()
