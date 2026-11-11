from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import AmbiguousReferenceError, NotFoundError
from app.models import (
    Assignment,
    AssignmentTarget,
    ClassMembership,
    StudentAssignmentState,
    User,
)
from app.services.authorization import teacher_class_scope


def normalize(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", value.casefold()))


def explicit_id(value: str) -> int | None:
    match = re.fullmatch(r"#?([0-9]{1,18})", value.strip())
    return int(match[1]) if match else None


def model_reference(reference: str | None, source: str) -> str | None:
    if reference is None:
        return None
    number = explicit_id(reference)
    if number is not None and not re.search(rf"(?<!\w)#{number}(?![0-9])", source):
        raise AmbiguousReferenceError("Please use the title or write the assignment number as #ID.")
    return reference


class AssignmentReferenceService:
    def choose(
        self, assignments: list[Assignment], reference: str | None, *, exact: bool = False
    ) -> Assignment:
        if reference is None:
            matches = assignments
        elif (number := explicit_id(reference)) is not None:
            matches = [item for item in assignments if item.id == number]
        else:
            clean = normalize(reference)
            matches = [item for item in assignments if clean and normalize(item.title) == clean]
            if not matches and not exact and clean:
                matches = [
                    item for item in assignments if normalize(item.title).startswith(clean + " ")
                ]
                if not matches:
                    meaningful = set(clean.split()) - {"a", "an", "the", "homework", "assignment"}
                    # Every meaningful word must match. A single shared generic word is too weak.
                    matches = [
                        item
                        for item in assignments
                        if meaningful and meaningful.issubset(normalize(item.title).split())
                    ]
        if not matches:
            raise NotFoundError("No authorized assignment matches that reference")
        if len(matches) > 1:
            raise AmbiguousReferenceError("Which assignment do you mean? " + self.describe(matches))
        return matches[0]

    @staticmethod
    def describe(assignments: list[Assignment]) -> str:
        return ", ".join(f"#{item.id} {item.title}" for item in assignments[:5])

    def for_teacher(
        self, db: Session, actor: User, classroom_id: int | None = None
    ) -> list[Assignment]:
        query = select(Assignment).where(
            Assignment.classroom_id.in_(teacher_class_scope(actor.id)),
            Assignment.status == "assigned",
        )
        if classroom_id is not None:
            query = query.where(Assignment.classroom_id == classroom_id)
        return list(db.scalars(query.order_by(Assignment.due_at, Assignment.id)))

    def for_student(self, db: Session, actor: User) -> list[Assignment]:
        return list(
            db.scalars(
                select(Assignment)
                .join(AssignmentTarget, AssignmentTarget.assignment_id == Assignment.id)
                .join(ClassMembership, ClassMembership.classroom_id == Assignment.classroom_id)
                .join(StudentAssignmentState, StudentAssignmentState.assignment_id == Assignment.id)
                .where(
                    AssignmentTarget.student_id == actor.id,
                    ClassMembership.user_id == actor.id,
                    ClassMembership.role == "student",
                    StudentAssignmentState.student_id == actor.id,
                    StudentAssignmentState.status.not_in(["completed", "cancelled"]),
                    Assignment.status == "assigned",
                )
                .order_by(Assignment.due_at, Assignment.id)
            )
        )

    def resolve_for_teacher(
        self, db: Session, actor: User, reference: str, classroom_id: int | None = None
    ) -> Assignment:
        return self.choose(self.for_teacher(db, actor, classroom_id), reference)

    def resolve_for_student(self, db: Session, actor: User, reference: str) -> Assignment:
        return self.choose(self.for_student(db, actor), reference)
