from datetime import UTC, datetime, timedelta
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.auth.security import current_user
from app.database import get_db
from app.models import (
    ActivityEvent,
    Assignment,
    AssignmentTarget,
    ClassMembership,
    Classroom,
    Feedback,
    NotificationDelivery,
    ProgressEvent,
    School,
    StudentAssignmentState,
    StudentSupportReply,
    User,
)
from app.services.authorization import (
    class_membership_scope,
    coordinated_schools,
    list_teacher_classes,
    student_work_scope,
    teacher_class_scope,
    workspace_roles,
)
from app.services.notification import NotificationService

router = APIRouter()
HELP_ALERT_BATCH = 10


def _teacher_help_requests(teacher_id: int):
    return (
        select(ProgressEvent, Assignment, Classroom.name, School.name, User.name)
        .select_from(ProgressEvent)
        .join(Assignment, Assignment.id == ProgressEvent.assignment_id)
        .join(Classroom, Classroom.id == Assignment.classroom_id)
        .join(School, School.id == Classroom.school_id)
        .join(User, User.id == ProgressEvent.student_id)
        .join(
            ClassMembership,
            and_(
                ClassMembership.classroom_id == Classroom.id,
                ClassMembership.user_id == ProgressEvent.student_id,
                ClassMembership.role == "student",
            ),
        )
        .join(
            AssignmentTarget,
            and_(
                AssignmentTarget.assignment_id == Assignment.id,
                AssignmentTarget.student_id == ProgressEvent.student_id,
            ),
        )
        .where(
            Classroom.id.in_(teacher_class_scope(teacher_id)),
            Assignment.status == "assigned",
            ProgressEvent.event_type == "blocked",
        )
    )


def teacher_help_cursor(db: Session, teacher_id: int) -> int:
    query = _teacher_help_requests(teacher_id).with_only_columns(ProgressEvent.id)
    recent = db.scalar(
        query.where(
            ProgressEvent.created_at >= datetime.now(UTC) - timedelta(days=1),
            ~select(StudentSupportReply.id).where(
                StudentSupportReply.assignment_id == ProgressEvent.assignment_id,
                StudentSupportReply.student_id == ProgressEvent.student_id,
                StudentSupportReply.created_at >= ProgressEvent.created_at,
            ).exists(),
        ).order_by(ProgressEvent.id).limit(1)
    )
    if recent:
        return recent - 1
    return db.scalar(query.order_by(ProgressEvent.id.desc()).limit(1)) or 0


def teacher_help_alerts(db: Session, teacher_id: int, after: int) -> list[dict]:
    rows = db.execute(
        _teacher_help_requests(teacher_id)
        .where(ProgressEvent.id > after)
        .order_by(ProgressEvent.id)
        .limit(HELP_ALERT_BATCH)
    )
    return [
        {
            "id": event.id,
            "student": student,
            "classroom": classroom,
            "school": school,
            "assignment": assignment.title,
            "message": event.message or "The student asked for help.",
            "requested_at": NotificationService.format_deadline(
                event.created_at, assignment.timezone
            ),
            "due_at": NotificationService.format_deadline(assignment.due_at, assignment.timezone),
            "url": (
                f"/teacher/classes/{assignment.classroom_id}/students/{event.student_id}"
                f"?assignment_id={assignment.id}#assignment-{assignment.id}"
            ),
        }
        for event, assignment, classroom, school, student in rows
    ]


def workspace_version(db: Session, user: User, workspace: str) -> str:
    if workspace == "teacher":
        scope = [item.id for item in list_teacher_classes(db, user)]
        events = ActivityEvent.classroom_id.in_(scope)
        deliveries = NotificationDelivery.classroom_id.in_(scope)
    elif workspace == "coordinator":
        scope = [item.id for item in coordinated_schools(db, user)]
        events = ActivityEvent.school_id.in_(scope)
        deliveries = NotificationDelivery.school_id.in_(scope)
    else:
        scope = sorted(db.scalars(class_membership_scope(user.id, "student")))
        assignments = student_work_scope(user.id)
        states = select(StudentAssignmentState.id).where(
            StudentAssignmentState.student_id == user.id,
            StudentAssignmentState.assignment_id.in_(assignments),
        )
        feedback = select(Feedback.id).where(
            Feedback.student_id == user.id, Feedback.assignment_id.in_(assignments)
        )
        support = select(StudentSupportReply.id).where(
            StudentSupportReply.student_id == user.id,
            StudentSupportReply.assignment_id.in_(assignments),
        )
        events = or_(
            and_(ActivityEvent.actor_user_id == user.id, ActivityEvent.classroom_id.in_(scope)),
            and_(
                ActivityEvent.event_type.in_((
                    "assignment_created", "assignment_published", "assignment_deadline_updated",
                    "assignment_instructions_updated", "assignment_cancelled",
                )),
                ActivityEvent.entity_id.in_(assignments),
            ),
            and_(
                ActivityEvent.entity_type == "student_assignment_state",
                ActivityEvent.entity_id.in_(states),
            ),
            and_(ActivityEvent.entity_type == "feedback", ActivityEvent.entity_id.in_(feedback)),
            and_(
                ActivityEvent.entity_type == "student_support_reply",
                ActivityEvent.entity_id.in_(support),
            ),
            and_(ActivityEvent.event_type == "class_archived", ActivityEvent.classroom_id.in_(scope)),
        )
        deliveries = and_(
            NotificationDelivery.user_id == user.id,
            NotificationDelivery.assignment_id.in_(assignments),
        )
    activity = db.scalar(select(func.max(ActivityEvent.id)).where(events))
    outbox = db.execute(
        select(
            NotificationDelivery.status, func.count(), func.max(NotificationDelivery.id),
            func.max(NotificationDelivery.last_attempt_at),
        )
        .where(deliveries, NotificationDelivery.kind != "assignment_details")
        .group_by(NotificationDelivery.status)
        .order_by(NotificationDelivery.status)
    ).all()
    return sha256(repr((scope, activity, outbox)).encode()).hexdigest()


@router.get("/api/live/{workspace}")
def live_workspace_version(
    workspace: str, request: Request, db: Session = Depends(get_db),
    help_after: int | None = Query(default=None, ge=0, le=2**63 - 1),
):
    user = current_user(request, db)
    if workspace not in {"student", "teacher", "coordinator"}:
        raise HTTPException(404)
    if workspace not in workspace_roles(db, user):
        raise HTTPException(403)
    payload = {"version": workspace_version(db, user, workspace)}
    if workspace == "teacher" and help_after is not None:
        payload["help_requests"] = teacher_help_alerts(db, user.id, help_after)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})
