from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.exceptions import AuthorizationError, ValidationError
from app.models import (
    Assignment,
    Classroom,
    Feedback,
    NotificationDelivery,
    School,
    Submission,
    User,
)
from app.services.authorization import (
    list_teacher_classes,
    require_staff_submission,
    require_teacher_class,
)
from app.services.submission_history import feedback_outcomes, submission_attempts
from app.services.workload import pending_reviews
from app.status_labels import REVIEW_STATUS
from app.web.files import INLINE_IMAGE_TYPES
from app.web.helpers import coordinator_school, csrf, operation_key, render, web_user

router = APIRouter()


@router.get("/teacher/submissions", response_class=HTMLResponse)
def teacher_review_queue(
    request: Request,
    class_id: int = 0,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    if "teacher" not in request.state.workspace_roles:
        raise HTTPException(403, "A teaching membership is required to open the review queue.")
    classes = list_teacher_classes(db, user)
    classroom = require_teacher_class(db, user, class_id) if class_id else None
    class_names = {item.id: item.name for item in classes}
    rows = db.execute(pending_reviews([class_id] if class_id else list(class_names))).all()
    return render(
        request,
        "staff/submissions.html",
        classroom=classroom,
        classes=classes,
        class_id=class_id,
        rows=[
            {
                "id": submission.id,
                "created_at": submission.created_at,
                "assignment_title": assignment.title,
                "timezone": assignment.timezone,
                "class_name": class_names[assignment.classroom_id],
                "student_name": student.name,
                "latest": True,
                "review_status": "awaiting",
            }
            for submission, assignment, student in rows
        ],
    )


@router.get("/coordinator/submissions", response_class=HTMLResponse)
def school_submission_queue(
    request: Request,
    school_id: int | None = None,
    class_id: int = 0,
    assignment_id: int = 0,
    student_id: int = 0,
    status: str = "",
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    schools = getattr(request.state, "managed_schools", ())
    if not schools:
        raise AuthorizationError("A coordinator membership is required to open school submissions.")
    school = coordinator_school(
        request, db, user, schools[0].id if school_id is None else school_id
    )
    classes = list(
        db.scalars(
            select(Classroom).where(Classroom.school_id == school.id).order_by(Classroom.name)
        )
    )
    class_names = {item.id: item.name for item in classes}
    if class_id and class_id not in class_names:
        raise AuthorizationError("Choose a class in the selected school.")
    if status and status not in REVIEW_STATUS:
        raise ValidationError("Choose a review status from the list.")

    assignments = list(
        db.scalars(
            select(Assignment)
            .where(Assignment.classroom_id.in_(class_names), Assignment.status != "draft")
            .order_by(Assignment.title, Assignment.id)
        )
    )
    students = db.execute(
        select(User.id, User.name)
        .join(Submission, Submission.student_id == User.id)
        .join(Assignment, Assignment.id == Submission.assignment_id)
        .where(Assignment.classroom_id.in_(class_names))
        .distinct()
        .order_by(User.name, User.id)
    ).all()
    if assignment_id and assignment_id not in {item.id for item in assignments}:
        raise AuthorizationError("Choose an assignment in the selected school.")
    if student_id and student_id not in {item.id for item in students}:
        raise AuthorizationError("Choose a student with submissions in the selected school.")

    attempts = submission_attempts([class_id] if class_id else list(class_names)).subquery()
    query = select(attempts).order_by(attempts.c.created_at.desc(), attempts.c.id.desc())
    if assignment_id:
        query = query.where(attempts.c.assignment_id == assignment_id)
    if student_id:
        query = query.where(attempts.c.student_id == student_id)
    if status:
        query = query.where(attempts.c.review_status == status)
    return render(
        request,
        "staff/submissions.html",
        school=school,
        school_switch_action="/coordinator/submissions",
        classes=classes,
        class_names=class_names,
        assignments=assignments,
        students=students,
        class_id=class_id,
        assignment_id=assignment_id,
        student_id=student_id,
        status=status,
        review_statuses=REVIEW_STATUS,
        rows=db.execute(query).all(),
    )


@router.get("/teacher/submissions/{submission_id}", response_class=HTMLResponse)
@router.get("/coordinator/submissions/{submission_id}", response_class=HTMLResponse)
def submission_detail(
    request: Request,
    submission_id: int,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return render_submission(request, user, db, submission_id)


def render_submission(
    request, user, db, submission_id, *, error=None, message="", feedback_key=None
):
    coordinating = request.url.path.startswith("/coordinator/")
    submission = require_staff_submission(db, user, submission_id, coordinator=coordinating)
    assignment = db.get(Assignment, submission.assignment_id)
    classroom = db.get(Classroom, assignment.classroom_id)
    school = (
        coordinator_school(request, db, user, classroom.school_id)
        if coordinating
        else db.get(School, classroom.school_id)
    )
    attempts = db.execute(
        submission_attempts([classroom.id])
        .where(
            Submission.assignment_id == assignment.id,
            Submission.student_id == submission.student_id,
        )
        .order_by(Submission.id.desc())
    ).all()
    attempt_number, selected = next(
        (number, attempt)
        for number, attempt in enumerate(reversed(attempts), start=1)
        if attempt.id == submission_id
    )
    feedback = db.execute(
        select(Feedback, User.name)
        .join(User, User.id == Feedback.teacher_id)
        .where(
            Feedback.assignment_id == assignment.id, Feedback.student_id == submission.student_id
        )
        .order_by(Feedback.id.desc())
    ).all()
    deliveries = {
        delivery.idempotency_key: delivery
        for delivery in db.scalars(
            select(NotificationDelivery).where(
                NotificationDelivery.assignment_id == assignment.id,
                NotificationDelivery.school_id == school.id,
                NotificationDelivery.classroom_id == classroom.id,
                NotificationDelivery.user_id == submission.student_id,
                NotificationDelivery.idempotency_key.in_(
                    [f"feedback:{review.idempotency_key or review.id}" for review, _ in feedback]
                ),
            )
        )
    }
    response = render(
        request,
        "staff/submission.html",
        status_code=422 if error else 200,
        school=school,
        classroom=classroom,
        assignment=assignment,
        submission=submission,
        selected=selected,
        attempt_number=attempt_number,
        attempts=attempts,
        feedback=feedback,
        feedback_outcomes=feedback_outcomes(db, assignment, [review.id for review, _ in feedback]),
        delivery_keys=deliveries,
        inline_image_types=INLINE_IMAGE_TYPES,
        csrf_token=csrf(request),
        feedback_key=feedback_key or operation_key("feedback"),
        error=error,
        draft_feedback=message,
        message=request.query_params.get("message"),
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response
