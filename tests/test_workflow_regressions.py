from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.models import Assignment, NotificationDelivery, Reminder, Submission
from app.services.assignment import AssignmentService, get_student_state
from app.services.overdue import OverdueService
from app.services.submission import SubmissionService
from tests.conftest import authenticate


def telegram_message(update_id: int, sender: int, text: str, chat_id: int | None = None) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": sender},
            "chat": {"id": chat_id or sender, "type": "group" if chat_id else "private"},
            "text": text,
        },
    }


def test_group_update_never_exposes_teacher_summary(app, db, data):
    response = app.state.telegram_service.process(db, telegram_message(1400, 101, "/status", -90))
    assert response["result"] == "ignored_non_private"
    assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 0


def test_invite_cannot_claim_an_unlinked_account_by_email(app, db, data):
    student = data["student2"]
    student.telegram_user_id = student.telegram_chat_id = None
    db.flush()
    result = app.state.telegram_service.process(
        db, telegram_message(1401, 30001, f"/join OTHER {student.email}")
    )
    assert result["result"] == "error:InviteError"
    assert student.telegram_user_id is None


def test_signed_in_teacher_connects_own_telegram(client, app, db, data):
    teacher = data["teacher1"]
    teacher.telegram_user_id = teacher.telegram_chat_id = None
    db.commit()
    csrf = authenticate(client, app, teacher.id)
    class_id = data["class1"].id
    unrelated = data["class2"].id
    assert (
        client.post(
            f"/teacher/classes/{unrelated}/connect-telegram", data={"csrf_token": csrf}
        ).status_code
        == 403
    )
    response = client.post(
        f"/teacher/classes/{class_id}/connect-telegram", data={"csrf_token": csrf}
    )
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    token = response.text.split("?start=", 1)[1].split('"', 1)[0]
    result = app.state.telegram_service.process(
        db, telegram_message(1402, 30002, f"/start {token}")
    )
    assert result["result"] == f"telegram_link:connected:{teacher.id}"
    assert teacher.telegram_chat_id == "30002"


def test_draft_is_private_until_published_and_uses_utc(client, app, db, data):
    teacher, student = data["teacher1"], data["student1"]
    local_deadline = datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=3)
    service = app.state.assignment_service
    draft = service.create(
        db,
        teacher,
        data["class1"].id,
        "Draft work",
        "Draft instructions",
        local_deadline,
        "Asia/Kolkata",
        draft=True,
    )
    draft_id = draft.id
    db.commit()
    db.expire(draft)
    assert draft.due_at.replace(tzinfo=UTC) == local_deadline.astimezone(UTC)
    assert db.scalar(select(Reminder).where(Reminder.assignment_id == draft_id)) is None
    authenticate(client, app, student.id)
    assert client.get(f"/student/assignments/{draft_id}").status_code == 404
    service.publish(db, teacher, draft_id)
    service.publish(db, teacher, draft_id)
    db.commit()
    assert db.get(Assignment, draft_id).status == "assigned"
    assert (
        db.scalar(
            select(func.count()).select_from(Reminder).where(Reminder.assignment_id == draft_id)
        )
        == 6
    )
    assert client.get(f"/student/assignments/{draft_id}").status_code == 200


def test_student_cannot_mark_submission_completed(client, app, db, data):
    student, assignment = data["student1"], data["assignment"]
    SubmissionService().submit(db, student, assignment.id, "single-answer", text_content="Answer")
    db.commit()
    csrf = authenticate(client, app, student.id)
    response = client.post(
        f"/student/assignments/{assignment.id}/progress",
        data={
            "csrf_token": csrf,
            "status": "completed",
            "message": "I'm trying to complete it.",
        },
    )
    assert response.status_code == 422
    assert get_student_state(db, assignment.id, student.id).status == "submitted"
    assert db.scalar(select(func.count()).select_from(Submission)) == 1


def test_future_deadline_restores_previous_blocked_state(db, data):
    assignment = data["assignment"]
    student = data["student1"]
    state = get_student_state(db, assignment.id, student.id)
    now = datetime.now(UTC)
    state.status = "blocked"
    state.last_activity_at = now - timedelta(hours=2)
    state.block_reason = "Need a hint"
    assignment.due_at = now - timedelta(minutes=5)
    assert OverdueService().mark_overdue(db, assignment, state, now)
    db.flush()
    AssignmentService().update_deadline(
        db, data["teacher1"], assignment.id, now + timedelta(days=1)
    )
    assert state.status == "blocked" and state.block_reason == "Need a hint"
    assert state.last_activity_at == now - timedelta(hours=2)
