from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import current_session, current_user
from app.database import get_db
from app.exceptions import ValidationError
from app.models import Assignment, School, User
from app.services.authorization import (
    coordinated_schools,
    list_teacher_classes,
    require_coordinator_school,
    workspace_roles,
)
from app.web.live import workspace_version

WORKSPACES = {"coordinator": "Coordinator", "teacher": "Teacher", "student": "Student"}
LIVE_PAGE = re.compile(
    r"/(?:student|teacher|coordinator)"
    r"(?:/submissions(?:/\d+)?|/classes/\d+/(?:work|students/\d+)|(?:/classes/\d+)?/assignments/\d+)?"
)


def render(request: Request, template: str, *, status_code: int = 200, **context):
    roles = getattr(request.state, "workspace_roles", set())
    workspaces = {role: name for role, name in WORKSPACES.items() if role in roles}
    role = request.url.path.split("/", 2)[1]
    if role not in workspaces:
        role = next(iter(workspaces), None)
    user = getattr(request.state, "workspace_user", None)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=template,
        status_code=status_code,
        context={
            "offline_demo": request.app.state.settings.telegram_mode == "log",
            "live_version": getattr(request.state, "live_version", None) if status_code == 200 else None,
            "workspaces": workspaces,
            "workspace": role,
            "managed_schools": getattr(request.state, "managed_schools", ()),
            "teaching_classes": getattr(request.state, "teaching_classes", ()),
            "teaching_schools": getattr(request.state, "teaching_schools", {}),
            "school": getattr(request.state, "active_school", None),
            "user": user,
            "csrf_token": csrf(request) if user else None,
            **context,
        },
    )


def web_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if not request.url.path.startswith("/api/"):
        request.state.workspace_user = user
        request.state.workspace_roles = workspace_roles(db, user)
        workspace = request.url.path.split("/", 2)[1]
        if (
            request.method == "GET" and workspace in request.state.workspace_roles
            and LIVE_PAGE.fullmatch(request.url.path)
        ):
            request.state.live_version = workspace_version(db, user, workspace)
        if (
            request.url.path.split("/", 2)[1] == "coordinator"
            and "coordinator" in request.state.workspace_roles
        ):
            request.state.managed_schools = coordinated_schools(db, user)
        if (
            request.url.path.split("/", 2)[1] == "teacher"
            and "teacher" in request.state.workspace_roles
        ):
            classes = list_teacher_classes(db, user)
            request.state.teaching_classes = classes
            request.state.teaching_schools = {
                school.id: school
                for school in db.scalars(
                    select(School).where(School.id.in_({item.school_id for item in classes}))
                )
            }
    return user


def coordinator_school(request: Request, db: Session, user: User, school_id: int) -> School:
    school = require_coordinator_school(db, user, school_id)
    request.state.active_school = school
    return school


def csrf(request: Request) -> str:
    return current_session(request).csrf_token


def operation_key(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


def assignment_path(assignment: Assignment, workspace: str) -> str:
    return f"/{workspace}/classes/{assignment.classroom_id}/assignments/{assignment.id}"


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
