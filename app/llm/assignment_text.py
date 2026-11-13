from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY = r"today|tomorrow|tomorow|tmrw|tmr|(?:next\s+)?(?:" + "|".join(WEEKDAYS) + r")"
GRADE_REFERENCE = re.compile(
    r"\b(?:grade|grde|garde|gade)\s+\d{1,2}(?:\s*-?\s*[a-z]\b)?",
    re.IGNORECASE,
)
EXPLICIT_DUE = re.compile(r"\bdue\s+(?:(?:on|by)\s+)?", re.IGNORECASE)
DEADLINE_START = re.compile(
    rf"\b(?:(?:by|until|on)\s+(?=(?:{DAY})\b|\d)|(?=(?:{DAY})\b))",
    re.IGNORECASE,
)
DEADLINE = re.compile(
    rf"(?P<day>{DAY}|\d{{4}}-\d{{2}}-\d{{2}})"
    r"(?:\s+(?:at\s+)?(?:(?P<part>morning|afternoon|evening)"
    r"|(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>[ap]m|[ap]\.m\.?)?))?",
    re.IGNORECASE,
)
NUMBER_WORD = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    "sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
)
AMBIGUOUS_NUMBER = re.compile(
    r"\b(?:questions?|qstns?|pages?|chapters?|chptr|exercises?)\s+"
    r"(?:[0-9]+[ilo]\b|[ilo][0-9]+\b|\?|[0-9]+\s+or\s+[0-9]+\b"
    rf"|\w+(?:[- ]\w+)?\s+or\s+(?:\d+|{NUMBER_WORD})\b"
    r"|[0-9]+\s*(?:\bto\b|[-–])\s*(?:\?|[0-9]+[ilo]\b|[ilo][0-9]+\b|[ilo]{2,}\b|$))",
    re.IGNORECASE,
)
RECIPIENT_PREFIX = re.compile(
    r"^(?:(?:assign\s+to|for)\s+([^:\n]+):|"
    r"(?:give|gve)\s+(?!(?:questions?|qstns?|pages?|chapter|worksheet|the)\b)"
    r"(\S(?:.*?\S)?)\s+(?=(?:questions?|qstns?|pages?|chapter|worksheet|the)\b))",
    re.IGNORECASE,
)
CLASS_QUALIFIER = re.compile(
    r"\s+(?!(?:complete|read|write|solve|questions?|qstns?|pages?)\b)"
    r"([a-z][a-z -]*?)\s+(?=(?:complete|read|write|solve|questions?|qstns?|pages?)\b)",
    re.IGNORECASE,
)


def split_deadline(text: str) -> tuple[str, str | None]:
    match = EXPLICIT_DUE.search(text) or DEADLINE_START.search(text)
    if match is None:
        return text, None
    return text[: match.start()].rstrip(" ,;:-"), text[match.end() :].strip().rstrip(".!?")


def parse_known_deadline(expression: str, timezone: str, now: datetime) -> datetime | None:
    """Resolve common deadline phrases independently of the language model."""
    expression = re.sub(
        r"^(?:due(?:\s+(?:on|by))?|by|on)\s+", "", expression.strip(), flags=re.IGNORECASE
    )
    match = DEADLINE.fullmatch(expression.rstrip(".!?"))
    if not match:
        return None
    local_now = now.astimezone(ZoneInfo(timezone))
    day = " ".join(match["day"].lower().split())
    date = local_now.date()
    if day in {"tomorrow", "tomorow", "tmrw", "tmr"}:
        date += timedelta(days=1)
    elif day[0].isdigit():
        date = datetime.fromisoformat(day).date()
    elif day != "today":
        next_week, _, weekday = day.rpartition(" ")
        target = WEEKDAYS.index(weekday)
        days = 7 + target - date.weekday() if next_week else (target - date.weekday()) % 7
        date += timedelta(days=days)
    hour = {"morning": 9, "afternoon": 15, "evening": 18}.get((match["part"] or "").lower(), 18)
    minute = int(match["minute"] or 0)
    if match["hour"]:
        hour = int(match["hour"])
        if match["period"]:
            if not 1 <= hour <= 12:
                raise ValueError("Enter a valid deadline time, including AM or PM.")
            hour = hour % 12 + (12 if match["period"].lower().startswith("p") else 0)
        elif 1 <= hour <= 12 and not match["hour"].startswith("0"):
            raise ValueError("Is the deadline time AM or PM?")
    due = datetime(date.year, date.month, date.day, hour, minute, tzinfo=local_now.tzinfo)
    if day in WEEKDAYS and due <= local_now:
        due += timedelta(days=7)
    return due


def reference_words(value: str) -> str:
    value = re.sub(r"(?<=\d)(?=[a-z])", " ", value.casefold())
    return " ".join(re.findall(r"[^\W_]+", value))
