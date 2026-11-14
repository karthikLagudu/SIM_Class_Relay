from __future__ import annotations

import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import validate_csrf
from app.database import get_db
from app.exceptions import AuthorizationError, NotFoundError
from app.models import (
    ActivityEvent,
    Assignment,
    ClassMembership,
    NotificationDelivery,
    ProgressEvent,
    School,
    StudentAssignmentState,
    StudentSupportReply,
    Submission,
    User,
)
from app.services.authorization import (
    require_active_class,
    require_coordinator_class,
    require_student_assignment,
    require_teacher_class,
)
from app.web.files import INLINE_IMAGE_TYPES, send_private_file
from app.web.helpers import (
    coordinator_school,
    operation_key,
    redirect_with_message,
    render,
    web_user,
)

router = APIRouter()


def support_replies(db: Session, assignment_ids: list[int], student_id: int):
    return db.execute(
        select(StudentSupportReply, User.name)
        .join(User, User.id == StudentSupportReply.author_id)
        .where(
            StudentSupportReply.assignment_id.in_(assignment_ids),
            StudentSupportReply.student_id == student_id,
        )
        .order_by(StudentSupportReply.id.desc())
    ).all()


def _profile_path(workspace: str, class_id: int, student_id: int) -> str:
    return f"/{workspace}/classes/{class_id}/students/{student_id}"


def _states(db: Session, class_id: int, student_id: int):
    return db.execute(
        select(Assignment, StudentAssignmentState)
        .join(StudentAssignmentState)
        .where(
            Assignment.classroom_id == class_id,
            Assignment.status != "draft",
            StudentAssignmentState.student_id == student_id,
        )
        .order_by(
            StudentAssignmentState.status.in_(("blocked", "overdue")).desc(),
            Assignment.due_at,
            Assignment.id,
        )
    ).all()


