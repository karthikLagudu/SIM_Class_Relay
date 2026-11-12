from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import (
    Assignment,
    ClassMembership,
    Classroom,
    SchoolMembership,
    StudentAssignmentState,
    Submission,
    User,
)
from app.services.risk import RiskItem, RiskService


def pending_reviews(classroom_ids):
    latest = select(func.max(Submission.id)).group_by(
        Submission.assignment_id, Submission.student_id
    )
    return (
        select(Submission, Assignment, User)
        .join(Assignment, Assignment.id == Submission.assignment_id)
        .join(Classroom, Classroom.id == Assignment.classroom_id)
        .join(User, User.id == Submission.student_id)
        .join(
            StudentAssignmentState,
            and_(
                StudentAssignmentState.assignment_id == Submission.assignment_id,
                StudentAssignmentState.student_id == Submission.student_id,
            ),
        )
        .join(
            ClassMembership,
            and_(
                ClassMembership.classroom_id == Assignment.classroom_id,
                ClassMembership.user_id == Submission.student_id,
                ClassMembership.role == "student",
            ),
        )
        .where(
            Assignment.classroom_id.in_(classroom_ids),
            Classroom.archived_at.is_(None),
            Assignment.status == "assigned",
            StudentAssignmentState.status == "submitted",
            Submission.id.in_(latest),
        )
        .order_by(Submission.created_at, Submission.id)
    )


def school_teachers(db: Session, school_id: int) -> list[User]:
    return school_users(db, school_id, "teacher")


def school_users(db: Session, school_id: int, role: str) -> list[User]:
    ids = (
        select(SchoolMembership.user_id)
        .where(
            SchoolMembership.school_id == school_id,
            SchoolMembership.role == role,
        )
        .union(
            select(ClassMembership.user_id)
            .join(Classroom)
            .where(
                Classroom.school_id == school_id,
                ClassMembership.role == role,
            )
        )
    )
    return list(db.scalars(select(User).where(User.id.in_(ids)).order_by(User.name, User.id)))


@dataclass
class ClassWorkload:
    classroom: Classroom
    teachers: list[User] = field(default_factory=list)
    students: list[User] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    reviews: list = field(default_factory=list)
    risks: list[RiskItem] = field(default_factory=list)


def class_workloads(db: Session, classes: list[Classroom]) -> dict[int, ClassWorkload]:
    workloads = {classroom.id: ClassWorkload(classroom) for classroom in classes}
    for member, user in db.execute(
        select(ClassMembership, User)
        .join(User, User.id == ClassMembership.user_id)
        .where(ClassMembership.classroom_id.in_(workloads))
        .order_by(User.name, User.id)
    ):
        row = workloads[member.classroom_id]
        (row.teachers if member.role == "teacher" else row.students).append(user)
    active = [classroom.id for classroom in classes if classroom.archived_at is None]
    for assignment in db.scalars(
        select(Assignment)
        .where(Assignment.classroom_id.in_(workloads))
        .order_by(Assignment.due_at, Assignment.id)
    ):
        workloads[assignment.classroom_id].assignments.append(assignment)
    for review in db.execute(pending_reviews(active)):
        workloads[review[1].classroom_id].reviews.append(review)
    for risk in RiskService().for_classes(db, active):
        workloads[risk.classroom_id].risks.append(risk)
    return workloads


def teacher_directory(teachers: list[User], workloads: dict[int, ClassWorkload]) -> list[dict]:
    by_teacher = defaultdict(list)
    for row in workloads.values():
        if row.classroom.archived_at is None:
            for teacher in row.teachers:
                by_teacher[teacher.id].append(row)

    directory = []
    for teacher in teachers:
        classes = by_teacher[teacher.id]
        assignments = [
            item for row in classes for item in row.assignments if item.status == "assigned"
        ]
        directory.append(
            {
                "teacher": teacher,
                "classes": classes,
                "student_count": len({student.id for row in classes for student in row.students}),
                "active_count": len(assignments),
                "personal_count": sum(
                    item.created_by_user_id == teacher.id for item in assignments
                ),
                "review_count": sum(len(row.reviews) for row in classes),
                "attention_count": len({risk.student_id for row in classes for risk in row.risks}),
            }
        )
    return directory
