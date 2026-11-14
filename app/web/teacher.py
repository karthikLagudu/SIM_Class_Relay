from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth.security import validate_csrf
from app.database import get_db
from app.exceptions import (
    InvalidStateTransition,
    LLMInterpretationError,
    NotFoundError,
    ValidationError,
)
from app.llm.speech import TranscriptionError
from app.models import (
    ActivityEvent,
    Assignment,
    AssignmentTarget,
    ClassMembership,
    Classroom,
    Feedback,
    NotificationDelivery,
    Reminder,
    School,
    StudentAssignmentState,
    Submission,
    User,
)
from app.reminders.policy import aware_utc
from app.services.assignment_draft import class_students, resolve_natural_draft
from app.services.authorization import (
    list_teacher_classes,
    require_active_class,
    require_coordinator_class,
    require_staff_assignment,
    require_staff_submission,
    require_teacher_class,
)
from app.services.risk import RiskService
from app.services.submission import FeedbackService
from app.services.submission_history import feedback_outcomes
from app.services.workload import pending_reviews
from app.web.files import INLINE_IMAGE_TYPES
from app.web.helpers import (
    assignment_path,
    coordinator_school,
    csrf,
    operation_key,
    parse_local_deadline,
    redirect_with_message,
    render,
    web_user,
)
from app.web.submissions import render_submission

router = APIRouter()
MAX_RECORDING_BYTES = 5 * 1024 * 1024
RECORDING_EXTENSIONS = {"audio/webm": "webm", "audio/mp4": "m4a", "audio/ogg": "ogg"}
ClassworkView = Literal["overview", "attention", "help", "due"]


