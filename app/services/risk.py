from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Assignment, ClassMembership, Classroom, StudentAssignmentState, User
from app.services.authorization import teacher_class_scope


@dataclass(frozen=True)
class RiskItem:
    classroom_id: int
    student_id: int
    student_name: str
    assignment_id: int
    assignment_title: str
    risk_score: int
    risk_level: str
    reasons: tuple[str, ...]
    deadline: datetime
    last_activity: datetime | None
    state: str

    def summary_fact(self) -> str:
        last = self.last_activity.isoformat() if self.last_activity else "none"
        return (
            f"{self.student_name}: {self.assignment_title}; state {self.state}; "
            f"{', '.join(self.reasons)}; due {self.deadline.isoformat()}; last activity {last}; "
            f"risk {self.risk_score}/100."
        )


class RiskService:
    def for_actor(
        self,
        db: Session,
        actor: User,
        now: datetime | None = None,
        *,
        classroom_id: int | None = None,
    ) -> list[RiskItem]:
        scope = teacher_class_scope(actor.id)
        if classroom_id is not None:
            scope = scope.where(Classroom.id == classroom_id)
        return self.for_classes(db, scope, now)

    def for_classes(
        self, db: Session, classroom_ids, now: datetime | None = None
    ) -> list[RiskItem]:
        now = now or datetime.now(UTC)
        query = (
            select(StudentAssignmentState, User, Assignment)
            .join(User, User.id == StudentAssignmentState.student_id)
            .join(Assignment, Assignment.id == StudentAssignmentState.assignment_id)
            .join(ClassMembership, ClassMembership.classroom_id == Assignment.classroom_id)
            .where(
                Assignment.status == "assigned",
                Assignment.classroom_id.in_(classroom_ids),
                ClassMembership.user_id == StudentAssignmentState.student_id,
                ClassMembership.role == "student",
            )
        )
        rows = db.execute(query).all()
        result: list[RiskItem] = []
        for state, student, assignment in rows:
            if state.status in {"submitted", "completed", "cancelled"}:
                continue
            score = 0
            reasons: list[str] = []
            due = assignment.due_at.replace(tzinfo=assignment.due_at.tzinfo or UTC)
            if state.status == "needs_revision":
                score += 45
                reasons.append("needs revision")
            if state.status == "blocked" or (state.status == "overdue" and state.block_reason):
                score += 70
                reasons.append("blocked")
            if state.status == "overdue" or due < now:
                score += 80
                reasons.append("overdue")
            elif due <= now + timedelta(hours=24):
                score += 30
                reasons.append("due within 24 hours")
            if state.last_activity_at is None:
                if due <= now + timedelta(hours=24):
                    score += 25
                    reasons.append("no acknowledgement or activity")
            else:
                last = state.last_activity_at.replace(tzinfo=state.last_activity_at.tzinfo or UTC)
                if last < now - timedelta(days=2):
                    score += 20
                    reasons.append("inactive for two days")
                elif last >= now - timedelta(hours=2):
                    score -= 10
            score = max(0, min(100, score))
            if score < 25:
                continue
            level = "critical" if score >= 80 else "high" if score >= 60 else "medium"
            result.append(
                RiskItem(
                    classroom_id=assignment.classroom_id,
                    student_id=student.id,
                    student_name=student.name,
                    assignment_id=assignment.id,
                    assignment_title=assignment.title,
                    risk_score=score,
                    risk_level=level,
                    reasons=tuple(reasons),
                    deadline=due,
                    last_activity=(last if state.last_activity_at is not None else None),
                    state=state.status,
                )
            )
        return sorted(result, key=lambda item: (-item.risk_score, item.student_name))
