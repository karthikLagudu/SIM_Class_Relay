from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import validate_csrf
from app.database import get_db
from app.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models import ClassMembership, Classroom, Invite, School, TelegramLinkToken, User
from app.reminders.policy import aware_utc
from app.services.authorization import (
    require_coordinator_class,
    require_teacher_class,
)
from app.services.classroom import ClassroomService
from app.services.telegram_link import TelegramConnectionLink
from app.services.workload import school_teachers
from app.web.helpers import coordinator_school, csrf, redirect_with_message, render, web_user
from app.web.school_activity import school_activity

router = APIRouter()


def _workspace(request: Request) -> str:
    return "coordinator" if request.url.path.startswith("/coordinator/") else "teacher"


def _class_url(request: Request, class_id: int) -> str:
    return f"/{_workspace(request)}/classes/{class_id}"


def _check_class(request: Request, db: Session, user: User, class_id: int):
    require = (
        require_coordinator_class if _workspace(request) == "coordinator" else require_teacher_class
    )
    classroom = require(db, user, class_id)
    if _workspace(request) == "coordinator":
        coordinator_school(request, db, user, classroom.school_id)
    return classroom


def _render_class_page(
    request: Request,
    user: User,
    db: Session,
    class_id: int,
    *,
    connection_link: TelegramConnectionLink | None = None,
    link_student_id: int | None = None,
    message: str | None = None,
    errors: dict[str, str] | None = None,
    form: dict | None = None,
):
    classroom = _check_class(request, db, user, class_id)
    coordinating = _workspace(request) == "coordinator"
    students = list(
        db.scalars(
            select(User)
            .join(ClassMembership, ClassMembership.user_id == User.id)
            .where(
                ClassMembership.classroom_id == class_id,
                ClassMembership.role == "student",
            )
            .order_by(User.name)
        )
    )
    teachers = (
        list(
            db.scalars(
                select(User)
                .join(ClassMembership, ClassMembership.user_id == User.id)
                .where(ClassMembership.classroom_id == class_id, ClassMembership.role == "teacher")
                .order_by(User.name)
            )
        )
        if coordinating
        else []
    )
    invites = list(
        db.scalars(
            select(Invite).where(Invite.classroom_id == class_id).order_by(Invite.created_at.desc())
        )
    )
    student_ids = [student.id for student in students]
    active_tokens = {}
    if student_ids:
        active_tokens = {
            token.user_id: token
            for token in db.scalars(
                select(TelegramLinkToken)
                .where(
                    TelegramLinkToken.user_id.in_(student_ids),
                    TelegramLinkToken.used_at.is_(None),
                    TelegramLinkToken.revoked_at.is_(None),
                    TelegramLinkToken.expires_at > datetime.now(UTC),
                )
                .order_by(TelegramLinkToken.created_at)
            )
        }
    response = render(
        request,
        "classroom/people.html",
        user=user,
        classroom=classroom,
        students=students,
        coordinating=coordinating,
        class_url=_class_url(request, class_id),
        teachers=teachers,
        available_teachers=school_teachers(db, classroom.school_id)
        if coordinating and not classroom.archived_at
        else [],
        team_ids={teacher.id for teacher in teachers},
        destinations=list(
            db.scalars(
                select(Classroom)
                .where(
                    Classroom.school_id == classroom.school_id,
                    Classroom.archived_at.is_(None),
                    Classroom.id != class_id,
                )
                .order_by(Classroom.name)
            )
        )
        if coordinating and not classroom.archived_at
        else [],
        activity=school_activity(db, classroom.school_id, class_id) if coordinating else [],
        invites=invites,
        active_tokens=active_tokens,
        school=db.get(School, classroom.school_id),
        expired_invites={
            item.id
            for item in invites
            if item.expires_at and aware_utc(item.expires_at) <= datetime.now(UTC)
        },
        can_link=bool(request.app.state.settings.telegram_bot_username),
        status_code=422 if errors else 200,
        errors=errors or {},
        form=form or {},
        connection_link=connection_link,
        link_student_id=link_student_id,
        csrf_token=csrf(request),
        message=message,
    )
    if connection_link:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/coordinator/classes/{class_id}", response_class=HTMLResponse)
