"""Normalize original publication timestamps without inventing a local date."""
from datetime import datetime, timezone
import re


def source_timestamp(value):
    if isinstance(value, str):
        if len(value) > 64 or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)', value
        ):
            return None
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    try:
        # Internal dataclass / database datetime values use naive UTC.
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.replace(microsecond=0) if value.year >= 1000 else None
    except (ValueError, OverflowError):
        return None