@router.get("/teacher", response_class=HTMLResponse)
def teacher_dashboard(
    request: Request,
    class_id: str = "",
    view: ClassworkView = "overview",
    page: int = Query(1, ge=1),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    if "teacher" not in request.state.workspace_roles:
        raise HTTPException(403, "A teaching membership is required to open this workspace.")
    try:
        selected_id = int(class_id) if class_id else None
    except ValueError as exc:
        raise ValidationError("Choose one of your assigned classes.") from exc
    active = require_teacher_class(db, user, selected_id) if selected_id is not None else None
    return _render_classwork(
        request, user, db, active, request.state.teaching_classes, request.state.teaching_schools,
        view, page,
    )


def _workspace(request: Request) -> str:
    return "coordinator" if request.url.path.startswith("/coordinator/") else "teacher"


def _published_message(request: Request) -> str:
    if request.app.state.settings.telegram_mode == "log":
        return "Assignment published. This demo simulates Telegram delivery."
    return "Assignment published. Check Notifications for Telegram delivery."


def _staff_class(request: Request, db: Session, user: User, class_id: int):
    require = (
        require_coordinator_class if _workspace(request) == "coordinator" else require_teacher_class
    )
    classroom = require(db, user, class_id)
    if _workspace(request) == "coordinator":
        coordinator_school(request, db, user, classroom.school_id)
    return classroom


@router.get("/coordinator/classes/{class_id}/work", response_class=HTMLResponse)
def coordinator_classwork(
    request: Request,
    class_id: int,
    view: ClassworkView = "overview",
    page: int = Query(1, ge=1),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    active = require_coordinator_class(db, user, class_id)
    return _render_classwork(
        request, user, db, active, [], {active.school_id: db.get(School, active.school_id)}, view, page
    )


def _render_classwork(request, user, db, active, classes, schools, view, page):
    assignments, risks, reviews, deliveries = [], [], [], []
    scope = [active] if active else classes
    class_ids = [item.id for item in scope]
    open_ids = [item.id for item in scope if item.archived_at is None]
    if class_ids and view in {"overview", "due"}:
        assignments = list(
            db.scalars(
                select(Assignment)
                .where(Assignment.classroom_id.in_(class_ids))
                .order_by(Assignment.due_at, Assignment.id)
            )
        )
    if open_ids and view != "due":
        risks = RiskService().for_classes(db, open_ids)
    if open_ids and view == "overview":
        reviews = db.execute(pending_reviews(open_ids)).all()
    if active and view == "overview":
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
        if item.classroom_id in open_ids
        and item.status == "assigned"
        and now <= aware_utc(item.due_at) <= now + timedelta(days=1)
    ]
    help_requests = [item for item in risks if "blocked" in item.reasons]
    attention = sorted(
        risks,
        key=lambda item: (
            "blocked" not in item.reasons, -item.risk_score, item.student_name,
            item.assignment_id, item.student_id,
        ),
    )
    items = {"attention": attention, "help": help_requests, "due": due_soon}.get(view, [])
    page_size = 12
    page_count = max(1, (len(items) + page_size - 1) // page_size)
    page = min(page, page_count)
    return render(
        request,
        "teacher/dashboard.html",
        user=user,
        classes=classes,
        schools=schools,
        classroom_names={item.id: item.name for item in scope},
        active=active,
        coordinating=_workspace(request) == "coordinator",
        school=schools[active.school_id] if active else None,
        assignments=assignments,
        due_soon=due_soon,
        risks=attention,
        reviews=reviews,
        blocked_count=len({item.student_id for item in help_requests}),
        view=view,
        items=items[(page - 1) * page_size : page * page_size],
        item_count=len(items),
        page=page,
        page_count=page_count,
        deliveries=deliveries,
        csrf_token=csrf(request),
        message=request.query_params.get("message"),
    )


def _render_assignment_form(
    request, db, user, classroom, school, *, entry="natural", form=None, errors=None
):
    students = class_students(db, classroom.id)
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
        speech_available=request.app.state.speech_provider is not None,
    )


@router.get("/coordinator/classes/{class_id}/assignments/new", response_class=HTMLResponse)
@router.get("/teacher/classes/{class_id}/assignments/new", response_class=HTMLResponse)
def new_assignment(
    request: Request,
    class_id: int,
    entry: str = "natural",
    next_class_id: int | None = None,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    classroom = require_active_class(_staff_class(request, db, user, class_id))
    if next_class_id is not None:
        destination = require_active_class(_staff_class(request, db, user, next_class_id))
        return RedirectResponse(
            f"/{_workspace(request)}/classes/{destination.id}/assignments/new", status_code=303
        )
    return _render_assignment_form(
        request,
        db,
        user,
        classroom,
        db.get(School, classroom.school_id),
        entry="manual" if entry == "manual" else "natural",
    )


@router.post("/teacher/classes/{class_id}/assignments/transcribe")
async def transcribe_assignment(
    request: Request,
    class_id: int,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, request.headers.get("x-csrf-token", ""))
    require_active_class(require_teacher_class(db, user, class_id))
    provider = request.app.state.speech_provider
    if provider is None:
        raise HTTPException(503, "Dictation is unavailable. You can still type the assignment.")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    extension = RECORDING_EXTENSIONS.get(content_type)
    if extension is None:
        raise HTTPException(
            415, "This recording format is unsupported. Type the work or try another browser."
        )
    audio = bytearray()
    async for chunk in request.stream():
        if len(audio) + len(chunk) > MAX_RECORDING_BYTES:
            raise HTTPException(413, "The recording is too large. Try a shorter recording.")
        audio.extend(chunk)
    if not audio:
        raise HTTPException(422, "The recording is empty. Try recording again.")
    signature = {
        "audio/webm": audio[:4] == b"\x1a\x45\xdf\xa3",
        "audio/ogg": audio[:4] == b"OggS",
        "audio/mp4": audio[4:8] == b"ftyp",
    }
    if not signature[content_type]:
        raise HTTPException(415, "The recording could not be read. Try recording again or type the work.")
    try:
        transcript = await run_in_threadpool(
            provider.transcribe, bytes(audio), f"assignment.{extension}", content_type
        )
    except TranscriptionError as exc:
        raise HTTPException(502, "Transcription failed. Try recording again or type the work.") from exc
    if not transcript or not transcript.strip():
        raise HTTPException(422, "No speech was recognized. Try again or type the work.")
    if len(transcript) > 5000:
        raise HTTPException(422, "The transcript is too long. Try a shorter recording.")
    return {"text": transcript}


@router.post("/coordinator/assignments")
@router.post("/teacher/assignments")
def create_assignment(
    request: Request,
    classroom_id: int = Form(),
    idempotency_key: str = Form(),
    csrf_token: str = Form(),
    natural_text: str = Form(default=""),
    source_transcript: str = Form(default=""),
    title: str = Form(default=""),
    instructions: str = Form(default=""),
    due_at: str = Form(default=""),
    entry: str = Form(default="natural"),
    student_ids: list[int] = Form(default=[]),
    targeting_mode: str = Form(default="all"),
    use_manual: bool = Form(default=False),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    classroom = require_active_class(_staff_class(request, db, user, classroom_id))
    school = db.get(School, classroom.school_id)
    values = {
        "natural_text": natural_text,
        "source_transcript": source_transcript[:5000],
        "title": title,
        "instructions": instructions,
        "due_at": due_at,
        "student_ids": student_ids,
        "idempotency_key": idempotency_key,
    }
    if use_manual and entry != "manual":
        values["instructions"] = natural_text
        return _render_assignment_form(
            request, db, user, classroom, school, entry="manual", form=values
        )
    errors = {}
    if len(source_transcript) > 5000:
        errors["general"] = "Use a transcript of up to 5,000 characters before saving the draft."
    targets = student_ids if targeting_mode == "selected" else None
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
                now = datetime.now(UTC)
                parsed = request.app.state.llm_service.assignment(
                    db, user.id, natural_text, school.timezone, now
                )
                deadline, targets = resolve_natural_draft(
                    parsed,
                    natural_text,
                    classroom,
                    school,
                    class_students(db, classroom.id),
                    targets,
                    now,
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
            title, instructions = parsed.title, parsed.instructions
        assignment = request.app.state.assignment_service.create(
            db,
            user,
            classroom_id,
            title,
            instructions,
            deadline,
            school.timezone,
            idempotency_key,
            targets,
            draft=True,
            coordinator=_workspace(request) == "coordinator",
            original_request=source_transcript or natural_text or None,
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
        else _published_message(request)
    )
    return redirect_with_message(assignment_path(assignment, _workspace(request)), message)


@router.post("/coordinator/assignments/{assignment_id}/publish")
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
        assignment = request.app.state.assignment_service.publish(
            db, user, assignment_id, coordinator=_workspace(request) == "coordinator"
        )
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
        assignment_path(assignment, _workspace(request)),
        _published_message(request),
    )


@router.get(
    "/coordinator/classes/{class_id}/assignments/{assignment_id}", response_class=HTMLResponse
)
@router.get("/teacher/classes/{class_id}/assignments/{assignment_id}", response_class=HTMLResponse)
@router.get("/teacher/assignments/{assignment_id}", response_class=HTMLResponse)
def teacher_assignment(
    request: Request,
    assignment_id: int,
    class_id: int | None = None,
    edit: str = "",
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return _render_assignment(
        request, user, db, assignment_id, class_id=class_id, edit_draft=edit == "draft"
    )


@router.post("/coordinator/assignments/{assignment_id}/draft")
@router.post("/teacher/assignments/{assignment_id}/draft")
def save_draft(
    request: Request,
    assignment_id: int,
    title: str = Form(),
    instructions: str = Form(),
    due_at: str = Form(),
    csrf_token: str = Form(),
    student_ids: list[int] = Form(default=[]),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    coordinator = _workspace(request) == "coordinator"
    assignment = require_staff_assignment(
        db, user, assignment_id, coordinator=coordinator, write=True
    )
    form = {"title": title, "instructions": instructions, "due_at": due_at, "student_ids": student_ids}
    errors = {}
    if not title.strip() or len(title) > 240:
        errors["title"] = "Enter a title of up to 240 characters."
    if not instructions.strip() or len(instructions) > 5000:
        errors["instructions"] = "Enter instructions of up to 5,000 characters."
    try:
        deadline = parse_local_deadline(due_at, assignment.timezone)
        if deadline <= datetime.now(UTC):
            errors["due_at"] = "Choose a future deadline."
    except ValidationError as exc:
        errors["due_at"] = str(exc)
    if not student_ids:
        errors["recipients"] = "Choose at least one student."
    if not errors:
        try:
            assignment = request.app.state.assignment_service.edit_draft(
                db,
                user,
                assignment_id,
                title,
                instructions,
                deadline,
                student_ids,
                coordinator=coordinator,
            )
            db.commit()
        except (ValidationError, IntegrityError, OperationalError) as exc:
            db.rollback()
            errors["draft"] = (
                str(exc) if isinstance(exc, ValidationError)
                else "The draft could not be saved. Your edits are still here. Try saving again."
            )
    if errors:
        return _render_assignment(
            request, user, db, assignment_id, errors=errors, form=form, edit_draft=True
        )
    return redirect_with_message(
        assignment_path(assignment, _workspace(request)),
        "Draft updated. Review the saved details, then publish when you’re ready.",
        anchor="instructions",
    )


def _render_assignment(
    request, user, db, assignment_id, *, class_id=None, errors=None, form=None, edit_draft=False
):
    errors, form = errors or {}, form or {}
    assignment = require_staff_assignment(
        db, user, assignment_id, coordinator=_workspace(request) == "coordinator"
    )
    if class_id is not None and assignment.classroom_id != class_id:
        raise NotFoundError("Assignment not found in this class")
    classroom = db.get(Classroom, assignment.classroom_id)
    editing = edit_draft and assignment.status == "draft" and not classroom.archived_at
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
    latest = {}
    for submission in submissions:
        latest.setdefault(submission.student_id, submission.id)
    enrolled_ids = set(
        db.scalars(
            select(ClassMembership.user_id).where(
                ClassMembership.classroom_id == classroom.id,
                ClassMembership.role == "student",
            )
        )
    )
    reviewable = {
        latest[state.student_id]
        for state, _ in rows
        if not classroom.archived_at
        and assignment.status == "assigned"
        and state.status == "submitted"
        and state.student_id in latest
        and state.student_id in enrolled_ids
    }
    return render(
        request,
        "teacher/assignment.html",
        status_code=422 if errors else 200,
        user=user,
        assignment=assignment,
        classroom=classroom,
        school=db.get(School, classroom.school_id),
        author=db.get(User, assignment.created_by_user_id),
        feedback_authors={
            item.id: item.name
            for item in db.scalars(
                select(User).where(User.id.in_({item.teacher_id for item in feedback}))
            )
        },
        recipients=recipients,
        editing_draft=editing,
        draft_students=class_students(db, classroom.id) if editing else [],
        draft_recipient_ids=form.get("student_ids", [student.id for student in recipients]),
        student_names={student.id: student.name for student in recipients},
        student_states={state.student_id: state for state, _ in rows},
        delivery_keys=delivery_keys,
        feedback_outcomes=feedback_outcomes(db, assignment, [review.id for review in feedback]),
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


@router.post("/coordinator/assignments/{assignment_id}/deadline")
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
    assignment = require_staff_assignment(
        db, user, assignment_id, coordinator=_workspace(request) == "coordinator"
    )
    try:
        deadline = parse_local_deadline(due_at, assignment.timezone)
        changed = deadline != aware_utc(assignment.due_at)
        request.app.state.assignment_service.update_deadline(
            db,
            user,
            assignment_id,
            deadline,
            idempotency_key,
            coordinator=_workspace(request) == "coordinator",
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
    return redirect_with_message(assignment_path(assignment, _workspace(request)), message)


@router.post("/coordinator/assignments/{assignment_id}/instructions")
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
        assignment = request.app.state.assignment_service.clarify(
            db,
            user,
            assignment_id,
            instructions,
            idempotency_key,
            coordinator=_workspace(request) == "coordinator",
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
    return redirect_with_message(
        assignment_path(assignment, _workspace(request)), "Instructions updated."
    )


@router.post("/coordinator/assignments/{assignment_id}/cancel")
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
    assignment = require_staff_assignment(
        db, user, assignment_id, coordinator=_workspace(request) == "coordinator"
    )
    was_draft = assignment.status == "draft"
    request.app.state.assignment_service.cancel(
        db, user, assignment_id, idempotency_key, coordinator=_workspace(request) == "coordinator"
    )
    db.commit()
    message = (
        "Draft cancelled. It was never shared with students."
        if was_draft
        else "Assignment cancelled. Future reminders have stopped. Check Notifications for delivery updates."
    )
    return redirect_with_message(assignment_path(assignment, _workspace(request)), message)


@router.post("/coordinator/submissions/{submission_id}/feedback")
@router.post("/teacher/submissions/{submission_id}/feedback")
def create_feedback(
    request: Request,
    submission_id: int,
    message: str = Form(),
    outcome: str = Form(),
    idempotency_key: str = Form(),
    csrf_token: str = Form(),
    from_detail: bool = Form(False),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    submission = require_staff_submission(
        db, user, submission_id, coordinator=_workspace(request) == "coordinator", write=True
    )
    assignment_id = submission.assignment_id
    assignment = db.get(Assignment, assignment_id)
    if _workspace(request) == "coordinator":
        coordinator_school(request, db, user, assignment.school_id)
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
            coordinator=_workspace(request) == "coordinator",
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
        if from_detail:
            return render_submission(
                request,
                user,
                db,
                submission_id,
                error=error,
                message=message,
                feedback_key=idempotency_key,
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
        f"/{_workspace(request)}/submissions/{submission_id}"
        if from_detail
        else assignment_path(assignment, _workspace(request)),
        f"{saved} The note is visible to the student. Check Telegram delivery below.",
        anchor="review-heading" if from_detail else f"submission-{submission_id}",
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