@router.get("/teacher/classes/{class_id}", response_class=HTMLResponse)
def class_people(
    request: Request,
    class_id: int,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return _render_class_page(
        request,
        user,
        db,
        class_id,
        message=request.query_params.get("message"),
    )


@router.post("/coordinator/classes/{class_id}/edit")
def edit_class(
    request: Request,
    class_id: int,
    name: str = Form(),
    grade: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    try:
        ClassroomService().edit_classroom(db, user, class_id, name, grade)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_class_page(
            request,
            user,
            db,
            class_id,
            errors={"class": str(exc)},
            form={"class_name": name, "grade": grade},
        )
    return redirect_with_message(
        _class_url(request, class_id), "Class details updated.", anchor="class-settings"
    )


@router.post("/coordinator/classes/{class_id}/archive")
def archive_class(
    request: Request,
    class_id: int,
    confirmed: bool = Form(False),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    _check_class(request, db, user, class_id)
    if not confirmed:
        raise ValidationError("Confirm that you want to archive the class.")
    ClassroomService().archive_classroom(db, user, class_id)
    db.commit()
    return redirect_with_message(
        _class_url(request, class_id), "Class archived. Its records are available in Classwork."
    )


def _enrollment_token(request: Request) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        request.app.state.settings.session_secret, salt="coordinator-enrollment-v1"
    )


@router.post(
    "/coordinator/classes/{class_id}/students/{student_id}/membership/preview",
    response_class=HTMLResponse,
)
def preview_student_membership(
    request: Request,
    class_id: int,
    student_id: int,
    destination_id: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    _check_class(request, db, user, class_id)
    if destination_id != "remove" and (
        not destination_id.isascii() or not destination_id.isdigit() or len(destination_id) > 18
    ):
        raise ValidationError("Choose a destination class or remove the student from this class.")
    destination = None if destination_id == "remove" else int(destination_id)
    source, membership, target = ClassroomService().student_class_change(
        db, user, class_id, student_id, destination
    )
    token = _enrollment_token(request).dumps(
        {
            "actor": user.id,
            "school": source.school_id,
            "class": class_id,
            "student": student_id,
            "membership": membership.id,
            "joined_at": aware_utc(membership.created_at).isoformat(),
            "destination": destination,
        }
    )
    return render(
        request,
        "coordinator/enrollment.html",
        school=db.get(School, source.school_id),
        classroom=source,
        student=db.get(User, student_id),
        destination=target,
        token=token,
    )


@router.post("/coordinator/classes/{class_id}/students/{student_id}/membership")
def change_student_membership(
    request: Request,
    class_id: int,
    student_id: int,
    change_token: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    classroom = _check_class(request, db, user, class_id)
    try:
        plan = _enrollment_token(request).loads(change_token, max_age=15 * 60)
    except BadSignature as exc:
        raise ValidationError(
            "This enrollment preview expired or is invalid. Open the class and review the change again."
        ) from exc
    if (plan["actor"], plan["school"], plan["class"], plan["student"]) != (
        user.id,
        classroom.school_id,
        class_id,
        student_id,
    ):
        raise AuthorizationError("Resource is outside your authorized scope")
    destination = plan["destination"]
    ClassroomService().change_student_class(
        db,
        user,
        class_id,
        student_id,
        destination,
        expected_membership_id=plan["membership"],
        expected_joined_at=plan.get("joined_at", ""),
    )
    db.commit()
    message = (
        "Student transferred. Their work remains in the original class."
        if destination
        else "Student removed from this class. Their work was preserved."
    )
    return redirect_with_message(
        _class_url(request, destination or class_id), message, anchor="students"
    )


@router.post("/teacher/classes/{class_id}/connect-telegram", response_class=HTMLResponse)
def connect_teacher_telegram(
    request: Request,
    class_id: int,
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    classroom = _check_class(request, db, user, class_id)
    try:
        link = request.app.state.telegram_link_service.create_teacher_link(db, user, class_id)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_class_page(request, user, db, class_id, errors={"telegram": str(exc)})
    response = render(
        request,
        "teacher/connect_telegram.html",
        user=user,
        link=link,
        class_id=class_id,
        classroom=classroom,
        csrf_token=csrf(request),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/coordinator/classes/{class_id}/students")
@router.post("/teacher/classes/{class_id}/students")
def add_student(
    request: Request,
    class_id: int,
    name: str = Form(),
    email: str = Form(),
    csrf_token: str = Form(),
    temporary_password: str = Form(default=""),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    _check_class(request, db, user, class_id)
    try:
        student = ClassroomService().add_student(
            db, user, class_id, name, email, temporary_password
        )
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_class_page(
            request,
            user,
            db,
            class_id,
            errors={"student": str(exc)},
            form={"name": name, "email": email},
        )
    return redirect_with_message(
        _class_url(request, class_id),
        f"{student.name} is enrolled. You can generate their Telegram link below.",
        anchor=f"student-{student.id}",
    )


@router.post("/coordinator/classes/{class_id}/students/{student_id}/telegram-link")
@router.post("/teacher/classes/{class_id}/students/{student_id}/telegram-link")
def create_telegram_link(
    request: Request,
    class_id: int,
    student_id: int,
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    _check_class(request, db, user, class_id)
    try:
        link = request.app.state.telegram_link_service.create_link(db, user, class_id, student_id)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_class_page(request, user, db, class_id, errors={"telegram": str(exc)})
    return _render_class_page(
        request,
        user,
        db,
        class_id,
        connection_link=link,
        link_student_id=student_id,
        message="Secure Telegram link generated. Copy it before leaving this page.",
    )


@router.post("/coordinator/classes/{class_id}/students/{student_id}/telegram-disconnect")
@router.post("/teacher/classes/{class_id}/students/{student_id}/telegram-disconnect")
def disconnect_telegram(
    request: Request,
    class_id: int,
    student_id: int,
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    _check_class(request, db, user, class_id)
    request.app.state.telegram_link_service.disconnect(db, user, class_id, student_id)
    db.commit()
    return redirect_with_message(
        _class_url(request, class_id), "Telegram disconnected.", anchor=f"student-{student_id}"
    )


@router.post("/coordinator/classes/{class_id}/invites")
@router.post("/teacher/classes/{class_id}/invites")
def create_invite(
    request: Request,
    class_id: int,
    expires_in_days: int = Form(default=7),
    max_uses: int = Form(default=20),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    _check_class(request, db, user, class_id)
    try:
        invite = ClassroomService().create_invite(db, user, class_id, expires_in_days, max_uses)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_class_page(
            request,
            user,
            db,
            class_id,
            errors={"invite": str(exc)},
            form={"expires_in_days": expires_in_days, "max_uses": max_uses},
        )
    return redirect_with_message(
        _class_url(request, class_id),
        f"Invite {invite.code} created. Share it with enrolled students.",
        anchor="invites",
    )


@router.post("/coordinator/invites/{invite_id}/disable")
@router.post("/teacher/invites/{invite_id}/disable")
def disable_invite(
    request: Request,
    invite_id: int,
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    invite = db.get(Invite, invite_id)
    if not invite:
        raise NotFoundError("Invite not found")
    _check_class(request, db, user, invite.classroom_id)
    invite = ClassroomService().disable_invite(db, user, invite_id)
    db.commit()
    return redirect_with_message(
        _class_url(request, invite.classroom_id),
        "Invite disabled. Existing class members keep their access.",
        anchor="invites",
    )
