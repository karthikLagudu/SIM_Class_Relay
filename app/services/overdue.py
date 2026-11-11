from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActivityEvent, Assignment, StudentAssignmentState
from app.services.state_machine import transition

ELIGIBLE = {"assigned", "acknowledged", "in_progress", "blocked", "needs_revision"}


class OverdueService:
    def mark_overdue(
        self,
        db: Session,
        assignment: Assignment,
        state: StudentAssignmentState,
        now: datetime,
    ) -> bool:
        due = assignment.due_at.replace(tzinfo=assignment.due_at.tzinfo or UTC)
        now = now.replace(tzinfo=now.tzinfo or UTC)
        if assignment.status == "cancelled" or state.status not in ELIGIBLE or now <= due:
            return False
        previous = state.status
        # System transitions do not count as a student's own activity.
        last_activity = state.last_activity_at
        transition(state, "overdue", now)
        state.last_activity_at = last_activity
        db.add(
            ActivityEvent(
                school_id=assignment.school_id,
                classroom_id=assignment.classroom_id,
                actor_user_id=None,
                event_type="student_assignment_overdue",
                entity_type="student_assignment_state",
                entity_id=state.id,
                metadata_json={"assignment_id": assignment.id, "from": previous},
            )
        )
        return True

    def restore_after_extension(self, db: Session, assignment: Assignment) -> None:
        states = db.scalars(select(StudentAssignmentState).where(
            StudentAssignmentState.assignment_id == assignment.id,
            StudentAssignmentState.status == "overdue",
        ))
        for state in states:
            event = db.scalar(select(ActivityEvent).where(
                ActivityEvent.entity_type == "student_assignment_state",
                ActivityEvent.entity_id == state.id,
                ActivityEvent.event_type == "student_assignment_overdue",
            ).order_by(ActivityEvent.id.desc()).limit(1))
            previous = event.metadata_json.get("from") if event else None
            if previous not in ELIGIBLE:
                previous = "blocked" if state.block_reason else "in_progress" if state.started_at else (
                    "acknowledged" if state.acknowledged_at else "assigned"
                )
            # Restore the state recorded before the system transition, preserving student timestamps.
            state.status = previous
