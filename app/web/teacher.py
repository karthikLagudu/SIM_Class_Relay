from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.auth.security import validate_csrf
from app.database import get_db
from app.exceptions import InvalidStateTransition, LLMInterpretationError, ValidationError
from app.models import (
    ActivityEvent,
    Assignment,
    AssignmentTarget,
    ClassMembership,
    Classroom,
    Feedback,
    Invite,
    NotificationDelivery,
    Reminder,
    School,
    SchoolMembership,
    StudentAssignmentState,
    Submission,
    TelegramLinkToken,
    User,
)
from app.reminders.policy import aware_utc
from app.services.authorization import (
    list_teacher_classes,
    require_teacher_assignment,
    require_teacher_class,
    require_teacher_submission,
)
from app.services.classroom import ClassroomService
from app.services.risk import RiskService
from app.services.submission import FeedbackService
from app.services.telegram_link import TelegramConnectionLink
from app.web.files import INLINE_IMAGE_TYPES
from app.web.helpers import (
    csrf,
    operation_key,
    parse_local_deadline,
    redirect_with_message,
    render,
    web_user,
)

router = APIRouter()


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
    classroom = require_teacher_class(db, user, class_id)
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
    assignments = list(
        db.scalars(
            select(Assignment)
            .where(Assignment.classroom_id == class_id)
            .order_by(Assignment.due_at)
        )
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
        "teacher/class.html",
        user=user,
        classroom=classroom,
        students=students,
        assignments=assignments,
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
        assignment_key=operation_key("assignment"),
        message=message,
    )
    if connection_link:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _teaching_schools(db: Session, user: User) -> list[School]:
    return list(
        db.scalars(
            select(School)
            .join(SchoolMembership, SchoolMembership.school_id == School.id)
            .where(
                SchoolMembership.user_id == user.id,
                SchoolMembership.role.in_(["teacher", "coordinator"]),
            )
            .distinct()
            .order_by(School.name)
        )
    )


