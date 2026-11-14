import json
import re
from datetime import UTC, datetime, timedelta

import httpx2 as httpx
import pytest
from sqlalchemy import select

from app.models import (
    ClassMembership,
    NotificationDelivery,
    SchoolMembership,
    StudentSupportReply,
)
from app.services.delivery import DeliveryProcessor
from app.services.progress import ProgressService
from tests.conftest import authenticate


@pytest.mark.parametrize(
    ("name", "content", "content_type", "display"),
    [
        ("diagram.png", b"\x89PNG\r\n\x1a\nexample", "image/png", "image/png"),
        ("notes.txt", b"Text notes", "text/plain", "application/octet-stream"),
        (
            "unsafe.svg",
            b"<svg><script>alert(1)</script></svg>",
            "image/svg+xml",
            "application/octet-stream",
        ),
    ],
)
def test_teacher_support_reply_and_shared_history(
    client, app, db, data, tmp_path, monkeypatch, name, content, content_type, display
):
    teacher, other_teacher = data["teacher1"], data["teacher2"]
    student, peer = data["student1"], data["student2"]
    classroom, assignment = data["class1"], data["assignment"]
    db.add_all(
        [
            SchoolMembership(school_id=classroom.school_id, user_id=teacher.id, role="coordinator"),
            SchoolMembership(
                school_id=data["school2"].id, user_id=other_teacher.id, role="coordinator"
            ),
        ]
    )
    ProgressService().update(
        db, student, assignment.id, "blocked", "I’m stuck on the third question."
    )
    db.commit()
    monkeypatch.setattr(app.state.settings, "upload_dir", tmp_path)
    teacher_profile = f"/teacher/classes/{classroom.id}/students/{student.id}"
    coordinator_profile = teacher_profile.replace("teacher", "coordinator", 1)
    response_url = f"{teacher_profile}/assignments/{assignment.id}/support"
    student_assignment = f"/student/assignments/{assignment.id}"

    csrf = authenticate(client, app, teacher.id)
    listing = client.get(f"/teacher/assignments/{assignment.id}")
    assert f"{teacher_profile}?assignment_id={assignment.id}" in listing.text
    profile = client.get(f"{teacher_profile}?assignment_id={assignment.id}")
    assert profile.status_code == 200 and "I’m stuck on the third question." in profile.text
    key = re.search(r'name="idempotency_key" value="([^"]+)"', profile.text)[1]
    data_fields = {"csrf_token": csrf, "idempotency_key": key, "message": "Start with the example."}
    assert client.post(response_url, data={**data_fields, "csrf_token": "bad"}).status_code == 403
    failed = client.post(response_url, data=data_fields, files={"file": ("empty.txt", b"")})
    assert failed.status_code == 422 and data_fields["message"] in failed.text
    for _ in range(2):
        result = client.post(
            response_url, data=data_fields, files={"file": (name, content, content_type)}
        )
        assert result.status_code == 200 and data_fields["message"] in result.text
    assert len(list(tmp_path.iterdir())) == 1
    row = db.scalars(select(StudentSupportReply)).one()
    message_delivery, file_delivery = db.scalars(
        select(NotificationDelivery).where(NotificationDelivery.kind == "student_support")
        .order_by(NotificationDelivery.id)
    ).all()
    assert all(item.user_id == student.id and item.status == "logged" for item in (message_delivery, file_delivery))
    assert "Start with the example." in message_delivery.body and name in message_delivery.body
    assert file_delivery.idempotency_key == f"support-reply:{row.id}:attachment"
    coordinator = client.get(coordinator_profile)
    assert name in coordinator.text and data_fields["message"] in coordinator.text
    assert "Send help to student" not in coordinator.text
    file_path = f"/support-replies/{row.id}/file"
    assert client.get(f"/coordinator{file_path}").content == content


    authenticate(client, app, other_teacher.id)
    for url in (
        teacher_profile,
        coordinator_profile,
        f"/teacher{file_path}",
        f"/coordinator{file_path}",
    ):
        assert client.get(url).status_code == 403

    authenticate(client, app, peer.id)
    assert "Start with the example." not in client.get(student_assignment).text
    assert client.get(f"/student{file_path}").status_code == 403
    authenticate(client, app, student.id)
    student_view = client.get(student_assignment)
    assert "Help from your teacher" in student_view.text and name in student_view.text
    attachment = client.get(f"/student{file_path}")
    assert attachment.content == content and attachment.headers["content-type"] == display
    assert attachment.headers["cache-control"] == "private, no-store"
    if display == "image/png":
        assert f'<img class="submission-preview" src="/student{file_path}"' in student_view.text
    else:
        assert "Help attachment:" not in student_view.text
        assert attachment.headers["content-disposition"].startswith("attachment")

    db.delete(
        db.scalar(
            select(ClassMembership).where(
                ClassMembership.classroom_id == classroom.id,
                ClassMembership.user_id == student.id,
            )
        )
    )
    db.commit()
    assert client.get(f"/student{file_path}").status_code == 403
    authenticate(client, app, teacher.id)
    assert "Former enrollment" in client.get(coordinator_profile).text
    assert client.get(f"/coordinator{file_path}").content == content


