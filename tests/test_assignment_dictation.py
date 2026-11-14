from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx2 as httpx
import pytest
from openai import OpenAI
from sqlalchemy import func, select

from app.llm.speech import OpenAISpeechProvider, TranscriptionError
from app.models import Assignment, AssignmentTarget, NotificationDelivery, StudentAssignmentState
from tests.conftest import authenticate

WEBM = b"\x1a\x45\xdf\xa3"
SPOKEN_ASSIGNMENT = (
    "Grade 8A, complete questions one through ten from chapter four by tomorrow evening. "
    "Draw a labeled diagram of photosynthesis."
)


def test_dictated_example_preserves_original_and_publishes_to_selected_student(client, app, db, data):
    data["class1"].name = "Grade 8A Science"
    db.commit()
    token = authenticate(client, app, data["teacher1"].id)
    original = SPOKEN_ASSIGNMENT.replace("photosynthesis", "photosyn thesis")
    target = data["student1"].id
    form = {
        "csrf_token": token,
        "classroom_id": data["class1"].id,
        "idempotency_key": "spoken-example",
        "source_transcript": original,
        "natural_text": SPOKEN_ASSIGNMENT,
        "student_ids": target,
        "targeting_mode": "selected",
    }
    for _ in range(2):
        preview = client.post("/teacher/assignments", data=form)
        assert preview.status_code == 200
        assert original in preview.text and "Grade 8A Science" in preview.text
        assert data["student1"].name in preview.text
        assert "6:00 PM IST" in preview.text
    draft = db.scalars(select(Assignment).where(Assignment.original_request == original)).one()
    assert draft.status == "draft"
    instructions = "Complete questions 1–10 from Chapter 4. Draw a labeled diagram of photosynthesis."
    assert draft.instructions == instructions
    assert draft.due_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Asia/Kolkata")).date() == (
        datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
    )
    for _ in range(2):
        assert client.post(
            f"/teacher/assignments/{draft.id}/publish", data={"csrf_token": token}
        ).status_code == 200
    deliveries = db.scalars(select(NotificationDelivery).where(
        NotificationDelivery.assignment_id == draft.id, NotificationDelivery.kind == "assignment_created"
    )).all()
    assert len(deliveries) == 1
    authenticate(client, app, target)
    view = client.get(f"/student/assignments/{draft.id}")
    assert instructions in view.text and "6:00 PM IST" in view.text


def test_cleanup_review_and_publish_only_approved_draft(client, app, db, data):
    data["class1"].name = "Grade 8A Science"
    db.commit()
    token = authenticate(client, app, data["teacher1"].id)
    source = "gve Grade 8 A qstns 1 to 10 frm chptr 4 due tmrw at 6 pm"
    result = client.post(
        "/teacher/assignments",
        data={
            "csrf_token": token,
            "classroom_id": data["class1"].id,
            "idempotency_key": "typo-heavy-draft",
            "natural_text": source,
        },
    )
    assert result.status_code == 200
    assert "Complete questions 1–10 from Chapter 4." in result.text
    assert source in result.text and "Publish assignment" in result.text
    assignment = db.scalar(select(Assignment).where(Assignment.original_request == source))
    assert assignment.status == "draft"
    zone = ZoneInfo("Asia/Kolkata")
    tomorrow = datetime.now(zone).date() + timedelta(days=1)
    assert assignment.due_at.replace(tzinfo=UTC).astimezone(zone).date() == tomorrow
    assert assignment.due_at.replace(tzinfo=UTC).astimezone(zone).hour == 18
    assert (
        db.scalar(
            select(NotificationDelivery).where(NotificationDelivery.assignment_id == assignment.id)
        )
        is None
    )

    student = data["student1"]
    title = "Chapter 4 review"
    instructions = "Complete questions 1–10 from Chapter 4. Show your work."
    edit = {
        "csrf_token": token,
        "title": title,
        "instructions": instructions,
        "due_at": f"{tomorrow}T17:30",
        "student_ids": student.id,
    }
    draft_path = f"/teacher/assignments/{assignment.id}/draft"
    rejected = client.post(draft_path, data={**edit, "due_at": "wrong"})
    assert rejected.status_code == 422 and instructions in rejected.text
    assert client.post(draft_path, data=edit).status_code == 200
    for _ in range(2):
        assert (
            client.post(
                f"/teacher/assignments/{assignment.id}/publish", data={"csrf_token": token}
            ).status_code
            == 200
        )
    assert (
        db.scalar(
            select(func.count())
            .select_from(StudentAssignmentState)
            .where(StudentAssignmentState.assignment_id == assignment.id)
        )
        == 1
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(NotificationDelivery)
            .where(
                NotificationDelivery.assignment_id == assignment.id,
                NotificationDelivery.kind == "assignment_created",
            )
        )
        == 1
    )
    authenticate(client, app, student.id)
    view = client.get(f"/student/assignments/{assignment.id}")
    assert view.status_code == 200 and instructions in view.text and title in view.text
    assert "5:30 PM" in view.text
    authenticate(client, app, data["student2"].id)
    assert client.get(f"/student/assignments/{assignment.id}").status_code == 403


