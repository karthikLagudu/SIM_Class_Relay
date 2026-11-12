from collections import defaultdict

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.exceptions import AuthorizationError, ValidationError
from app.models import (
    ActivityEvent,
    ClassMembership,
    Classroom,
    NotificationDelivery,
    SchoolMembership,
    User,
)
from app.services.authorization import has_school_role
from app.services.workload import pending_reviews, school_teachers


def staffing_plan(
    db: Session,
    actor: User,
    school_id: int,
    selected: list[int],
    *,
    class_id: int | None = None,
    teacher_id: int | None = None,
    revoke: bool = False,
) -> dict:
    if not has_school_role(db, actor.id, school_id, {"coordinator"}):
        raise AuthorizationError("Resource is outside your authorized scope")
    teachers = {teacher.id for teacher in school_teachers(db, school_id)}
    classes = {
        item.id: item
        for item in db.scalars(
            select(Classroom)
            .where(Classroom.school_id == school_id)
            .order_by(Classroom.name, Classroom.id)
        )
    }
    active = {item.id for item in classes.values() if item.archived_at is None}
    wanted = set(selected)
    current = defaultdict(set)
    for member in db.scalars(
        select(ClassMembership).where(
            ClassMembership.classroom_id.in_(classes),
            ClassMembership.role == "teacher",
        )
    ):
        current[member.classroom_id].add(member.user_id)

    if class_id is not None and teacher_id is None and not revoke:
        if class_id not in classes or not wanted.issubset(teachers):
            raise AuthorizationError("Resource is outside your authorized scope")
        if class_id not in active:
            raise ValidationError("This class is archived. Its teaching team is read-only.")
        proposed = {class_id: wanted}
    elif class_id is None and teacher_id is not None:
        if teacher_id not in teachers or not wanted.issubset(classes):
            raise AuthorizationError("Resource is outside your authorized scope")
        if not wanted.issubset(active):
            raise ValidationError("Teachers can only be assigned to active classes.")
        proposed = {}
        for item_id in classes if revoke else active:
            team = current[item_id]
            proposed[item_id] = (
                team | {teacher_id} if not revoke and item_id in wanted else team - {teacher_id}
            )
    else:
        raise ValidationError("Choose a class team or a teacher’s class assignments.")

    changed = [
        item_id
        for item_id in classes
        if item_id in proposed and proposed[item_id] != current[item_id]
    ]
    roster_counts = dict(
        db.execute(
            select(ClassMembership.classroom_id, func.count())
            .where(
                ClassMembership.classroom_id.in_(changed),
                ClassMembership.role == "student",
            )
            .group_by(ClassMembership.classroom_id)
        ).all()
    )
    review_counts = defaultdict(int)
    for _, assignment, _ in db.execute(
        pending_reviews([item_id for item_id in changed if item_id in active])
    ):
        review_counts[assignment.classroom_id] += 1
    return {
        "actor_id": actor.id,
        "school_id": school_id,
        "class_id": class_id,
        "teacher_id": teacher_id,
        "selected": sorted(wanted),
        "revoke": revoke,
        "changes": [
            {
                "class_id": item_id,
                "current": sorted(current[item_id]),
                "proposed": sorted(proposed[item_id]),
                "student_count": roster_counts.get(item_id, 0),
                "review_count": review_counts[item_id],
            }
            for item_id in changed
        ],
    }


def apply_staffing(db: Session, actor: User, plan: dict) -> None:
    school_id, teacher_id = plan["school_id"], plan["teacher_id"]
    fresh = staffing_plan(
        db,
        actor,
        school_id,
        plan["selected"],
        class_id=plan["class_id"],
        teacher_id=teacher_id,
        revoke=plan["revoke"],
    )
    if fresh != plan:
        raise ValidationError(
            "Class staffing or workload changed. Review the proposed changes again."
        )
    outgoing = {teacher_id} if plan["revoke"] else set()
    for change in fresh["changes"]:
        class_id = change["class_id"]
        members = {
            member.user_id: member
            for member in db.scalars(
                select(ClassMembership).where(
                    ClassMembership.classroom_id == class_id,
                    ClassMembership.role == "teacher",
                )
            )
        }
        old, new = set(change["current"]), set(change["proposed"])
        outgoing.update(old - new)
        removed = {str(teacher): members[teacher].id for teacher in sorted(old - new)}
        for teacher in old - new:
            db.delete(members[teacher])
        added = {
            str(teacher): ClassMembership(classroom_id=class_id, user_id=teacher, role="teacher")
            for teacher in sorted(new - old)
        }
        db.add_all(added.values())
        db.flush()
        db.add(
            ActivityEvent(
                school_id=school_id,
                classroom_id=class_id,
                actor_user_id=actor.id,
                event_type="teaching_team_changed",
                entity_type="classroom",
                entity_id=class_id,
                metadata_json={
                    **change,
                    "removed_memberships": removed,
                    "added_memberships": {teacher: member.id for teacher, member in added.items()},
                },
            )
        )
    if outgoing:
        # These queued replies can summarize several teaching classes and have no single class scope.
        db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.user_id.in_(outgoing),
                NotificationDelivery.assignment_id.is_(None),
                NotificationDelivery.kind.in_(
                    (
                        "classes",
                        "reminders",
                        "risk_summary",
                        "teacher_confirmation",
                        "teacher_clarification",
                        "assignment_created",
                    )
                ),
                NotificationDelivery.status.in_(("pending", "failed")),
            )
            .values(status="skipped", error="Teaching access changed", next_attempt_at=None)
        )
    if plan["revoke"]:
        membership = db.scalar(
            select(SchoolMembership).where(
                SchoolMembership.school_id == school_id,
                SchoolMembership.user_id == teacher_id,
                SchoolMembership.role == "teacher",
            )
        )
        if membership:
            db.delete(membership)
        db.add(
            ActivityEvent(
                school_id=school_id,
                actor_user_id=actor.id,
                event_type="school_teaching_access_removed",
                entity_type="user",
                entity_id=teacher_id,
                metadata_json={
                    "school_membership_id": membership.id if membership else None,
                    "classroom_ids": [change["class_id"] for change in fresh["changes"]],
                },
            )
        )