@pytest.mark.parametrize("filename", [None, "diagram.png", "instructions.pdf"])
def test_telegram_receives_full_help_note_and_original_file(
    client, app, db, data, tmp_path, monkeypatch, filename,
):
    teacher, student = data["teacher1"], data["student1"]
    assignment, classroom, school = data["assignment"], data["class1"], data["school1"]
    token = authenticate(client, app, teacher.id)
    settings = app.state.settings
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    monkeypatch.setattr(settings, "telegram_mode", "real")
    monkeypatch.setattr(settings, "telegram_bot_token", "mock-api-token")
    note = "💡 Try the example first.\n" + "Read the example again. " * 200 + "\nCheck your final answer."
    path = f"/teacher/classes/{classroom.id}/students/{student.id}/assignments/{assignment.id}/support"
    fields = {"csrf_token": token, "idempotency_key": "support-delivery", "message": note}
    content = b"\x89PNG\r\n\x1a\ntest-diagram-contents" if filename == "diagram.png" else b"%PDF-1.4 mock"
    files = {"file": (filename, content)} if filename else None
    for _ in range(2):
        assert client.post(path, data=fields, files=files).status_code == 200

    sent = []
    upload_attempts = []

    def telegram_api(request):
        assert request.url.host == "api.telegram.org"
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
        else:
            assert request.url.path.endswith("/sendDocument")
            assert b'multipart/form-data' in request.headers["content-type"].encode()
            assert filename.encode() in request.content and content in request.content
            assert f'\r\n\r\n{student.telegram_chat_id}\r\n'.encode() in request.content
            assert f"Help attachment from {teacher.name}".encode() in request.content
            upload_attempts.append(request)
            if len(upload_attempts) == 1:
                return httpx.Response(429)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 500 + len(sent)}})

    telegram = app.state.telegram_client
    with httpx.Client(transport=httpx.MockTransport(telegram_api)) as api:
        monkeypatch.setattr(telegram, "http", api)
        processor = DeliveryProcessor(telegram, app.state.session_factory)
        now = datetime.now(UTC)
        assert len(processor.run(now)) == 2 + bool(filename)
        assert len(processor.run(now + timedelta(minutes=2))) == bool(filename)
        assert processor.run(now + timedelta(minutes=5)) == []

    assert len(sent) == 2 and len(upload_attempts) == (2 if filename else 0)
    assert all(message["chat_id"] == student.telegram_chat_id for message in sent)
    full_message = "".join(message["text"] for message in sent)
    assert full_message.endswith(note)
    for text in (teacher.name, student.name, school.name, classroom.name, assignment.title, "IST"):
        assert text in full_message
    assert sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"assignment:{assignment.id}"
    db.expire_all()
    view = client.get(path.split("/assignments/")[0])
    assert "Telegram message" in view.text and "Sent" in view.text
    if filename:
        assert "Telegram attachment" in view.text and filename in full_message


@pytest.mark.parametrize("access", ["unlinked", "relocated", "removed"])
def test_private_help_attachment_is_not_sent_without_current_access(
    client, app, db, data, tmp_path, monkeypatch, access,
):
    student, classroom, assignment = data["student1"], data["class1"], data["assignment"]
    token = authenticate(client, app, data["teacher1"].id)
    telegram = app.state.telegram_client
    monkeypatch.setattr(telegram.settings, "upload_dir", tmp_path)
    monkeypatch.setattr(telegram.settings, "telegram_mode", "real")
    if access == "unlinked":
        student.telegram_chat_id = None
        db.commit()
    path = f"/teacher/classes/{classroom.id}/students/{student.id}/assignments/{assignment.id}/support"
    response = client.post(path, data={"csrf_token": token, "idempotency_key": access}, files={
        "file": ("help.txt", b"Private help for one student only"),
    })
    assert response.status_code == 200
    if access == "unlinked":
        assert "student hasn’t connected Telegram" in response.text
    elif access == "relocated":
        student.telegram_chat_id = "700"
    else:
        db.delete(db.scalar(select(ClassMembership).where(
            ClassMembership.classroom_id == classroom.id,
            ClassMembership.user_id == student.id,
        )))
    db.commit()

    def fail(*args):
        pytest.fail("The private help reply was sent to an unauthorized chat")

    monkeypatch.setattr(telegram, "send_message", fail)
    monkeypatch.setattr(telegram, "send_document", fail)
    assert DeliveryProcessor(telegram, app.state.session_factory).run() == []
    deliveries = db.scalars(select(NotificationDelivery).where(NotificationDelivery.kind == "student_support")).all()
    for delivery in deliveries:
        db.refresh(delivery)
    assert [delivery.status for delivery in deliveries] == ["skipped", "skipped"]
