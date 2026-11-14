from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.llm.fake import DemoLLM
from app.models import Assignment, AssignmentTarget, ClassMembership, NotificationDelivery
from tests.conftest import authenticate
from tests.test_telegram import tap


@pytest.mark.parametrize(
    ("source", "instructions", "deadline"),
    [
        (
            "compleet qns 1-10 frm chpter 4 by tmrw evning",
            "Complete questions 1–10 from Chapter 4.",
            "2026-11-15T18:00:00+05:30",
        ),
        (
            "submt pgs 12-15 and draw a labled diagram of photosyntehsis by fri at 5pm",
            "Submit pages 12–15 and draw a labeled diagram of photosynthesis.",
            "2026-11-20T17:00:00+05:30",
        ),
        (
            "Complete Q2(b), Q4 and Q7–Q9\nfrom ch 4 by tomorrow",
            "Complete Q2(b), Q4 and Q7–Q9 from Chapter 4.",
            "2026-11-15T18:00:00+05:30",
        ),
        (
            "For Grade 8A: Read Dr. Mehta’s notes on RuBisCO and bring graph paper by tmrw at 6pm",
            "Read Dr. Mehta’s notes on RuBisCO and bring graph paper.",
            "2026-11-15T18:00:00+05:30",
        ),
    ],
)
def test_offline_assignment_correction(source, instructions, deadline):
    now = datetime(2026, 11, 14, 10, tzinfo=UTC)
    parsed = DemoLLM().interpret_assignment(source, "Asia/Kolkata", now)
    assert parsed.instructions == instructions
    assert parsed.due_at.isoformat() == deadline


def test_relative_deadline_uses_school_calendar_near_midnight():
    now = datetime(2026, 11, 14, 20, tzinfo=UTC)
    source = "read ch 4 by tmrw evning"
    parser = DemoLLM()
    assert parser.interpret_assignment(source, "Asia/Kolkata", now).due_at.day == 16
    assert parser.interpret_assignment(source, "America/New_York", now).due_at.day == 15


@pytest.mark.parametrize(
    ("source", "question"),
    [
        ("finish qns 3 to by tomorrow", "What is the last number in the range starting at 3?"),
        ("For Grade 8B: Read Chapter 4 by tomorrow", "Your wording names"),
        ("For Student One: Read Chapter 4 by tomorrow", "Who should receive the assignment?"),
        ("For Studnt One: Read Chapter 4 by tomorrow", "Which student did you mean"),
    ],
)
def test_web_clarification_preserves_input_and_selected_recipients(
    client, app, db, data, source, question
):
    classroom = data["class1"]
    classroom.name = "Grade 8A Science"
    data["student2"].name = "Student Ones"
    db.commit()
    csrf = authenticate(client, app, data["teacher1"].id)
    chosen = data["student2"].id
    response = client.post(
        "/teacher/assignments",
        data={
            "csrf_token": csrf,
            "classroom_id": classroom.id,
            "natural_text": source,
            "targeting_mode": "selected",
            "student_ids": chosen,
            "idempotency_key": "clarification",
        },
    )
    assert response.status_code == 422
    assert question in response.text and source in response.text
    assert f'name="student_ids" value="{chosen}" checked' in response.text
    assert list(db.scalars(select(Assignment))) == [data["assignment"]]


def test_web_provider_error_offers_manual_edit_with_original_and_selection(
    client,
    app,
    db,
    data,
    monkeypatch,
):
    def unavailable(*args):
        raise TimeoutError

    monkeypatch.setattr(app.state.llm_service.provider, "interpret_assignment", unavailable)
    csrf = authenticate(client, app, data["teacher1"].id)
    source = "Complete Q2(b), Q4 and Q7–Q9 by tomorrow"
    payload = {
        "csrf_token": csrf,
        "classroom_id": data["class1"].id,
        "natural_text": source,
        "targeting_mode": "selected",
        "student_ids": data["student1"].id,
        "idempotency_key": "manual-fallback",
    }
    failed = client.post("/teacher/assignments", data=payload)
    assert failed.status_code == 422 and source in failed.text and "Edit manually" in failed.text
    manual = client.post("/teacher/assignments", data={**payload, "use_manual": "true"})
    assert manual.status_code == 200
    assert f"{source}</textarea>" in manual.text
    assert f'name="student_ids" value="{data["student1"].id}" checked' in manual.text


def test_telegram_preview_revision_cancellation_and_idempotent_publish(app, db, data):
    service = app.state.telegram_service
    teacher, classroom = data["teacher1"], data["class1"]

    def message(update_id, text):
        return service.process(
            db,
            {
                "update_id": update_id,
                "message": {
                    "from": {"id": 101},
                    "chat": {"id": 101, "type": "private"},
                    "text": text,
                },
            },
        )["result"]

    def button(update_id, action, nonce):
        return service.process(db, tap(update_id, 101, f"{action}_draft:{nonce}"))

    def current():
        return service.intent_router.context.get(db, teacher, "101").pending_payload_json

    assert (
        message(22001, f"/assign {classroom.id} compleet qns 1-10 frm chpter 4 by tmrw evning")
        == "assignment_preview"
    )
    first = current()["nonce"]
    preview = db.scalars(
        select(NotificationDelivery).where(NotificationDelivery.kind == "teacher_draft_preview")
    ).one()
    assert "Complete questions 1–10 from Chapter 4." in preview.body
    assert {button["text"] for button in preview.reply_markup["inline_keyboard"][0]} == {
        "Publish",
        "Cancel",
    }
    assert list(db.scalars(select(Assignment))) == [data["assignment"]]
    assert (
        message(22002, "For Student One: Complete Q2(b), Q4 and Q7–Q9 by tomorrow")
        == "assignment_preview"
    )
    assert button(22003, "publish", first)["result"] == "error:ValidationError"
    assert button(22004, "cancel", current()["nonce"])["result"] == "assignment_preview_cancelled"
    assert list(db.scalars(select(Assignment))) == [data["assignment"]]

    assert (
        message(
            22005,
            f"/assign {classroom.id} For Student One: Complete Q2(b), Q4 and Q7–Q9 by tomorrow",
        )
        == "assignment_preview"
    )
    nonce = current()["nonce"]
    result = button(22006, "publish", nonce)["result"]
    assignment_id = int(result.split(":")[1])
    assert button(22006, "publish", nonce)["duplicate"] is True
    assert button(22007, "publish", nonce)["result"] == "error:ValidationError"
    assignment = db.get(Assignment, assignment_id)
    assert (
        assignment.status == "assigned"
        and assignment.instructions == "Complete Q2(b), Q4 and Q7–Q9."
    )
    assert list(
        db.scalars(
            select(AssignmentTarget.student_id).where(
                AssignmentTarget.assignment_id == assignment_id,
            )
        )
    ) == [data["student1"].id]
    assert (
        len(
            list(
                db.scalars(
                    select(NotificationDelivery).where(
                        NotificationDelivery.assignment_id == assignment_id,
                        NotificationDelivery.kind == "assignment_created",
                    )
                )
            )
        )
        == 1
    )

    assert (
        message(22008, f"/assign {classroom.id} Complete qns 1–5 by tomorrow")
        == "assignment_preview"
    )
    nonce = current()["nonce"]
    db.delete(
        db.scalar(
            select(ClassMembership).where(
                ClassMembership.classroom_id == classroom.id,
                ClassMembership.user_id == data["student2"].id,
            )
        )
    )
    db.flush()
    assert button(22009, "publish", nonce)["result"] == "error:ValidationError"
    assert len(list(db.scalars(select(Assignment)))) == 2
