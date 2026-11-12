from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import validate_csrf
from app.database import get_db
from app.exceptions import InvalidStateTransition, NotFoundError, ValidationError
from app.models import (
    Assignment,
    Classroom,
    Feedback,
    NotificationDelivery,
    ProgressEvent,
    Reminder,
    School,
    StudentAssignmentState,
    Submission,
    User,
)
from app.reminders.policy import aware_utc
from app.services.assignment import get_student_state
from app.services.authorization import (
    class_membership_scope,
    require_active_class,
    require_student_assignment,
    require_student_class,
    require_student_submission,
    student_work_scope,
)
from app.services.progress import ProgressService
from app.services.state_machine import TRANSITIONS
from app.services.submission import SubmissionService
from app.services.submission_history import feedback_outcomes
from app.web.helpers import (
    assignment_path,
    csrf,
    operation_key,
    redirect_with_message,
    render,
    web_user,
)

router = APIRouter()


@router.get("/student", response_class=HTMLResponse)
def student_dashboard(
    request: Request,
    class_id: str = "",
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    if "student" not in request.state.workspace_roles:
        raise HTTPException(403, "A student membership is required to open this workspace.")
    try:
        selected_id = int(class_id) if class_id else None
    except ValueError as exc:
        raise ValidationError("Choose a class from your enrolled classes.") from exc
    active = require_student_class(db, user, selected_id) if selected_id is not None else None
    classes = list(
        db.scalars(
            select(Classroom)
            .where(Classroom.id.in_(class_membership_scope(user.id, "student")))
            .order_by(Classroom.name, Classroom.id)
        )
    )
    schools = {
        school.id: school
        for school in db.scalars(
            select(School).where(School.id.in_({item.school_id for item in classes}))
        )
    }
    assignments = (
        select(Assignment, StudentAssignmentState, Classroom)
        .join(StudentAssignmentState, StudentAssignmentState.assignment_id == Assignment.id)
        .join(Classroom, Classroom.id == Assignment.classroom_id)
        .where(
            StudentAssignmentState.student_id == user.id,
            Assignment.id.in_(student_work_scope(user.id)),
        )
        .order_by(Assignment.due_at, Assignment.id)
    )
    if active:
        assignments = assignments.where(Assignment.classroom_id == active.id)
    rows = db.execute(assignments).all()
    actionable = [
        row
        for row in rows
        if row[0].status != "cancelled"
        and row[2].archived_at is None
        and row[1].status not in {"submitted", "completed", "cancelled"}
    ]
    actionable.sort(key=lambda row: (row[1].status != "needs_revision", row[0].due_at))
    assignment_by_id = {assignment.id: assignment for assignment, _, _ in rows}
    feedback = list(
        db.scalars(
            select(Feedback)
            .where(
                Feedback.student_id == user.id,
                Feedback.assignment_id.in_(assignment_by_id),
            )
            .order_by(Feedback.id.desc())
            .limit(6)
        )
    )
    return render(
        request,
        "student/dashboard.html",
        user=user,
        classes=classes,
        class_names={classroom.id: classroom.name for classroom in classes},
        active=active,
        school=schools[active.school_id] if active else None,
        schools=schools,
        actionable=actionable,
        history=[row for row in rows if row not in actionable],
        feedback=feedback,
        assignments=assignment_by_id,
        csrf_token=csrf(request),
        now=datetime.now(UTC),
        message=request.query_params.get("message"),
    )


@router.get("/student/classes/{class_id}/assignments/{assignment_id}", response_class=HTMLResponse)
@router.get("/student/assignments/{assignment_id}", response_class=HTMLResponse)
def student_assignment(
    request: Request,
    assignment_id: int,
    class_id: int | None = None,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return _render_assignment(request, user, db, assignment_id, class_id=class_id)


def _render_assignment(
    request, user, db, assignment_id, *, class_id=None, errors=None, form=None, status_code=200
):
    errors, form = errors or {}, form or {}
    assignment = require_student_assignment(db, user, assignment_id)
    if class_id is not None and assignment.classroom_id != class_id:
        raise NotFoundError("Assignment not found in this class")
    state = get_student_state(db, assignment_id, user.id)
    progress = list(
        db.scalars(
            select(ProgressEvent)
            .where(
                ProgressEvent.assignment_id == assignment_id,
                ProgressEvent.student_id == user.id,
            )
            .order_by(ProgressEvent.created_at.desc())
        )
    )
    submissions = list(
        db.scalars(
            select(Submission)
            .where(Submission.assignment_id == assignment_id, Submission.student_id == user.id)
            .order_by(Submission.id.desc())
        )
    )
    feedback = list(
        db.scalars(
            select(Feedback)
            .where(Feedback.assignment_id == assignment_id, Feedback.student_id == user.id)
            .order_by(Feedback.created_at.desc())
        )
    )
    reminders = list(
        db.scalars(
            select(Reminder)
            .where(
                Reminder.assignment_id == assignment_id,
                Reminder.student_id == user.id,
                Reminder.status.in_(["sent", "skipped", "failed"]),
            )
            .order_by(Reminder.scheduled_for.desc())
        )
    )
    reminder_deliveries = {
        item.idempotency_key: item.status
        for item in db.scalars(
            select(NotificationDelivery).where(
                NotificationDelivery.assignment_id == assignment_id,
                NotificationDelivery.user_id == user.id,
                NotificationDelivery.idempotency_key.in_(
                    [f"reminder:{row.dedupe_key}" for row in reminders]
                ),
            )
        )
    }
    classroom = db.get(Classroom, assignment.classroom_id)
    open_for_work = assignment.status == "assigned" and classroom.archived_at is None
    actions = TRANSITIONS[state.status] | {state.status} if open_for_work else set()
    can_submit = open_for_work and state.status not in {
        "submitted",
        "completed",
        "cancelled",
    }
    return render(
        request,
        "student/assignment.html",
        status_code=status_code,
        classroom=classroom,
        school=db.get(School, classroom.school_id),
        actions=actions,
        can_submit=can_submit,
        errors=errors,
        form=form,
        max_upload_mb=f"{request.app.state.settings.max_upload_bytes / (1024 * 1024):g}",
        reminder_deliveries=reminder_deliveries,
        past_due=aware_utc(assignment.due_at) < datetime.now(UTC),
        user=user,
        assignment=assignment,
        state=state,
        progress=progress,
        reminders=reminders,
        submissions=submissions,
        feedback=feedback,
        feedback_outcomes=feedback_outcomes(db, assignment, [item.id for item in feedback]),
        feedback_authors=dict(
            db.execute(
                select(User.id, User.name).where(
                    User.id.in_({item.teacher_id for item in feedback})
                )
            ).all()
        ),
        csrf_token=csrf(request),
        idempotency_key=form.get("idempotency_key") or operation_key("submission"),
        message=request.query_params.get("message"),
    )


@router.post("/student/assignments/{assignment_id}/progress")
def student_progress(
    request: Request,
    assignment_id: int,
    status: str = Form(),
    message: str = Form(),
    csrf_token: str = Form(),
    progress_percent: str = Form(default=""),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    assignment = require_student_assignment(db, user, assignment_id)
    require_active_class(db.get(Classroom, assignment.classroom_id))
    values = {
        "progress_message": message,
        "progress_percent": progress_percent,
        "progress_status": status,
    }
    try:
        if status == "blocked" and not message.strip():
            raise ValidationError("Tell your teacher what you need help with.")
        if len(message) > 2000:
            raise ValidationError("Keep the update within 2,000 characters.")
        percent = None
        if progress_percent:
            if (
                not progress_percent.isascii()
                or not progress_percent.isdigit()
                or len(progress_percent) > 3
                or int(progress_percent) > 100
            ):
                raise ValidationError("Enter a whole-number percentage between 0 and 100.")
            percent = int(progress_percent)
        if status == "in_progress" and not message.strip() and percent is None:
            raise ValidationError("Enter an update or add a percentage.")
        ProgressService().update(
            db, user, assignment_id, status, message.strip() or "Progress updated", percent
        )
        db.commit()
    except (ValidationError, InvalidStateTransition) as exc:
        db.rollback()
        return _render_assignment(
            request,
            user,
            db,
            assignment_id,
            errors={"progress": str(exc)},
            form=values,
            status_code=422,
        )
    success = {
        "acknowledged": "Assignment acknowledged. You can submit when you’re ready.",
        "blocked": "Help requested. Your teacher can see your note on their dashboard.",
        "in_progress": "Progress saved.",
    }
    return redirect_with_message(
        assignment_path(assignment, "student"), success[status], anchor="next-action"
    )


@router.post("/student/assignments/{assignment_id}/submit")
async def student_submit(
    request: Request,
    assignment_id: int,
    idempotency_key: str = Form(),
    text_content: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    assignment = require_student_assignment(db, user, assignment_id)
    require_active_class(db.get(Classroom, assignment.classroom_id))
    settings = request.app.state.settings
    values = {"text_content": text_content, "idempotency_key": idempotency_key}

    def retry(message, status_code=422):
        return _render_assignment(
            request,
            user,
            db,
            assignment_id,
            errors={"submission": message},
            form=values,
            status_code=status_code,
        )

    if len(text_content) > 10000:
        return retry("Keep the written response within 10,000 characters.")
    content = stored_path = original_name = mime_type = None
    if file and file.filename:
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            return retry("That file is too large. Choose a smaller file and submit again.", 413)
        if not content:
            return retry("That file is empty. Choose a file with your work.")
        original_name = file.filename.replace("\\", "/").rsplit("/", 1)[-1][:240]
        root = settings.upload_dir.resolve()
        destination = root / uuid.uuid4().hex
        try:
            root.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                output.write(content)
        except OSError:
            return retry("The file could not be saved. Select it again and retry.", 503)
        stored_path, mime_type = str(destination), file.content_type
    if not text_content.strip() and content is None:
        return retry("Write a response or attach a file.")
    keep_file = False
    try:
        submission = SubmissionService().submit(
            db,
            user,
            assignment_id,
            idempotency_key,
            text_content=text_content,
            stored_file_path=stored_path,
            original_filename=original_name,
            mime_type=mime_type,
            content=content,
        )
        db.commit()
        keep_file = submission.stored_file_path == stored_path
    except (ValidationError, InvalidStateTransition) as exc:
        db.rollback()
        return retry(
            str(exc) + (" If you retry, select the file again." if file and file.filename else "")
        )
    finally:
        if stored_path and not keep_file:
            Path(stored_path).unlink(missing_ok=True)
    return redirect_with_message(
        assignment_path(assignment, "student"),
        "Work submitted. Your teacher or school coordinator can review it now.",
        anchor=f"submission-{submission.id}",
    )


@router.get("/api/student/assignments/{assignment_id}")
def api_student_assignment(
    assignment_id: int, user: User = Depends(web_user), db: Session = Depends(get_db)
):
    item = require_student_assignment(db, user, assignment_id)
    state = get_student_state(db, assignment_id, user.id)
    return {"id": item.id, "title": item.title, "status": state.status, "due_at": item.due_at}


@router.get("/api/student/submissions/{submission_id}")
def api_student_submission(
    submission_id: int, user: User = Depends(web_user), db: Session = Depends(get_db)
):
    item = require_student_submission(db, user, submission_id)
    return {"id": item.id, "assignment_id": item.assignment_id, "type": item.submission_type}