@pytest.mark.parametrize(
    ("source", "question"),
    [
        ("Give Grade 8 B questions 1–10 due tomorrow at 6 PM", "Did you mean Grade 8A Science?"),
        ("Complete questions 1 to 1O by tomorrow at 6 PM", "What exact question"),
        ("Complete question 14 or 40 by tomorrow at 6 PM", "What exact question"),
        ("Complete question fourteen or forty by tomorrow at 6 PM", "What exact question"),
        ("For Studnt One: Read pages 10–12 by tomorrow evening", "Which student did you mean"),
        ("Read pages 10–12 by tomorrow at 6", "Should this be due at 6 AM or 6 PM?"),
        ("Read pages one through ten by tomorrow at six", "Should this be due at 6 AM or 6 PM?"),
        (SPOKEN_ASSIGNMENT.replace("8A", "8B"), "Did you mean Grade 8A Science?"),
        ("For Student One or Student Two: Read pages one through ten by tomorrow evening", "Which student did you mean"),
        ("Read pages 10–12", "When is this due?"),
    ],
)
def test_clarification_keeps_input_and_creates_no_draft(client, app, db, data, source, question):
    data["class1"].name = "Grade 8A Science"
    db.commit()
    token = authenticate(client, app, data["teacher1"].id)
    result = client.post(
        "/teacher/assignments",
        data={
            "csrf_token": token,
            "classroom_id": data["class1"].id,
            "idempotency_key": "ambiguous-text",
            "natural_text": source,
            "source_transcript": "Raw microphone transcript",
            "student_ids": data["student1"].id,
            "targeting_mode": "selected",
        },
    )
    assert result.status_code == 422
    assert question in result.text and source in result.text
    assert "Raw microphone transcript" in result.text
    assert f'value="{data["student1"].id}" checked' in result.text
    assert db.scalar(select(Assignment).where(Assignment.original_request == source)) is None


@pytest.mark.parametrize("field", ["instructions", "due_at"])
def test_server_checks_protected_numbers_and_known_deadline(
    client, app, db, data, monkeypatch, field
):
    token = authenticate(client, app, data["teacher1"].id)
    source = "Complete question 14 by tomorrow at 6 PM."
    provider = app.state.llm_service.provider
    interpret = provider.interpret_assignment

    def inaccurate_draft(*args):
        draft = interpret(*args)
        change = (
            "Complete question 40."
            if field == "instructions"
            else draft.due_at + timedelta(hours=1)
        )
        return draft.model_copy(update={field: change})

    monkeypatch.setattr(provider, "interpret_assignment", inaccurate_draft)
    result = client.post(
        "/teacher/assignments",
        data={
            "csrf_token": token,
            "classroom_id": data["class1"].id,
            "idempotency_key": "inaccurate-draft",
            "natural_text": source,
        },
    )
    assert result.status_code == 422 and source in result.text
    assert db.scalar(select(Assignment).where(Assignment.original_request == source)) is None


def test_named_recipient_resolves_to_one_enrolled_student(client, app, db, data):
    token = authenticate(client, app, data["teacher1"].id)
    source = "For Student One: Read pages 10 to 12 by tomorrow evening"
    result = client.post(
        "/teacher/assignments",
        data={
            "csrf_token": token,
            "classroom_id": data["class1"].id,
            "idempotency_key": "named-recipient-draft",
            "natural_text": source,
        },
    )
    assert result.status_code == 200 and "Read pages 10–12." in result.text
    assignment = db.scalar(select(Assignment).where(Assignment.original_request == source))
    assert list(
        db.scalars(
            select(AssignmentTarget.student_id).where(
                AssignmentTarget.assignment_id == assignment.id
            )
        )
    ) == [data["student1"].id]


def test_recording_requires_authorized_class_csrf_and_enabled_provider(client, app, data):
    token = authenticate(client, app, data["teacher1"].id)
    url = f"/teacher/classes/{data['class1'].id}/assignments/transcribe"
    forbidden = f"/teacher/classes/{data['class2'].id}/assignments/transcribe"
    headers = {"Content-Type": "audio/webm;codecs=opus", "X-CSRF-Token": token}
    assert client.post(url, content=b"audio", headers=headers).status_code == 503
    assert client.post(forbidden, content=b"audio", headers=headers).status_code == 403
    assert (
        client.post(url, content=b"audio", headers={"Content-Type": "audio/webm"}).status_code
        == 403
    )

    class SpeechStub:
        def transcribe(self, audio, filename, content_type):
            assert (filename, content_type) == ("assignment.webm", "audio/webm")
            if audio == WEBM + b"failure":
                raise TranscriptionError("private provider details")
            return "Complete the energy worksheet by tomorrow afternoon."

    app.state.speech_provider = SpeechStub()
    result = client.post(url, content=WEBM + b"audio", headers=headers)
    assert result.json() == {"text": "Complete the energy worksheet by tomorrow afternoon."}
    error = client.post(url, content=WEBM + b"failure", headers=headers)
    assert error.status_code == 502 and "private provider details" not in error.text
    assert client.post(url, content=b"", headers=headers).status_code == 422
    assert client.post(url, content=b"not a WebM file", headers=headers).status_code == 415
    assert (
        client.post(
            url, content=b"audio", headers={**headers, "Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    assert (
        client.post(url, content=b"x" * (5 * 1024 * 1024 + 1), headers=headers).status_code == 413
    )
    token = authenticate(client, app, data["student1"].id)
    assert (
        client.post(url, content=b"audio", headers={**headers, "X-CSRF-Token": token}).status_code
        == 403
    )


def test_openai_speech_uses_audio_transcription_contract(monkeypatch):
    def transport(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        assert b"gpt-transcribe" in request.content and b"sample audio" in request.content
        assert b'filename="assignment.webm"' in request.content
        return httpx.Response(200, json={"text": " Read chapter 4 tomorrow. "})

    sdk = OpenAI(
        api_key="test-only",
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    monkeypatch.setattr("app.llm.speech.OpenAI", lambda **_kwargs: sdk)
    provider = OpenAISpeechProvider("test-only", "gpt-transcribe")
    assert provider.transcribe(b"sample audio", "assignment.webm", "audio/webm") == (
        "Read chapter 4 tomorrow."
    )
