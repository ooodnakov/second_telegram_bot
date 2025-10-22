"""Global constants and shared configuration for the Telegram bot."""

from __future__ import annotations

import re
from datetime import timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:  # pragma: no cover - python <3.11 fallback
    from datetime import UTC
except ImportError:  # pragma: no cover - python <3.11 fallback
    UTC = timezone.utc  # type: ignore[assignment]

try:  # pragma: no cover - python <3.11 fallback
    from zoneinfo import ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - python <3.11 fallback
    ZoneInfoNotFoundError = Exception  # type: ignore[misc]

from bot.logging import logger
from bot.messages import get_message

LIST_PAGE_SIZE = 5
SKIP_KEYWORD = get_message("workflow.skip_keyword")
SKIP_KEYWORD_PATTERN = rf"(?i)^{re.escape(SKIP_KEYWORD)}$"

try:
    MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:  # pragma: no cover - depends on system tzdata
    MOSCOW_TZ = timezone(timedelta(hours=3))
    logger.warning(
        "Timezone data for Europe/Moscow not found; falling back to UTC+3 offset."
    )

MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

(POSITION, CONDITION, PHOTOS, SIZE, MATERIAL, DESCRIPTION, PRICE, CONTACTS) = range(8)

__all__ = [
    "CONTACTS",
    "CONDITION",
    "DESCRIPTION",
    "LIST_PAGE_SIZE",
    "MATERIAL",
    "MEDIA_ROOT",
    "MOSCOW_TZ",
    "PHOTOS",
    "POSITION",
    "PRICE",
    "SIZE",
    "SKIP_KEYWORD",
    "SKIP_KEYWORD_PATTERN",
    "UTC",
]
