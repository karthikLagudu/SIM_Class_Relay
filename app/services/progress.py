from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.exceptions import ValidationError
from app.models import ActivityEvent, Classroom, ProgressEvent, User
from app.services.assignment import get_student_state
from app.services.authorization import require_active_class, require_student_assignment
from app.services.notification import NotificationService
from app.services.state_machine import transition


class ProgressService:
    def __init__(self, notifications: NotificationService | None = None):
        self.notifications = notifications

    def update(
        self,
        db: Session,
        student: User,
        assignment_id: int,
        status: str,
        message: str,
        progress_percent: int | None = None,
    ) -> ProgressEvent:
        if status not in {"acknowledged", "in_progress", "blocked"}:
            raise ValidationError("Choose acknowledged, in progress, or blocked")
        assignment = require_student_assignment(db, student, assignment_id)
        require_active_class(db.get(Classroom, assignment.classroom_id))
        state = get_student_state(db, assignment_id, student.id)
        now = datetime.now(UTC)
        transition(state, status, now)
        state.last_activity_at = now
        if status == "blocked":
            state.block_reason = message
        event = ProgressEvent(
            assignment_id=assignment_id,
            student_id=student.id,
            event_type=status,
            message=message,
            progress_percent=progress_percent,
        )
        db.add(event)
        db.add(
            ActivityEvent(
                school_id=assignment.school_id,
                classroom_id=assignment.classroom_id,
                actor_user_id=student.id,
                event_type=f"student_{status}",
                entity_type="assignment",
                entity_id=assignment_id,
                metadata_json={"progress_percent": progress_percent},
            )
        )
        if status == "blocked" and self.notifications:
            db.flush()
            self.notifications.notify_staff(db, assignment, student, "staff_help", event.id)
        return event
