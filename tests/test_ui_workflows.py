from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models import Assignment, Feedback, NotificationDelivery
from app.services.submission import FeedbackService, SubmissionService
from tests.conftest import authenticate


def test_teacher_clarification_preserves_input_and_recipients(client, app, db, data):
    token = authenticate(client, app, data["teacher1"].id)
    before = db.scalar(select(func.count()).select_from(Assignment))
    response = client.post(
        "/teacher/assignments",
        data={
            "csrf_token": token,
            "classroom_id": data["class1"].id,
            "idempotency_key": "clarify-ui",
            "natural_text": "Finish the worksheet ???",
            "student_ids": data["student1"].id,
            "targeting_mode": "selected",
            "draft": "true",
        },
    )
    assert response.status_code == 422
    assert "Finish the worksheet ???</textarea>" in response.text
    assert 'aria-describedby="assignment-example natural_text-error"' in response.text
    assert f'value="{data["student1"].id}" checked' in response.text
    assert f'value="{data["student2"].id}" checked' not in response.text
    assert db.scalar(select(func.count()).select_from(Assignment)) == before


def test_manual_draft_reviews_recipient_and_schedules_only_on_publish(client, app, db, data):
    token = authenticate(client, app, data["teacher1"].id)
    deadline = (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(hours=20)).strftime(
        "%Y-%m-%dT%H:%M"
    )
    draft = client.post(
        "/teacher/assignments",
        data={
            "csrf_token": token,
            "classroom_id": data["class1"].id,
            "idempotency_key": "manual-draft-ui",
            "entry": "manual",
            "title": "Weather observations",
            "instructions": "Include temperatures.",
            "due_at": deadline,
            "student_ids": data["student1"].id,
            "targeting_mode": "selected",
            "draft": "true",
        },
    )
    assignment = db.scalar(select(Assignment).where(Assignment.title == "Weather observations"))
    assert assignment.status == "draft"
    assert "Instructions &amp; recipients" in draft.text
    assert data["student1"].name in draft.text and data["student2"].name not in draft.text
    assert "Publish assignment" in draft.text and "IST" in draft.text
    assert "This demo simulates Telegram delivery." in draft.text
    assert (
        db.scalar(
            select(NotificationDelivery).where(NotificationDelivery.assignment_id == assignment.id)
        )
        is None
    )

    published = client.post(
        f"/teacher/assignments/{assignment.id}/publish", data={"csrf_token": token}
    )
    assert published.status_code == 200
    assert "Assignment published. This demo simulates Telegram delivery." in published.text
    assert "Simulated" in published.text
    assert db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.assignment_id == assignment.id)
    )
    assert "can send <code>/assignments</code>" in published.text


def test_draft_can_be_retried_after_a_failed_write(client, app, db, data, monkeypatch):
    token = authenticate(client, app, data["teacher1"].id)
    deadline = (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(hours=20)).strftime(
        "%Y-%m-%dT%H:%M"
    )
    form = {
        "csrf_token": token,
        "classroom_id": data["class1"].id,
        "idempotency_key": "retry-draft-ui",
        "entry": "manual",
        "title": "Field journal",
        "instructions": "Compare two samples.",
        "due_at": deadline,
        "student_ids": data["student1"].id,
        "targeting_mode": "selected",
        "draft": "true",
    }

    def fail_write(*args, **kwargs):
        raise OperationalError(None, None, RuntimeError("database is locked"))

    with monkeypatch.context() as patch:
        patch.setattr(app.state.assignment_service, "create", fail_write)
        failed = client.post("/teacher/assignments", data=form)

    assert failed.status_code == 422
    assert "Your entries are still here" in failed.text
    assert 'value="retry-draft-ui"' in failed.text and 'value="Field journal"' in failed.text
    assert "Compare two samples.</textarea>" in failed.text and f'value="{deadline}"' in failed.text
    assert f'value="{data["student1"].id}" checked' in failed.text
    assert f'value="{data["student2"].id}" checked' not in failed.text

    retried = client.post("/teacher/assignments", data=form)
    assert retried.status_code == 200 and "Draft saved" in retried.text
    assert (
        db.scalar(
            select(func.count()).select_from(Assignment).where(Assignment.title == "Field journal")
        )
        == 1
    )


def test_feedback_conflict_preserves_unsaved_note(client, app, db, data):
    student, teacher = data["student1"], data["teacher1"]
    assignment_id = data["assignment"].id
    submission = SubmissionService().submit(
        db, student, assignment_id, "ui-first", text_content="First draft"
    )
    FeedbackService().create(db, teacher, submission.id, "Add an example", False, "ui-revision")
    SubmissionService().submit(db, student, assignment_id, "ui-second", text_content="New draft")
    db.commit()

    token = authenticate(client, app, teacher.id)
    response = client.post(
        f"/teacher/submissions/{submission.id}/feedback",
        data={
            "csrf_token": token,
            "idempotency_key": "ui-stale",
            "message": "Unsaved notes for the review",
            "outcome": "complete",
        },
    )
    assert response.status_code == 422
    assert "Review the latest submission" in response.text
    assert "Unsaved notes for the review</textarea>" in response.text
    assert db.scalar(select(func.count()).select_from(Feedback)) == 1


def test_browser_denial_gives_a_recovery_link(client, app, data):
    authenticate(client, app, data["teacher1"].id)
    denied = client.get(f"/teacher/classes/{data['class2'].id}", headers={"Accept": "text/html"})
    assert denied.status_code == 403
    assert "Go to my workspace" in denied.text
    assert data["class2"].name not in denied.text
    api = client.get(f"/api/teacher/classes/{data['class2'].id}", headers={"Accept": "text/html"})
    assert api.status_code == 403
    assert api.json()["detail"] == "Resource is outside your authorized scope"
