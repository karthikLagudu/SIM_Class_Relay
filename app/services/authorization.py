from __future__ import annotations

from sqlalchemy import Select, exists, or_, select
from sqlalchemy.orm import Session

from app.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models import (
    Assignment,
    AssignmentTarget,
    ClassMembership,
    Classroom,
    School,
    SchoolMembership,
    Submission,
    User,
)


def has_school_role(db: Session, user_id: int, school_id: int, roles: set[str]) -> bool:
    return bool(
        db.scalar(
            select(
                exists().where(
                    SchoolMembership.user_id == user_id,
                    SchoolMembership.school_id == school_id,
                    SchoolMembership.role.in_(roles),
                )
            )
        )
    )


def teacher_class_scope(user_id: int) -> Select[tuple[int]]:
    return select(Classroom.id).where(
        Classroom.archived_at.is_(None),
        Classroom.id.in_(class_membership_scope(user_id, "teacher")),
    )


def class_membership_scope(user_id: int, role: str) -> Select[tuple[int]]:
    return select(ClassMembership.classroom_id).where(
        ClassMembership.user_id == user_id, ClassMembership.role == role
    )


def coordinator_class_scope(user_id: int) -> Select[tuple[int]]:
    schools = select(SchoolMembership.school_id).where(
        SchoolMembership.user_id == user_id,
        SchoolMembership.role == "coordinator",
    )
    return select(Classroom.id).where(Classroom.school_id.in_(schools))


def coordinated_schools(db: Session, user: User) -> list[School]:
    return list(
        db.scalars(
            select(School)
            .join(SchoolMembership)
            .where(
                SchoolMembership.user_id == user.id,
                SchoolMembership.role == "coordinator",
            )
            .order_by(SchoolMembership.id)
        )
    )


def require_coordinator_school(db: Session, user: User, school_id: int) -> School:
    if not has_school_role(db, user.id, school_id, {"coordinator"}):
        raise AuthorizationError("Resource is outside your authorized scope")
    return db.get(School, school_id)


def workspace_roles(db: Session, user: User) -> set[str]:
    return set(
        db.scalars(
            select(SchoolMembership.role)
            .where(SchoolMembership.user_id == user.id)
            .union(select(ClassMembership.role).where(ClassMembership.user_id == user.id))
        )
    )


def list_teacher_classes(db: Session, user: User) -> list[Classroom]:
    return list(
        db.scalars(
            select(Classroom)
            .where(
                Classroom.id.in_(teacher_class_scope(user.id)),
            )
            .order_by(Classroom.name, Classroom.id)
        )
    )


def require_teacher_class(db: Session, user: User, classroom_id: int) -> Classroom:
    return _require_class(db, classroom_id, Classroom.id.in_(teacher_class_scope(user.id)))


def require_coordinator_class(db: Session, user: User, classroom_id: int) -> Classroom:
    return _require_class(db, classroom_id, Classroom.id.in_(coordinator_class_scope(user.id)))


def require_active_class(classroom: Classroom) -> Classroom:
    if classroom.archived_at is not None:
        raise ValidationError("This class is archived. Its records are read-only.")
    return classroom


def require_managed_class(db: Session, user: User, classroom_id: int) -> Classroom:
    return require_active_class(
        _require_class(
            db,
            classroom_id,
            or_(
                Classroom.id.in_(teacher_class_scope(user.id)),
                Classroom.id.in_(coordinator_class_scope(user.id)),
            ),
        )
    )


def _require_class(db: Session, classroom_id: int, allowed) -> Classroom:
    classroom = db.get(Classroom, classroom_id)
    if not classroom:
        raise NotFoundError("Class not found")
    if db.scalar(select(Classroom.id).where(Classroom.id == classroom_id, allowed)) is None:
        raise AuthorizationError("Resource is outside your authorized scope")
    return classroom


def require_student_class(db: Session, user: User, classroom_id: int) -> Classroom:
    return _require_class(
        db, classroom_id, Classroom.id.in_(class_membership_scope(user.id, "student"))
    )


def student_work_scope(user_id: int) -> Select[tuple[int]]:
    return select(Assignment.id).where(
        Assignment.status != "draft",
        Assignment.classroom_id.in_(class_membership_scope(user_id, "student")),
        Assignment.id.in_(
            select(AssignmentTarget.assignment_id).where(AssignmentTarget.student_id == user_id)
        ),
    )


def require_teacher_assignment(db: Session, user: User, assignment_id: int) -> Assignment:
    return require_staff_assignment(db, user, assignment_id)


def require_staff_assignment(
    db: Session, user: User, assignment_id: int, *, coordinator: bool = False, write: bool = False
) -> Assignment:
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        raise NotFoundError("Assignment not found")
    require = require_coordinator_class if coordinator else require_teacher_class
    classroom = require(db, user, assignment.classroom_id)
    if write:
        require_active_class(classroom)
    return assignment


def require_student_assignment(db: Session, user: User, assignment_id: int) -> Assignment:
    assignment = db.get(Assignment, assignment_id)
    if not assignment or assignment.status == "draft":
        raise NotFoundError("Assignment not found")
    if db.scalar(student_work_scope(user.id).where(Assignment.id == assignment_id)) is None:
        raise AuthorizationError("Resource is outside your authorized scope")
    return assignment


def require_teacher_submission(db: Session, user: User, submission_id: int) -> Submission:
    return require_staff_submission(db, user, submission_id)


def require_staff_submission(
    db: Session, user: User, submission_id: int, *, coordinator: bool = False, write: bool = False
) -> Submission:
    submission = db.get(Submission, submission_id)
    if not submission:
        raise NotFoundError("Submission not found")
    require_staff_assignment(
        db, user, submission.assignment_id, coordinator=coordinator, write=write
    )
    return submission


def require_student_submission(db: Session, user: User, submission_id: int) -> Submission:
    submission = db.get(Submission, submission_id)
    if not submission:
        raise NotFoundError("Submission not found")
    if submission.student_id != user.id:
        raise AuthorizationError("Resource is outside your authorized scope")
    require_student_assignment(db, user, submission.assignment_id)
    return submission
