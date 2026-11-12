from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select

from app.models import (
    Assignment,
    ClassMembership,
    Classroom,
    ConversationContext,
    NotificationDelivery,
    SchoolMembership,
    Submission,
)
from app.reminders.scheduler import ReminderScheduler
from app.services.assignment import AssignmentService, get_student_state
from app.services.risk import RiskService
from app.telegram.service import TelegramService
from tests.conftest import authenticate

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/9r0AAAAASUVORK5CYII="
)


def update(number: int, sender: int, text: str = "", **details) -> dict:
    return {
        "update_id": number,
        "message": {
            "from": {"id": sender},
            "chat": {"id": sender, "type": "private"},
            "text": text,
            **details,
        },
    }


def test_attachment_waits_for_authorized_choice_across_restart(
    app, client, db, data, monkeypatch, tmp_path
):
    bot = app.state.telegram_client
    monkeypatch.setattr(bot.settings, "upload_dir", tmp_path)
    monkeypatch.setattr(bot.settings, "telegram_mode", "real")
    monkeypatch.setattr(bot.settings, "telegram_bot_token", "mock-bot-token")
    requested = []

    def transport(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.url.host == "api.telegram.org"
        if request.url.path.endswith("/getFile"):
            assert request.url.params["file_id"] == "photo-a"
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "photos/photo.png"}}
            )
        assert request.url.path.endswith("/photos/photo.png")
        return httpx.Response(200, content=PNG)

    with httpx.Client(transport=httpx.MockTransport(transport)) as mock:
        monkeypatch.setattr(bot, "http", mock)
        original = update(4001, 201, photo=[{"file_id": "photo-a"}])
        assert app.state.telegram_service.process(db, original)["result"] == "file_needs_context"
        assert app.state.telegram_service.process(db, original)["duplicate"]
        assert not requested
        db.commit()
        with app.state.session_factory.begin() as resumed:
            service = TelegramService(app.state.llm_service, bot)
            assert (
                service.process(resumed, update(4002, 201, "#9999"))["result"]
                == "error:AmbiguousReferenceError"
            )
            assert not requested
            expected = data["assignment"].id
            result = service.process(resumed, update(4003, 201, f"#{expected}"))
            assert result["result"].startswith("submission:")
            assert len(requested) == 2
        submission = db.scalar(select(Submission))
        assert submission.assignment_id == expected
        assert submission.content_hash == hashlib.sha256(PNG).hexdigest()
        assert Path(submission.stored_file_path).parent == tmp_path.resolve()
        assert len(Path(submission.stored_file_path).name) == 32
        authenticate(client, app, data["teacher1"].id)
        assert "submission-preview" in client.get(f"/teacher/assignments/{expected}").text
        file_url = f"/submissions/{submission.id}/file"
        assert client.get(file_url).content == PNG
        authenticate(client, app, data["student1"].id)
        assert client.get(file_url).content == PNG
        authenticate(client, app, data["student2"].id)
        assert client.get(file_url).status_code == 403


def test_ambiguous_teacher_class_requires_fresh_authorized_choice(app, db, data):
    second = Classroom(school_id=data["school1"].id, name="Grade 8 - B", grade="8")
    db.add(second)
    db.flush()
    db.add(ClassMembership(classroom_id=second.id, user_id=data["teacher1"].id, role="teacher"))
    db.commit()
    service = app.state.telegram_service
    assert (
        service.process(db, update(4010, 101, "Complete the worksheet by tomorrow evening"))[
            "result"
        ]
        == "teacher_needs_context"
    )
    assert list(db.scalars(select(Assignment))) == [data["assignment"]]
    assert (
        service.process(db, update(4011, 101, f"#{data['class2'].id}"))["result"]
        == "error:AmbiguousReferenceError"
    )
    result = service.process(db, update(4012, 101, f"#{data['class1'].id}"))["result"]
    assert result.startswith("assignment:")
    created = db.get(Assignment, int(result.split(":")[-1]))
    assert created.classroom_id == data["class1"].id


