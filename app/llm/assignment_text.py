from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAY_NAMES = {alias: day for day in WEEKDAYS for alias in (day, day[:3])}
DAY = r"today|tomorrow|tomorow|tmrw|tmr|(?:next\s+)?(?:" + "|".join(WEEKDAY_NAMES) + r")"
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
    r"(?:\s+(?:at\s+)?(?:(?P<part>morning|afternoon|evening|evning)"
    r"|(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>[ap]m|[ap]\.m\.?)?))?",
    re.IGNORECASE,
)
UNITS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
NUMBER_WORD = "|".join(UNITS + TEENS + TENS)
NUMBER_VALUES = {**dict(zip(UNITS + TEENS, range(1, 20))), **dict(zip(TENS, range(20, 100, 10)))}
REFERENCE_NUMBER = (
    rf"(?:\d+|(?:{'|'.join(TENS)})(?:[- ](?:{'|'.join(UNITS)}))?|{NUMBER_WORD})\b"
    rf"(?![- ](?:{NUMBER_WORD}|hundred|thousand|million)\b)"
)
SPOKEN_REFERENCE = re.compile(
    r"\b(questions?|qstns?|qns?|pages?|pgs?|chapters?|chpter|chptr|ch\.?|exercises?)\s+"
    rf"({REFERENCE_NUMBER})(?:\s*(through|to|[-–])\s*({REFERENCE_NUMBER}))?"
    r"(?!\s+(?:through|to)\s+\w)",
    re.IGNORECASE,
)
SENTENCE_BREAK = re.compile(r"[.!?]\s+(?=\S)|\n+(?=\S)")
MISSING_RANGE_END = re.compile(
    r"\b(?:questions?|qstns?|qns?|pages?|pgs?|exercises?)\s+(\d+)\s*(?:\bto\b|[-–])\s*[.!?]?$",
    re.IGNORECASE,
)
QUESTION_REFERENCE = re.compile(
    r"\bQ\d+(?:\([a-z0-9]+\))?(?:[-–]Q\d+(?:\([a-z0-9]+\))?)?(?![\w(])", re.IGNORECASE
)
AMBIGUOUS_NUMBER = re.compile(
    r"\b(?:questions?|qstns?|qns?|pages?|pgs?|chapters?|chptr|exercises?)\s+"
    r"(?:[0-9]+[ilo]\b|[ilo][0-9]+\b|\?|[0-9]+\s+or\s+[0-9]+\b"
    rf"|\w+(?:[- ]\w+)?\s+or\s+(?:\d+|{NUMBER_WORD})\b"
    r"|[0-9]+\s*(?:\bto\b|[-–])\s*(?:\?|[0-9]+[ilo]\b|[ilo][0-9]+\b|[ilo]{2,}\b|$))",
    re.IGNORECASE,
)
RECIPIENT_PREFIX = re.compile(
    r"^(?:(?:assign\s+to|for)\s+([^:\n]+):|"
    r"(?:give|gve)\s+(?!(?:questions?|qstns?|qns?|pages?|pgs?|chapter|ch|worksheet|the)\b)"
    r"(\S(?:.*?\S)?)\s+(?=(?:questions?|qstns?|qns?|pages?|pgs?|chapter|ch|worksheet|the)\b))",
    re.IGNORECASE,
)
CLASS_QUALIFIER = re.compile(
    r"\s+(?!(?:complete|compleet|read|write|solve|submit|submt|questions?|qstns?|qns?|pages?|pgs?)\b)"
    r"([a-z][a-z -]*?)\s+(?=(?:complete|compleet|read|write|solve|submit|submt|questions?|qstns?|qns?|pages?|pgs?)\b)",
    re.IGNORECASE,
)


def split_deadline(text: str) -> tuple[str, str | None]:
    match = EXPLICIT_DUE.search(text) or DEADLINE_START.search(text)
    if match is None:
        return text, None
    work = text[: match.start()].rstrip(" ,;:-")
    deadline = text[match.end() :].strip()
    if boundary := SENTENCE_BREAK.search(deadline):
        if work and work[-1] not in ".!?":
            work += "."
        work = " ".join(filter(None, (work, deadline[boundary.end() :])))
        deadline = deadline[: boundary.start()]
    return work, deadline.rstrip(".!?")


def _number_digits(value: str) -> str:
    if value.isdecimal():
        return value
    return str(sum(NUMBER_VALUES[word] for word in re.split(r"[- ]", value.lower())))


def normalize_spoken_references(text: str) -> str:
    def replace_reference(match: re.Match[str]) -> str:
        label, first, separator, last = match.groups()
        if first.isdecimal() and (last is None or last.isdecimal()):
            return match[0]
        end = f"–{_number_digits(last)}" if separator and last else ""
        return f"{label} {_number_digits(first)}{end}"

    return SPOKEN_REFERENCE.sub(replace_reference, text)


def parse_known_deadline(expression: str, timezone: str, now: datetime) -> datetime | None:
    """Resolve common deadline phrases independently of the language model."""
    expression = re.sub(
        r"^(?:due(?:\s+(?:on|by))?|by|on)\s+", "", expression.strip(), flags=re.IGNORECASE
    )
    expression = re.sub(
        rf"\bat\s+({NUMBER_WORD})\b(?=\s*(?:[ap]\.?m\.?\s*)?$)",
        lambda match: "at " + _number_digits(match[1]),
        expression,
        flags=re.IGNORECASE,
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
        target = WEEKDAYS.index(WEEKDAY_NAMES[weekday])
        days = 7 + target - date.weekday() if next_week else (target - date.weekday()) % 7
        date += timedelta(days=days)
    hour = {"morning": 9, "afternoon": 15}.get((match["part"] or "").lower(), 18)
    minute = int(match["minute"] or 0)
    if match["hour"]:
        hour = int(match["hour"])
        if match["period"]:
            if not 1 <= hour <= 12:
                raise ValueError("Enter a valid deadline time, including AM or PM.")
            hour = hour % 12 + (12 if match["period"].lower().startswith("p") else 0)
        elif 1 <= hour <= 12 and not match["hour"].startswith("0"):
            clock = match["hour"] + (f":{match['minute']}" if match["minute"] else "")
            raise ValueError(f"Should this be due at {clock} AM or {clock} PM?")
    due = datetime(date.year, date.month, date.day, hour, minute, tzinfo=local_now.tzinfo)
    if day in WEEKDAY_NAMES and due <= local_now:
        due += timedelta(days=7)
    return due


def reference_words(value: str) -> str:
    value = re.sub(r"(?<=\d)(?=[a-z])", " ", value.casefold())
    return " ".join(re.findall(r"[^\W_]+", value))


def recipient_references(match: re.Match[str]) -> list[str]:
    return [
        name.strip()
        for name in re.split(r",|\s+and\s+", match[1] or match[2], flags=re.IGNORECASE)
        if name.strip()
    ]
