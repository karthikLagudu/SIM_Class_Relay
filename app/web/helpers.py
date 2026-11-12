from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.security import current_session, current_user
from app.database import get_db
from app.exceptions import ValidationError
from app.models import User


def render(request: Request, template: str, *, status_code: int = 200, **context):
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=template,
        status_code=status_code,
        context={"offline_demo": request.app.state.settings.telegram_mode == "log", **context},
    )


def web_user(request: Request, db: Session = Depends(get_db)) -> User:
    return current_user(request, db)


def csrf(request: Request) -> str:
    return current_session(request).csrf_token


def operation_key(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


def redirect_with_message(path: str, message: str, *, anchor: str = "") -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    suffix = f"#{anchor}" if anchor else ""
    return RedirectResponse(
        f"{path}{separator}{urlencode({'message': message})}{suffix}", status_code=303
    )


def local_input(value: datetime, timezone: str) -> str:
    return (
        value.replace(tzinfo=value.tzinfo or UTC)
        .astimezone(ZoneInfo(timezone))
        .strftime("%Y-%m-%dT%H:%M")
    )


def parse_local_deadline(raw: str, timezone: str) -> datetime:
    try:
        wall_time = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("Enter a local date and time for the deadline.") from exc
    zone = ZoneInfo(timezone)
    value = wall_time.replace(tzinfo=zone)
    if wall_time.tzinfo is not None or value.utcoffset() != value.replace(fold=1).utcoffset():
        raise ValidationError("Choose an unambiguous time in the school’s time zone.")
    return value.astimezone(UTC)
