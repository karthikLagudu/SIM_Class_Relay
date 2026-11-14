import asyncio
import re

import pytest
from sqlalchemy import select

from app.models import (
    ClassMembership,
    NotificationDelivery,
    ProcessedTelegramUpdate,
    ProgressEvent,
    SchoolMembership,
    Submission,
)
from app.services.delivery import DeliveryProcessor
from app.telegram.polling import poll_telegram
from tests.conftest import authenticate
from tests.test_telegram import latest_delivery, message, tap


def test_direct_telegram_polling_commits_updates_before_advancing_offset(app, db, data, monkeypatch):
    assignment_id = data["assignment"].id
    requests = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            update = tap(9000, 201, f"ack:{assignment_id}")
            update["callback_query"]["id"] = "test-callback"
            return {"result": [update]}

    class BotApi:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            method = url.rsplit("/", 1)[-1]
            requests.append((method, json))
            if len(requests) == 3:
                raise asyncio.CancelledError
            return Response()

    monkeypatch.setattr("app.telegram.polling.httpx.AsyncClient", BotApi)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poll_telegram(app))
    assert db.get(ProcessedTelegramUpdate, 9000)
    assert db.scalar(select(ProgressEvent.event_type)) == "acknowledged"
    assert requests[0][1]["offset"] is None
    assert requests[1] == ("answerCallbackQuery", {"callback_query_id": "test-callback"})
    assert requests[2][1]["offset"] == 9001


def test_telegram_progress_and_submission_refresh_the_authorized_web_views(client, app, db, data):
    class_id = data["class1"].id
    assignment_id = data["assignment"].id
    assert client.get("/api/live/teacher").status_code == 401
    authenticate(client, app, data["teacher2"].id)
    unrelated = client.get("/api/live/teacher").json()
    authenticate(client, app, data["student2"].id)
    classmate = client.get("/api/live/student").json()
    assert client.get("/api/live/coordinator").status_code == 403
    authenticate(client, app, data["teacher1"].id)
    version = client.get("/api/live/teacher").json()["version"]
    page = client.get(f"/teacher/classes/{class_id}/assignments/{assignment_id}")
    assert f'data-live-version="{version}"' in page.text

    service = app.state.telegram_service
    assert service.process(db, tap(9001, 201, f"progress:{assignment_id}"))["result"] == (
        f"pending_progress:{assignment_id}"
    )
    assert service.process(db, message(9002, 201, "65%"))["result"] == (
        f"progress:{assignment_id}:in_progress"
    )
    db.commit()
    assert db.scalar(select(ProgressEvent.progress_percent)) == 65
    changed = client.get("/api/live/teacher").json()["version"]
    assert changed != version

    assert service.process(db, message(9003, 201, f"/submit {assignment_id} My answer"))[
        "result"
    ].startswith("submission:")
    db.commit()
    assert client.get("/api/live/teacher").json()["version"] != changed
    queue = client.get("/teacher/submissions")
    submission_id = db.scalar(select(Submission.id))
    assert f'/teacher/submissions/{submission_id}' in queue.text
    assert "My answer" in client.get(f"/teacher/submissions/{submission_id}").text

    authenticate(client, app, data["teacher2"].id)
    assert client.get("/api/live/teacher").json() == unrelated
    authenticate(client, app, data["student2"].id)
    assert client.get("/api/live/student").json() == classmate


