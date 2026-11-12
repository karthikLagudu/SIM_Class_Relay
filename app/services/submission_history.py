from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import aliased

from app.models import (
    ActivityEvent,
    Assignment,
    Classroom,
    Feedback,
    StudentAssignmentState,
    Submission,
    User,
)
from app.services.workload import pending_reviews


def feedback_outcomes(db, assignment: Assignment, feedback_ids):
    if not feedback_ids:
        return {}
    return {
        event.entity_id: event.metadata_json.get("complete")
        for event in db.scalars(
            select(ActivityEvent)
            .where(
                ActivityEvent.school_id == assignment.school_id,
                ActivityEvent.classroom_id == assignment.classroom_id,
                ActivityEvent.entity_type == "feedback",
                ActivityEvent.event_type == "feedback_sent",
                ActivityEvent.entity_id.in_(feedback_ids),
            )
            .order_by(ActivityEvent.id)
        )
    }


def submission_attempts(classroom_ids):
    attempt = aliased(Submission)
    latest_id = (
        select(func.max(attempt.id))
        .where(
            attempt.assignment_id == Submission.assignment_id,
            attempt.student_id == Submission.student_id,
        )
        .correlate(Submission)
        .scalar_subquery()
    )
    latest_feedback = (
        select(func.max(Feedback.id))
        .where(Feedback.submission_id == Submission.id)
        .correlate(Submission)
        .scalar_subquery()
    )
    outcome = (
        select(ActivityEvent.metadata_json["complete"].as_boolean())
        .where(
            ActivityEvent.entity_id == latest_feedback,
            ActivityEvent.entity_type == "feedback",
            ActivityEvent.event_type == "feedback_sent",
            ActivityEvent.school_id == Assignment.school_id,
            ActivityEvent.classroom_id == Assignment.classroom_id,
        )
        .correlate(Submission, Assignment)
        .order_by(ActivityEvent.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    latest = Submission.id == latest_id
    awaiting = pending_reviews(classroom_ids).with_only_columns(Submission.id).order_by(None)
    status = case(
        (Submission.id.in_(awaiting), "awaiting"),
        (outcome.is_(True), "complete"),
        (outcome.is_(False), "revision"),
        (and_(latest, StudentAssignmentState.status == "completed"), "complete"),
        (and_(latest, StudentAssignmentState.status == "needs_revision"), "revision"),
        (latest_feedback.is_not(None), "feedback"),
        else_="closed",
    )
    return (
        select(
            Submission.id,
            Submission.created_at,
            Assignment.id.label("assignment_id"),
            Assignment.title.label("assignment_title"),
            Assignment.timezone,
            Classroom.id.label("class_id"),
            Classroom.name.label("class_name"),
            Classroom.archived_at,
            User.id.label("student_id"),
            User.name.label("student_name"),
            latest.label("latest"),
            status.label("review_status"),
        )
        .select_from(Submission)
        .join(Assignment, Assignment.id == Submission.assignment_id)
        .join(Classroom, Classroom.id == Assignment.classroom_id)
        .join(User, User.id == Submission.student_id)
        .outerjoin(
            StudentAssignmentState,
            and_(
                StudentAssignmentState.assignment_id == Submission.assignment_id,
                StudentAssignmentState.student_id == Submission.student_id,
            ),
        )
        .where(Classroom.id.in_(classroom_ids))
    )
