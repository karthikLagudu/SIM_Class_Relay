from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import ValidationError
from app.models import ActivityEvent, Assignment, Classroom, Feedback, Submission, User
from app.services.assignment import get_student_state
from app.services.authorization import (
    require_active_class,
    require_staff_submission,
    require_student_assignment,
)
from app.services.notification import NotificationService
from app.services.state_machine import transition


class SubmissionService:
    def submit(
        self,
        db: Session,
        student: User,
        assignment_id: int,
        idempotency_key: str,
        text_content: str | None = None,
        telegram_file_id: str | None = None,
        stored_file_path: str | None = None,
        original_filename: str | None = None,
        mime_type: str | None = None,
        content: bytes | None = None,
    ) -> Submission:
        assignment = require_student_assignment(db, student, assignment_id)
        require_active_class(db.get(Classroom, assignment.classroom_id))
        existing = db.scalar(
            select(Submission).where(Submission.idempotency_key == idempotency_key)
        )
        if existing:
            if existing.student_id != student.id or existing.assignment_id != assignment_id:
                from app.exceptions import AuthorizationError

                raise AuthorizationError("Idempotency key belongs to another operation")
            return existing
        if text_content is not None and not text_content.strip():
            text_content = None
        if not text_content and not (telegram_file_id or stored_file_path):
            raise ValidationError("Include your work as text or a file")
        state = get_student_state(db, assignment_id, student.id)
        if state.status in {"submitted", "completed", "cancelled"}:
            raise ValidationError(
                "This assignment is no longer accepting a submission. A teacher can request revision."
            )
        now = datetime.now(UTC)
        if state.status in {"assigned", "acknowledged"}:
            transition(state, "in_progress", now)
        transition(state, "submitted", now)
        submission_type = "file" if telegram_file_id or stored_file_path else "text"
        digest_input = (
            content if content is not None else (text_content or telegram_file_id or "").encode()
        )
        submission = Submission(
            assignment_id=assignment_id,
            student_id=student.id,
            submission_type=submission_type,
            text_content=text_content,
            instructions_at_submission=assignment.instructions,
            telegram_file_id=telegram_file_id,
            stored_file_path=stored_file_path,
            original_filename=original_filename,
            mime_type=mime_type,
            content_hash=hashlib.sha256(digest_input).hexdigest(),
            idempotency_key=idempotency_key,
        )
        db.add(submission)
        db.flush()
        db.add(
            ActivityEvent(
                school_id=assignment.school_id,
                classroom_id=assignment.classroom_id,
                actor_user_id=student.id,
                event_type="submission_received",
                entity_type="submission",
                entity_id=submission.id,
                metadata_json={"type": submission_type},
            )
        )
        return submission


class FeedbackService:
    def __init__(self, notifications: NotificationService | None = None):
        self.notifications = notifications

    def create(
        self,
        db: Session,
        teacher: User,
        submission_id: int,
        message: str,
        complete: bool = False,
        idempotency_key: str | None = None,
        *,
        coordinator: bool = False,
    ) -> Feedback:
        submission = require_staff_submission(
            db, teacher, submission_id, coordinator=coordinator, write=True
        )
        if idempotency_key:
            existing = db.scalar(
                select(Feedback).where(Feedback.idempotency_key == idempotency_key)
            )
            if existing:
                if existing.teacher_id != teacher.id or existing.submission_id != submission_id:
                    from app.exceptions import AuthorizationError

                    raise AuthorizationError("Idempotency key belongs to another operation")
                return existing
        assignment = require_student_assignment(
            db, db.get(User, submission.student_id), submission.assignment_id
        )
        message = message.strip()
        if not message:
            raise ValidationError("Feedback cannot be empty")
        latest_id = db.scalar(
            select(Submission.id)
            .where(
                Submission.assignment_id == submission.assignment_id,
                Submission.student_id == submission.student_id,
            )
            .order_by(Submission.id.desc())
            .limit(1)
        )
        state = get_student_state(db, submission.assignment_id, submission.student_id)
        if latest_id != submission.id or state.status != "submitted":
            raise ValidationError("Review the latest submission that is awaiting feedback")
        transition(state, "completed" if complete else "needs_revision", datetime.now(UTC))
        feedback = Feedback(
            submission_id=submission.id,
            assignment_id=submission.assignment_id,
            teacher_id=teacher.id,
            student_id=submission.student_id,
            message=message.strip(),
            idempotency_key=idempotency_key,
        )
        db.add(feedback)
        db.flush()
        db.add(
            ActivityEvent(
                school_id=assignment.school_id,
                classroom_id=assignment.classroom_id,
                actor_user_id=teacher.id,
                event_type="feedback_sent",
                entity_type="feedback",
                entity_id=feedback.id,
                metadata_json={"submission_id": submission.id, "complete": complete},
            )
        )
        if self.notifications:
            assignment = db.get(Assignment, submission.assignment_id)
            student = db.get(User, submission.student_id)
            self.notifications.notify_feedback(
                db,
                assignment,
                student,
                feedback.message,
                f"feedback:{idempotency_key or feedback.id}",
            )
        return feedback