def test_teacher_help_alerts_include_web_and_telegram_requests_in_class_scope(client, app, db, data):
    assignment = data["assignment"]
    class_id = data["class1"].id
    teacher = data["teacher1"]
    student = data["student1"]
    feed = "/api/live/teacher?help_after=0"
    authenticate(client, app, teacher.id)
    page = client.get(f"/teacher/classes/{class_id}/assignments/new")
    assert 'data-teacher-help-cursor="0"' in page.text
    assert client.get(feed).json()["help_requests"] == []

    token = authenticate(client, app, student.id)
    web_message = "I’m stuck on question 2.\nWhat does <example> mean?"
    result = client.post(
        f"/student/assignments/{assignment.id}/progress",
        data={"csrf_token": token, "status": "blocked", "message": web_message},
        follow_redirects=False,
    )
    assert result.status_code == 303
    assert client.get(feed).status_code == 403

    telegram_message = "I also need help with the diagram."
    app.state.telegram_service.process(
        db, message(9130, 201, f"/blocked {assignment.id} {telegram_message}")
    )
    db.commit()
    authenticate(client, app, data["teacher2"].id)
    assert client.get(feed).json()["help_requests"] == []

    authenticate(client, app, teacher.id)
    response = client.get(feed)
    assert response.headers["cache-control"] == "no-store"
    first, second = response.json()["help_requests"]
    assert [first["message"], second["message"]] == [web_message, telegram_message]
    assert first["student"] == student.name
    assert first["classroom"] == data["class1"].name and first["school"] == data["school1"].name
    assert first["assignment"] == assignment.title
    assert "IST" in first["requested_at"] and "IST" in first["due_at"]
    assert client.get(first["url"]).status_code == 200
    reopened = client.get("/teacher")
    assert f'data-teacher-help-cursor="{first["id"] - 1}"' in reopened.text
    assert client.get(f"/api/live/teacher?help_after={first['id']}").json()["help_requests"] == [second]
    assert client.get(f"/api/live/teacher?help_after={second['id']}").json()["help_requests"] == []

    membership = db.scalar(select(ClassMembership).where(
        ClassMembership.classroom_id == class_id, ClassMembership.user_id == teacher.id,
        ClassMembership.role == "teacher",
    ))
    db.delete(membership)
    db.commit()
    assert client.get(feed).json()["help_requests"] == []


