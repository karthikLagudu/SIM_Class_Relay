from __future__ import annotations

import uuid

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.security import current_session, current_user
from app.database import get_db
from app.models import User


def render(request: Request, template: str, **context):
    return request.app.state.templates.TemplateResponse(
        request=request, name=template, context={"request": request, **context}
    )


def web_user(request: Request, db: Session = Depends(get_db)) -> User:
    return current_user(request, db)


def csrf(request: Request) -> str:
    return current_session(request).csrf_token


def operation_key(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"
