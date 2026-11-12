from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.models import ActivityEvent, Classroom, User

MANAGEMENT_EVENTS = {
    "school_updated": "School details updated",
    "teaching_team_changed": "Teaching team updated",
    "school_teaching_access_removed": "School teaching access removed",
    "teacher_created": "Teacher added",
    "class_created": "Class created",
    "class_updated": "Class details updated",
    "class_archived": "Class archived",
    "student_created": "Student account created",
    "student_enrolled": "Student enrolled",
    "student_transferred": "Student transferred",
    "student_removed": "Student removed from class",
}


def school_activity(db: Session, school_id: int, class_id: int | None = None) -> list[dict]:
    subject = aliased(User)
    query = (
        select(ActivityEvent, User.name, Classroom.name, subject.name)
        .outerjoin(User, User.id == ActivityEvent.actor_user_id)
        .outerjoin(Classroom, Classroom.id == ActivityEvent.classroom_id)
        .outerjoin(
            subject, (subject.id == ActivityEvent.entity_id) & (ActivityEvent.entity_type == "user")
        )
        .where(
            ActivityEvent.school_id == school_id, ActivityEvent.event_type.in_(MANAGEMENT_EVENTS)
        )
    )
    if class_id is not None:
        query = query.where(ActivityEvent.classroom_id == class_id)
    rows = db.execute(query.order_by(ActivityEvent.id.desc()).limit(20)).all()
    staff_ids = {
        item
        for event, *_ in rows
        for key in ("current", "proposed")
        for item in event.metadata_json.get(key, [])
    }
    staff = dict(db.execute(select(User.id, User.name).where(User.id.in_(staff_ids))).all())
    return [
        {
            "event": event,
            "label": MANAGEMENT_EVENTS[event.event_type],
            "actor": actor,
            "class_name": classroom,
            "subject": name,
            "before": [
                staff[item] for item in event.metadata_json.get("current", []) if item in staff
            ],
            "after": [
                staff[item] for item in event.metadata_json.get("proposed", []) if item in staff
            ],
        }
        for event, actor, classroom, name in rows
    ]
