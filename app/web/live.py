from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.auth.security import current_user
from app.database import get_db
from app.models import (
    ActivityEvent,
    Feedback,
    NotificationDelivery,
    StudentAssignmentState,
    StudentSupportReply,
    User,
)
from app.services.authorization import (
    class_membership_scope,
    coordinated_schools,
    list_teacher_classes,
    student_work_scope,
    workspace_roles,
)

router = APIRouter()


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
):
    user = current_user(request, db)
    if workspace not in {"student", "teacher", "coordinator"}:
        raise HTTPException(404)
    if workspace not in workspace_roles(db, user):
        raise HTTPException(403)
    return JSONResponse(
        {"version": workspace_version(db, user, workspace)}, headers={"Cache-Control": "no-store"}
    )