def test_web_submission_and_feedback_queue_real_telegram_updates(client, app, db, data, monkeypatch):
    telegram = app.state.telegram_client
    monkeypatch.setattr(telegram.settings, "telegram_mode", "real")
    sent = []

    def send(chat_id, body, markup):
        sent.append((chat_id, body, markup))
        return str(len(sent))

    monkeypatch.setattr(telegram, "send_message", send)
    student, teacher = data["student1"], data["teacher1"]
    assignment_id = data["assignment"].id
    token = authenticate(client, app, student.id)
    response = client.post(
        f"/student/assignments/{assignment_id}/submit",
        data={"text_content": "Written on the web", "csrf_token": token, "idempotency_key": "sync-web"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    submission = db.scalar(select(Submission))
    worker = DeliveryProcessor(telegram, app.state.session_factory)
    assert len(worker.run()) == 2
    assert {(chat, body.splitlines()[0]) for chat, body, _ in sent} == {
        ("101", "Work turned in · Student One"),
        ("201", "Work turned in · Essay"),
    }
    assert worker.run() == []

    token = authenticate(client, app, teacher.id)
    feedback = client.post(
        f"/teacher/submissions/{submission.id}/feedback",
        data={
            "csrf_token": token, "outcome": "revision", "message": "Please add a source.",
            "idempotency_key": "sync-feedback",
        },
        follow_redirects=False,
    )
    assert feedback.status_code == 303
    assert len(worker.run()) == 1
    assert sent[-1][:2] == ("201", "Revision requested · Essay\n\nPlease add a source.")
    assert sent[-1][2]["inline_keyboard"][0][0]["callback_data"] == f"assignment:{assignment_id}"

    service = app.state.telegram_service
    assert service.process(db, tap(9100, 201, "feedback"))["result"] == "feedback"
    assert "Please add a source." in latest_delivery(db).body


def test_teacher_can_publish_from_menu_and_students_get_delivery(client, app, db, data, monkeypatch):
    telegram = app.state.telegram_client
    monkeypatch.setattr(telegram.settings, "telegram_mode", "real")
    assignment_class = data["class1"].id
    service = app.state.telegram_service
    authenticate(client, app, data["student1"].id)
    before = client.get("/api/live/student").json()
    assert service.process(db, tap(9300, 101, "assign"))["result"] == "assign_help"
    assert service.process(db, tap(9301, 101, f"assign_to:{assignment_class}"))["result"] == (
        "pending_assignment"
    )
    result = service.process(db, message(9302, 101, "Read chapter 3 by tomorrow at 5 pm."))["result"]
    assert result == "assignment_preview"
    assert client.get("/api/live/student").json() == before
    context = service.intent_router.context.get(db, data["teacher1"], "101")
    nonce = context.pending_payload_json["nonce"]
    result = service.process(db, tap(9303, 101, f"publish_draft:{nonce}"))["result"]
    assert result.startswith("assignment:")
    db.commit()
    assert client.get("/api/live/student").json() != before
    new_id = int(result.split(":")[1])
    assert "Read Chapter 3" in client.get(f"/student/assignments/{new_id}").text
    rows = db.execute(
        select(NotificationDelivery.kind, NotificationDelivery.status, NotificationDelivery.chat_id)
        .where(NotificationDelivery.assignment_id == new_id)
    ).all()
    assert set(rows) == {
        ("assignment_created", "pending", "201"),
        ("assignment_created", "pending", "202"),
        ("teacher_confirmation", "pending", "101"),
    }


def test_coordinator_can_link_and_only_see_authorized_school_updates(client, app, db, data, monkeypatch):
    coordinator = data["teacher2"]
    coordinator.telegram_user_id = coordinator.telegram_chat_id = None
    db.add(SchoolMembership(school_id=data["school2"].id, user_id=coordinator.id, role="coordinator"))
    db.commit()
    monkeypatch.setattr(app.state.settings, "telegram_mode", "real")
    token = authenticate(client, app, coordinator.id)
    class_id = data["class2"].id
    dashboard = client.get("/coordinator")
    assert "Connect my Telegram" in dashboard.text
    foreign = client.post(
        f"/coordinator/classes/{data['class1'].id}/connect-telegram",
        data={"csrf_token": token},
    )
    assert foreign.status_code == 403
    link = client.post(f"/coordinator/classes/{class_id}/connect-telegram", data={"csrf_token": token})
    start = re.search(r'value="(/start [^"]+)"', link.text).group(1)
    service = app.state.telegram_service
    assert service.process(db, message(9200, 2020, start))["result"] == (
        f"telegram_link:connected:{coordinator.id}"
    )
    assert "My schools" in repr(latest_delivery(db).reply_markup)
    assert service.process(db, tap(9201, 2020, "schools"))["result"] == "schools"
    assert "School Two" in latest_delivery(db).body and "School One" not in latest_delivery(db).body
    assert service.process(db, tap(9202, 2020, "reviews"))["result"] == "reviews"
    assert "no work awaiting review" in latest_delivery(db).body
    service.process(db, message(9203, 201, f"/submit {data['assignment'].id} An answer"))
    db.commit()
    assert not db.scalar(select(NotificationDelivery.id).where(
        NotificationDelivery.user_id == coordinator.id, NotificationDelivery.kind == "staff_submission"
    ))


def test_staff_help_alerts_recheck_school_access_before_sending(app, db, data, monkeypatch):
    telegram = app.state.telegram_client
    monkeypatch.setattr(telegram.settings, "telegram_mode", "real")
    coordinator = data["teacher2"]
    membership = SchoolMembership(
        school_id=data["school1"].id, user_id=coordinator.id, role="coordinator"
    )
    db.add(membership)
    db.commit()
    assignment_id = data["assignment"].id
    service = app.state.telegram_service
    assert service.process(db, tap(9400, 201, f"ask:{assignment_id}"))["result"] == (
        f"progress:{assignment_id}:blocked"
    )
    db.commit()
    queued = db.scalars(select(NotificationDelivery).where(
        NotificationDelivery.kind == "staff_help"
    )).all()
    assert {alert.user_id for alert in queued} == {data["teacher1"].id, coordinator.id}
    event = db.scalars(select(ProgressEvent)).one()
    assert "A note may follow." in event.message
    service.process(db, tap(9402, 201, f"ask:{assignment_id}"))
    assert service.process(db, message(9401, 201, "Can you explain question 3?"))["result"] == (
        f"progress:{assignment_id}:blocked"
    )
    db.commit()
    assert db.scalars(select(ProgressEvent)).one().message == "Can you explain question 3?"
    alerts = db.scalars(select(NotificationDelivery).where(
        NotificationDelivery.kind == "staff_help"
    )).all()
    assert {alert.user_id for alert in alerts} == {data["teacher1"].id, coordinator.id}
    db.delete(membership)
    db.commit()
    sent = []

    def send(chat_id, body, markup):
        sent.append((chat_id, body))
        return str(len(sent))

    monkeypatch.setattr(telegram, "send_message", send)
    DeliveryProcessor(telegram, app.state.session_factory).run()
    assert {chat for chat, _ in sent} == {"101", "201"}
    assert all("Can you explain question 3?" not in body for _, body in sent)
    for alert in alerts:
        db.refresh(alert)
    assert {alert.user_id: alert.status for alert in alerts} == {
        data["teacher1"].id: "sent", coordinator.id: "skipped",
    }