def test_teacher_edit_with_explicit_class_never_reuses_unrelated_focus(app, db, data, monkeypatch):
    from app.llm.schemas import TeacherIntent

    teacher = data["teacher1"]
    assignment = data["assignment"]
    service = app.state.telegram_service
    service.intent_router.context.set(db, teacher, "101", assignment_id=assignment.id)
    original = assignment.due_at
    monkeypatch.setattr(
        app.state.llm_service.provider,
        "interpret_teacher",
        lambda *_args: TeacherIntent(
            intent="update_assignment_deadline",
            assignment_reference="it",
            class_reference="Class Two",
            due_at=datetime.now(UTC) + timedelta(days=3),
            confidence=0.95,
        ),
    )
    result = service.process(db, update(4013, 101, "Move it in Class Two to tomorrow."))
    assert result["result"] == "error:NotFoundError"
    assert assignment.due_at == original


def test_new_assignment_does_not_replace_pending_student_photo(app, db, data):
    service = app.state.telegram_service
    photo = update(4020, 201, photo=[{"file_id": "held-file"}])
    assert service.process(db, photo)["result"] == "file_needs_context"
    assert service.process(db, update(4021, 101, "Finish the worksheet by tomorrow evening"))[
        "result"
    ].startswith("assignment:")
    context = db.scalar(
        select(ConversationContext).where(ConversationContext.user_id == data["student1"].id)
    )
    assert context.pending_action == "file_assignment"
    assert context.pending_payload_json["attachment"]["file_id"] == "held-file"


def test_coordinator_manual_run_and_risk_exclude_other_school(client, app, db, data, monkeypatch):
    now = datetime.now(UTC)
    teacher, other_teacher = data["teacher1"], data["teacher2"]
    student, other_student = data["student1"], data["student2"]
    db.add_all(
        [
            SchoolMembership(user_id=teacher.id, school_id=data["school1"].id, role="coordinator"),
            ClassMembership(
                user_id=other_student.id, classroom_id=data["class2"].id, role="student"
            ),
        ]
    )
    db.flush()
    db.delete(
        db.scalar(
            select(ClassMembership).where(
                ClassMembership.user_id == teacher.id,
                ClassMembership.role == "teacher",
            )
        )
    )
    assignment = data["assignment"]
    assignment.due_at = now + timedelta(hours=20)
    ReminderScheduler().schedule_assignment(db, assignment, now)
    other = AssignmentService().create(
        db,
        other_teacher,
        data["class2"].id,
        "Other school",
        "Write.",
        now + timedelta(hours=20),
        "Asia/Kolkata",
    )
    get_student_state(db, assignment.id, student.id).status = "blocked"
    get_student_state(db, other.id, other_student.id).status = "blocked"
    db.commit()
    assert RiskService().for_actor(db, teacher) == []
    monkeypatch.setattr(app.state.settings, "quiet_hour_start", 0)
    monkeypatch.setattr(app.state.settings, "quiet_hour_end", 0)
    csrf = authenticate(client, app, teacher.id)
    response = client.post(
        "/coordinator/reminders/run", data={"csrf_token": csrf, "school_id": data["school1"].id}, follow_redirects=False
    )
    assert response.status_code == 303
    assert set(db.scalars(select(NotificationDelivery.assignment_id))) == {assignment.id}


def test_idempotent_noop_instruction_update_cannot_undo_later_edit(db, data):
    service = AssignmentService()
    assignment, teacher = data["assignment"], data["teacher1"]
    original = assignment.instructions
    service.clarify(db, teacher, assignment.id, original, "first-noop")
    db.commit()
    service.clarify(db, teacher, assignment.id, "Include citations", "second")
    db.commit()
    service.clarify(db, teacher, assignment.id, original, "first-noop")
    assert assignment.instructions == "Include citations"


def test_real_callback_returns_telegram_ack_after_state_commit(client, app, db, data, monkeypatch):
    monkeypatch.setattr(app.state.settings, "telegram_mode", "real")
    assignment, student = data["assignment"], data["student1"]
    event = {
        "update_id": 4110,
        "callback_query": {
            "id": "cb-offline-test",
            "from": {"id": 201},
            "message": {"chat": {"id": 201, "type": "private"}},
            "data": f"ack:{assignment.id}",
        },
    }
    route = "/telegram/webhook/test-webhook-secret"
    header = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
    for _ in range(2):
        response = client.post(route, json=event, headers=header)
        assert response.json() == {
            "method": "answerCallbackQuery",
            "callback_query_id": "cb-offline-test",
        }
    assert get_student_state(db, assignment.id, student.id).status == "acknowledged"
    assert len(list(db.scalars(select(NotificationDelivery)))) == 1