@router.get("/teacher", response_class=HTMLResponse)
def teacher_dashboard(
    request: Request,
    class_id: int | None = None,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return _render_dashboard(request, user, db, class_id=class_id)


def _render_dashboard(
    request: Request, user: User, db: Session, *, class_id=None, class_error=None, form=None
):
    classes = list_teacher_classes(db, user)
    schools = _teaching_schools(db, user)
    if not classes and not schools:
        raise HTTPException(403, "Use a teacher or coordinator account to view classwork.")
    active = (
        require_teacher_class(db, user, class_id)
        if class_id is not None
        else next(iter(classes), None)
    )
    assignments, risks, reviews, deliveries = [], [], [], []
    if active:
        assignments = list(
            db.scalars(
                select(Assignment)
                .where(Assignment.classroom_id == active.id)
                .order_by(Assignment.due_at, Assignment.id)
            )
        )
        risks = RiskService().for_actor(db, user, classroom_id=active.id)
        latest = select(func.max(Submission.id)).group_by(
            Submission.assignment_id, Submission.student_id
        )
        reviews = db.execute(
            select(Submission, Assignment, User)
            .join(Assignment, Assignment.id == Submission.assignment_id)
            .join(User, User.id == Submission.student_id)
            .join(
                StudentAssignmentState,
                and_(
                    StudentAssignmentState.assignment_id == Submission.assignment_id,
                    StudentAssignmentState.student_id == Submission.student_id,
                ),
            )
            .where(
                Assignment.classroom_id == active.id,
                Assignment.status == "assigned",
                StudentAssignmentState.status == "submitted",
                Submission.id.in_(latest),
            )
            .order_by(Submission.created_at, Submission.id)
        ).all()
        deliveries = list(
            db.scalars(
                select(NotificationDelivery)
                .join(Assignment, Assignment.id == NotificationDelivery.assignment_id)
                .where(
                    Assignment.classroom_id == active.id,
                    NotificationDelivery.classroom_id == active.id,
                    NotificationDelivery.school_id == active.school_id,
                )
                .order_by(NotificationDelivery.id.desc())
                .limit(8)
            )
        )
    now = datetime.now(UTC)
    due_soon = [
        item
        for item in assignments
        if item.status == "assigned" and now <= aware_utc(item.due_at) <= now + timedelta(days=1)
    ]
    return render(
        request,
        "teacher/dashboard.html",
        status_code=422 if class_error else 200,
        user=user,
        classes=classes,
        schools=schools,
        active=active,
        school=db.get(School, active.school_id) if active else None,
        assignments=assignments,
        due_soon=due_soon,
        risks=risks,
        reviews=reviews,
        blocked_count=len({item.student_id for item in risks if "blocked" in item.reasons}),
        deliveries=deliveries,
        csrf_token=csrf(request),
        class_error=class_error,
        form=form or {},
        message=request.query_params.get("message"),
    )


@router.get("/teacher/classes/{class_id}", response_class=HTMLResponse)
def teacher_class(
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


@router.post("/teacher/classes/{class_id}/connect-telegram", response_class=HTMLResponse)
def connect_teacher_telegram(
    request: Request,
    class_id: int,
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    try:
        link = request.app.state.telegram_link_service.create_teacher_link(db, user, class_id)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_class_page(request, user, db, class_id, errors={"telegram": str(exc)})
    response = render(
        request, "teacher/connect_telegram.html", user=user, link=link, class_id=class_id
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/teacher/classes")
def create_classroom(
    request: Request,
    school_id: int = Form(),
    name: str = Form(),
    grade: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    try:
        classroom = ClassroomService().create_classroom(db, user, school_id, name, grade)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_dashboard(
            request,
            user,
            db,
            class_error=str(exc),
            form={"school_id": school_id, "name": name, "grade": grade},
        )
    return redirect_with_message(
        f"/teacher/classes/{classroom.id}",
        "Class created. Add students to get started.",
        anchor="students",
    )


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
        f"/teacher/classes/{class_id}",
        f"{student.name} is enrolled. You can generate their Telegram link below.",
        anchor=f"student-{student.id}",
    )


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
    request.app.state.telegram_link_service.disconnect(db, user, class_id, student_id)
    db.commit()
    return RedirectResponse(
        f"/teacher/classes/{class_id}?message=Telegram+disconnected", status_code=303
    )


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
        f"/teacher/classes/{class_id}",
        f"Invite {invite.code} created. Share it with enrolled students.",
        anchor="invites",
    )


@router.post("/teacher/invites/{invite_id}/disable")
def disable_invite(
    request: Request,
    invite_id: int,
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    invite = ClassroomService().disable_invite(db, user, invite_id)
    db.commit()
    return redirect_with_message(
        f"/teacher/classes/{invite.classroom_id}",
        "Invite disabled. Existing class members keep their access.",
        anchor="invites",
    )


def _render_assignment_form(
    request, db, user, classroom, school, *, entry="natural", form=None, errors=None
):
    students = list(
        db.scalars(
            select(User)
            .join(ClassMembership, ClassMembership.user_id == User.id)
            .where(ClassMembership.classroom_id == classroom.id, ClassMembership.role == "student")
            .order_by(User.name)
        )
    )
    return render(
        request,
        "teacher/new_assignment.html",
        status_code=422 if errors else 200,
        user=user,
        classroom=classroom,
        school=school,
        students=students,
        entry=entry,
        form=form or {},
        errors=errors or {},
        csrf_token=csrf(request),
        idempotency_key=(form or {}).get("idempotency_key") or operation_key("assignment"),
        selected_ids=(form or {}).get("student_ids", [student.id for student in students]),
    )


@router.get("/teacher/classes/{class_id}/assignments/new", response_class=HTMLResponse)
def new_assignment(
    request: Request,
    class_id: int,
    entry: str = "natural",
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    classroom = require_teacher_class(db, user, class_id)
    return _render_assignment_form(
        request,
        db,
        user,
        classroom,
        db.get(School, classroom.school_id),
        entry="manual" if entry == "manual" else "natural",
    )


@router.post("/teacher/assignments")
def create_assignment(
    request: Request,
    classroom_id: int = Form(),
    idempotency_key: str = Form(),
    csrf_token: str = Form(),
    natural_text: str = Form(default=""),
    title: str = Form(default=""),
    instructions: str = Form(default=""),
    due_at: str = Form(default=""),
    entry: str = Form(default="natural"),
    student_ids: list[int] = Form(default=[]),
    targeting_mode: str = Form(default="all"),
    draft: bool = Form(default=False),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    classroom = require_teacher_class(db, user, classroom_id)
    school = db.get(School, classroom.school_id)
    values = {
        "natural_text": natural_text,
        "title": title,
        "instructions": instructions,
        "due_at": due_at,
        "student_ids": student_ids,
        "idempotency_key": idempotency_key,
    }
    errors = {}
    if targeting_mode == "selected" and not student_ids:
        errors["recipients"] = "Choose at least one student."
    if entry == "manual":
        if not title.strip() or len(title) > 240:
            errors["title"] = "Enter a title of up to 240 characters."
        if not instructions.strip() or len(instructions) > 5000:
            errors["instructions"] = "Enter instructions of up to 5,000 characters."
        try:
            deadline = parse_local_deadline(due_at, school.timezone)
            if deadline <= datetime.now(UTC):
                errors["due_at"] = "Choose a future deadline."
        except ValidationError as exc:
            errors["due_at"] = str(exc)
    elif not natural_text.strip() or len(natural_text) > 5000:
        errors["natural_text"] = (
            "Describe the work and include a deadline, in up to 5,000 characters."
        )
    if errors:
        return _render_assignment_form(
            request, db, user, classroom, school, entry=entry, form=values, errors=errors
        )
    try:
        if entry != "manual":
            try:
                parsed = request.app.state.llm_service.assignment(
                    db, user.id, natural_text, school.timezone, datetime.now(UTC)
                )
            except LLMInterpretationError as exc:
                db.commit()
                return _render_assignment_form(
                    request,
                    db,
                    user,
                    classroom,
                    school,
                    entry=entry,
                    form=values,
                    errors={"natural_text": str(exc)},
                )
            title, instructions, deadline = parsed.title, parsed.instructions, parsed.due_at
        assignment = request.app.state.assignment_service.create(
            db,
            user,
            classroom_id,
            title,
            instructions,
            deadline,
            school.timezone,
            idempotency_key,
            student_ids if targeting_mode == "selected" else None,
            draft=draft,
        )
        db.commit()
    except (ValidationError, IntegrityError, OperationalError) as exc:
        db.rollback()
        error = (
            str(exc)
            if isinstance(exc, ValidationError)
            else "The assignment could not be saved. Your entries are still here. Try saving again."
        )
        return _render_assignment_form(
            request,
            db,
            user,
            classroom,
            school,
            entry=entry,
            form=values,
            errors={"general": error},
        )
    message = (
        "Draft saved. Review the recipients, instructions, and deadline before publishing."
        if assignment.status == "draft"
        else "Assignment published. Check Notifications for delivery updates."
    )
    return redirect_with_message(f"/teacher/assignments/{assignment.id}", message)


@router.post("/teacher/assignments/{assignment_id}/publish")
def publish_assignment(
    request: Request,
    assignment_id: int,
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    try:
        request.app.state.assignment_service.publish(db, user, assignment_id)
        db.commit()
    except (ValidationError, IntegrityError, OperationalError) as exc:
        db.rollback()
        error = (
            str(exc)
            if isinstance(exc, ValidationError)
            else "Publishing could not be confirmed. Check the draft status and try again."
        )
        return _render_assignment(request, user, db, assignment_id, errors={"publish": error})
    return redirect_with_message(
        f"/teacher/assignments/{assignment_id}",
        "Assignment published. Students can open it now. Check Notifications for Telegram delivery.",
    )


@router.get("/teacher/assignments/{assignment_id}", response_class=HTMLResponse)
def teacher_assignment(
    request: Request,
    assignment_id: int,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return _render_assignment(request, user, db, assignment_id)


def _render_assignment(request, user, db, assignment_id, *, errors=None, form=None):
    errors, form = errors or {}, form or {}
    assignment = require_teacher_assignment(db, user, assignment_id)
    classroom = db.get(Classroom, assignment.classroom_id)
    recipients = list(
        db.scalars(
            select(User)
            .join(AssignmentTarget, AssignmentTarget.student_id == User.id)
            .where(AssignmentTarget.assignment_id == assignment_id)
            .order_by(User.name)
        )
    )
    rows = db.execute(
        select(StudentAssignmentState, User)
        .join(User, User.id == StudentAssignmentState.student_id)
        .where(StudentAssignmentState.assignment_id == assignment_id)
    ).all()
    submissions = list(
        db.scalars(
            select(Submission)
            .where(Submission.assignment_id == assignment_id)
            .order_by(Submission.id.desc())
        )
    )
    feedback = list(
        db.scalars(
            select(Feedback)
            .where(Feedback.assignment_id == assignment_id)
            .order_by(Feedback.created_at.desc())
        )
    )
    reminders = list(
        db.scalars(
            select(Reminder)
            .where(Reminder.assignment_id == assignment_id)
            .order_by(Reminder.scheduled_for)
        )
    )
    deliveries = list(
        db.scalars(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.assignment_id == assignment_id,
                NotificationDelivery.classroom_id == assignment.classroom_id,
                NotificationDelivery.school_id == assignment.school_id,
            )
            .order_by(NotificationDelivery.created_at.desc())
        )
    )
    delivery_keys = {item.idempotency_key: item for item in deliveries if item.idempotency_key}
    feedback_outcomes = {
        event.entity_id: event.metadata_json.get("complete")
        for event in db.scalars(
            select(ActivityEvent).where(
                ActivityEvent.entity_type == "feedback",
                ActivityEvent.event_type == "feedback_sent",
                ActivityEvent.classroom_id == assignment.classroom_id,
                ActivityEvent.entity_id.in_([review.id for review in feedback]),
            )
        )
    }
    latest = {}
    for submission in submissions:
        latest.setdefault(submission.student_id, submission.id)
    reviewable = {
        latest[state.student_id]
        for state, _ in rows
        if state.status == "submitted" and state.student_id in latest
    }
    return render(
        request,
        "teacher/assignment.html",
        status_code=422 if errors else 200,
        user=user,
        assignment=assignment,
        classroom=classroom,
        recipients=recipients,
        student_names={student.id: student.name for student in recipients},
        student_states={state.student_id: state for state, _ in rows},
        delivery_keys=delivery_keys,
        feedback_outcomes=feedback_outcomes,
        errors=errors,
        form=form,
        state_rows=rows,
        submissions=submissions,
        feedback=feedback,
        reviewable=reviewable,
        inline_image_types=INLINE_IMAGE_TYPES,
        reminders=reminders,
        deliveries=deliveries,
        csrf_token=csrf(request),
        deadline_key=operation_key("deadline"),
        instructions_key=operation_key("instructions"),
        cancel_key=operation_key("cancel"),
        feedback_keys={item.id: operation_key("feedback") for item in submissions},
        message=request.query_params.get("message"),
    )


@router.post("/teacher/assignments/{assignment_id}/deadline")
def update_deadline(
    request: Request,
    assignment_id: int,
    due_at: str = Form(),
    idempotency_key: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    assignment = require_teacher_assignment(db, user, assignment_id)
    try:
        deadline = parse_local_deadline(due_at, assignment.timezone)
        changed = deadline != aware_utc(assignment.due_at)
        request.app.state.assignment_service.update_deadline(
            db, user, assignment_id, deadline, idempotency_key
        )
        db.commit()
    except (ValidationError, IntegrityError, OperationalError) as exc:
        db.rollback()
        error = (
            str(exc)
            if isinstance(exc, ValidationError)
            else "The deadline could not be saved. Try updating it again."
        )
        return _render_assignment(
            request,
            user,
            db,
            assignment_id,
            errors={"due_at": error},
            form={"due_at": due_at, "deadline_key": idempotency_key},
        )
    if not changed:
        message = "The deadline is already set to that time. The reminder schedule is unchanged."
    elif assignment.status == "draft":
        message = "Draft deadline updated. Reminders will be scheduled when you publish."
    else:
        message = "Deadline updated. The old reminder schedule was replaced. Check Notifications for delivery updates."
    return redirect_with_message(f"/teacher/assignments/{assignment_id}", message)


@router.post("/teacher/assignments/{assignment_id}/instructions")
def update_instructions(
    request: Request,
    assignment_id: int,
    instructions: str = Form(),
    idempotency_key: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    try:
        if len(instructions) > 5000:
            raise ValidationError("Keep instructions within 5,000 characters.")
        request.app.state.assignment_service.clarify(
            db, user, assignment_id, instructions, idempotency_key
        )
        db.commit()
    except (ValidationError, IntegrityError, OperationalError) as exc:
        db.rollback()
        error = (
            str(exc)
            if isinstance(exc, ValidationError)
            else "The instructions could not be saved. Try updating them again."
        )
        return _render_assignment(
            request,
            user,
            db,
            assignment_id,
            errors={"instructions": error},
            form={"instructions": instructions, "instructions_key": idempotency_key},
        )
    return redirect_with_message(f"/teacher/assignments/{assignment_id}", "Instructions updated.")


@router.post("/teacher/assignments/{assignment_id}/cancel")
def cancel_assignment(
    request: Request,
    assignment_id: int,
    idempotency_key: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    assignment = require_teacher_assignment(db, user, assignment_id)
    was_draft = assignment.status == "draft"
    request.app.state.assignment_service.cancel(db, user, assignment_id, idempotency_key)
    db.commit()
    message = (
        "Draft cancelled. It was never shared with students."
        if was_draft
        else "Assignment cancelled. Future reminders have stopped. Check Notifications for delivery updates."
    )
    return redirect_with_message(f"/teacher/assignments/{assignment_id}", message)


@router.post("/teacher/submissions/{submission_id}/feedback")
def create_feedback(
    request: Request,
    submission_id: int,
    message: str = Form(),
    outcome: str = Form(),
    idempotency_key: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    submission = require_teacher_submission(db, user, submission_id)
    assignment_id = submission.assignment_id
    try:
        if outcome not in {"revision", "complete"}:
            raise ValidationError("Choose Request revision or Mark complete.")
        if not message.strip() or len(message) > 5000:
            raise ValidationError("Enter feedback of up to 5,000 characters.")
        feedback = FeedbackService(request.app.state.notification_service).create(
            db,
            user,
            submission_id,
            message,
            complete=outcome == "complete",
            idempotency_key=idempotency_key,
        )
        event = db.scalar(
            select(ActivityEvent).where(
                ActivityEvent.entity_type == "feedback",
                ActivityEvent.entity_id == feedback.id,
                ActivityEvent.event_type == "feedback_sent",
            )
        )
        db.commit()
    except (ValidationError, InvalidStateTransition, IntegrityError, OperationalError) as exc:
        db.rollback()
        error = (
            "The feedback could not be saved. Your note is below. Try again after checking the latest submission."
            if isinstance(exc, (IntegrityError, OperationalError))
            else str(exc)
        )
        return _render_assignment(
            request,
            user,
            db,
            assignment_id,
            errors={"feedback": error},
            form={
                "submission_id": submission_id,
                "feedback": message,
                "feedback_key": idempotency_key,
            },
        )
    complete = event.metadata_json.get("complete") if event else None
    saved = {True: "Marked complete.", False: "Revision requested."}.get(
        complete, "Feedback saved."
    )
    return redirect_with_message(
        f"/teacher/assignments/{assignment_id}",
        f"{saved} The note is visible to the student. Check Telegram delivery below.",
        anchor=f"submission-{submission_id}",
    )


@router.post("/teacher/reminders/run")
def run_reminders(
    request: Request,
    csrf_token: str = Form(),
    class_id: int | None = Form(default=None),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    classroom_ids = {item.id for item in list_teacher_classes(db, user)}
    if not classroom_ids:
        raise HTTPException(403, "Teacher access required")
    if class_id is not None:
        require_teacher_class(db, user, class_id)
        classroom_ids = {class_id}
    reminders = request.app.state.reminder_service.run(db, classroom_ids=classroom_ids)
    db.commit()
    message = (
        f"Processed {len(reminders)} reminder jobs. Check Telegram delivery below."
        if reminders
        else "No reminder jobs are due right now."
    )
    return redirect_with_message(
        f"/teacher?class_id={class_id}" if class_id else "/teacher", message
    )


@router.get("/api/teacher/classes/{class_id}")
def api_teacher_class(class_id: int, user: User = Depends(web_user), db: Session = Depends(get_db)):
    item = require_teacher_class(db, user, class_id)
    return {"id": item.id, "name": item.name, "school_id": item.school_id}