def _render_profile(
    request, db, actor, classroom, student_id, *, focus_id=None, error=None, message="", key=None
):
    coordinating = request.url.path.startswith("/coordinator/")
    school = (
        coordinator_school(request, db, actor, classroom.school_id)
        if coordinating
        else db.get(School, classroom.school_id)
    )
    enrolled = (
        db.scalar(
            select(ClassMembership.id).where(
                ClassMembership.classroom_id == classroom.id,
                ClassMembership.user_id == student_id,
                ClassMembership.role == "student",
            )
        )
        is not None
    )
    rows = _states(db, classroom.id, student_id)
    if not enrolled and not rows:
        raise NotFoundError("This student has no enrollment or assignment history in this class.")
    student = db.get(User, student_id)
    assignment_ids = [assignment.id for assignment, _ in rows]
    requests, replies = defaultdict(list), defaultdict(list)
    for event in db.scalars(
        select(ProgressEvent)
        .where(
            ProgressEvent.student_id == student_id,
            ProgressEvent.assignment_id.in_(assignment_ids),
            ProgressEvent.event_type == "blocked",
        )
        .order_by(ProgressEvent.id.desc())
    ):
        requests[event.assignment_id].append(event)
    for reply, author in support_replies(db, assignment_ids, student_id):
        replies[reply.assignment_id].append((reply, author))
    delivery_parts = defaultdict(list)
    for delivery in db.scalars(
        select(NotificationDelivery).where(
            NotificationDelivery.kind == "student_support",
            NotificationDelivery.user_id == student_id,
            NotificationDelivery.classroom_id == classroom.id,
            NotificationDelivery.assignment_id.in_(assignment_ids),
            NotificationDelivery.idempotency_key.like("support-reply:%"),
        ).order_by(NotificationDelivery.id)
    ):
        _, reply_id, *part = delivery.idempotency_key.split(":")
        label = "Attachment" if part == ["attachment"] else (
            f"Message (part {part[1]})" if len(part) == 2 else "Message"
        )
        delivery_parts[reply_id].append((label, delivery))
    latest_submissions = {}
    for submission_id, assignment_id in db.execute(
        select(Submission.id, Submission.assignment_id)
        .where(
            Submission.student_id == student_id,
            Submission.assignment_id.in_(assignment_ids),
        )
        .order_by(Submission.id.desc())
    ):
        latest_submissions.setdefault(assignment_id, submission_id)
    response = render(
        request,
        "staff/student.html",
        status_code=422 if error else 200,
        classroom=classroom,
        school=school,
        student=student,
        rows=rows,
        enrolled=enrolled,
        can_reply=not coordinating and enrolled and not classroom.archived_at,
        requests=requests,
        replies=replies,
        delivery_parts=delivery_parts,
        latest_submissions=latest_submissions,
        focus_id=focus_id,
        error=error,
        draft_message=message,
        reply_keys={
            assignment_id: key if assignment_id == focus_id and key else operation_key("support")
            for assignment_id in assignment_ids
        },
        inline_image_types=INLINE_IMAGE_TYPES,
        max_upload_mb=f"{request.app.state.settings.max_upload_bytes / (1024 * 1024):g}",
        message=request.query_params.get("message"),
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("/coordinator/classes/{class_id}/students/{student_id}", response_class=HTMLResponse)
@router.get("/teacher/classes/{class_id}/students/{student_id}", response_class=HTMLResponse)
def student_profile(
    request: Request,
    class_id: int,
    student_id: int,
    assignment_id: int | None = None,
    actor: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    require_class = (
        require_coordinator_class
        if request.url.path.startswith("/coordinator/")
        else require_teacher_class
    )
    classroom = require_class(db, actor, class_id)
    return _render_profile(request, db, actor, classroom, student_id, focus_id=assignment_id)


def _image_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.post(
    "/teacher/classes/{class_id}/students/{student_id}/assignments/{assignment_id}/support"
)
def send_support_reply(
    request: Request,
    class_id: int,
    student_id: int,
    assignment_id: int,
    csrf_token: str = Form(),
    idempotency_key: str = Form(),
    message: str = Form(""),
    file: UploadFile | None = File(None),
    actor: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    classroom = require_active_class(require_teacher_class(db, actor, class_id))
    student = db.get(User, student_id)
    if not student:
        raise NotFoundError("Student not found.")
    assignment = require_student_assignment(db, student, assignment_id)
    if assignment.classroom_id != class_id:
        raise NotFoundError("Assignment not found in this class.")
    settings = request.app.state.settings
    profile = _profile_path("teacher", class_id, student_id)

    def retry(error):
        return _render_profile(
            request,
            db,
            actor,
            classroom,
            student_id,
            focus_id=assignment_id,
            error=error,
            message=message,
            key=idempotency_key,
        )

    if not idempotency_key or len(idempotency_key) > 160:
        return retry("Reload the student page and try again.")
    existing = db.scalar(
        select(StudentSupportReply).where(StudentSupportReply.idempotency_key == idempotency_key)
    )
    if existing:
        if (existing.author_id, existing.student_id, existing.assignment_id) != (
            actor.id,
            student_id,
            assignment_id,
        ):
            raise AuthorizationError("Resource is outside your authorized scope")
        return redirect_with_message(
            profile, "This reply is already saved. Check Telegram delivery below.",
            anchor=f"assignment-{assignment_id}"
        )
    if assignment.status != "assigned":
        raise NotFoundError("This assignment is closed to new help replies.")
    if (
        db.scalar(
            select(StudentAssignmentState.id).where(
                StudentAssignmentState.assignment_id == assignment.id,
                StudentAssignmentState.student_id == student_id,
            )
        )
        is None
    ):
        raise NotFoundError("No assignment record found for this student.")
    if len(message) > 5000:
        return retry("Keep the message within 5,000 characters.")
    if not message.strip() and not (file and file.filename):
        return retry("Write a message, attach a file, or do both.")

    destination, filename, mime_type = None, None, None
    if file and file.filename:
        content = file.file.read(settings.max_upload_bytes + 1)
        if not content or len(content) > settings.max_upload_bytes:
            return retry("Choose a non-empty file within the size limit, then try again.")
        filename = file.filename.replace("\\", "/").rsplit("/", 1)[-1][:240] or "Help attachment"
        mime_type = _image_type(content)
        root = settings.upload_dir.resolve()
        candidate = root / uuid.uuid4().hex
        try:
            root.mkdir(parents=True, exist_ok=True)
            with candidate.open("xb") as output:
                destination = candidate
                output.write(content)
        except OSError:
            if destination:
                destination.unlink(missing_ok=True)
            return retry("The attachment could not be saved. Select it again and retry.")

    saved = False
    try:
        reply = StudentSupportReply(
            assignment_id=assignment.id,
            student_id=student_id,
            author_id=actor.id,
            message=message.strip(),
            stored_file_path=str(destination) if destination else None,
            original_filename=filename,
            mime_type=mime_type,
            idempotency_key=idempotency_key,
        )
        db.add(reply)
        db.flush()
        db.add(
            ActivityEvent(
                school_id=assignment.school_id,
                classroom_id=class_id,
                actor_user_id=actor.id,
                event_type="student_support_sent",
                entity_type="student_support_reply",
                entity_id=reply.id,
            )
        )
        delivery = request.app.state.notification_service.notify_student_support(
            db, assignment, student, actor, classroom, reply,
        )
        notice = {
            "pending": "Reply saved in Class Relay and queued for Telegram.",
            "skipped": "Reply saved in Class Relay. The student hasn’t connected Telegram.",
            "logged": "Reply saved in Class Relay. Telegram delivery was simulated.",
        }[delivery.status]
        db.commit()
        saved = True
    finally:
        if destination and not saved:
            destination.unlink(missing_ok=True)
    return redirect_with_message(
        profile,
        notice,
        anchor=f"assignment-{assignment_id}",
    )


@router.get("/student/support-replies/{reply_id}/file")
@router.get("/teacher/support-replies/{reply_id}/file")
@router.get("/coordinator/support-replies/{reply_id}/file")
def support_attachment(
    request: Request,
    reply_id: int,
    download: bool = False,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    reply = db.get(StudentSupportReply, reply_id)
    if not reply:
        raise NotFoundError("Help attachment not found.")
    workspace = request.url.path.split("/")[1]
    if workspace == "student":
        if user.id != reply.student_id:
            raise AuthorizationError("Resource is outside your authorized scope")
        require_student_assignment(db, user, reply.assignment_id)
    else:
        assignment = db.get(Assignment, reply.assignment_id)
        require_class = (
            require_coordinator_class if workspace == "coordinator" else require_teacher_class
        )
        require_class(db, user, assignment.classroom_id)
    return send_private_file(
        request,
        reply.stored_file_path,
        reply.original_filename,
        reply.mime_type,
        download,
    )
