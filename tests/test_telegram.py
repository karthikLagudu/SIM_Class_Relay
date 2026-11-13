from sqlalchemy import func, select

from app.models import NotificationDelivery, ProcessedTelegramUpdate, ProgressEvent, Submission
from app.services.assignment import AssignmentService


def message(update_id, sender_id, body):
    return {
        "update_id": update_id,
        "message": {"from": {"id": sender_id}, "chat": {"id": sender_id}, "text": body},
    }


def tap(update_id, sender_id, callback):
    return {
        "update_id": update_id,
        "callback_query": {
            "from": {"id": sender_id},
            "message": {"chat": {"id": sender_id}},
            "data": callback,
        },
    }


def latest_delivery(db):
    return db.scalar(select(NotificationDelivery).order_by(NotificationDelivery.id.desc()).limit(1))


def test_same_telegram_update_runs_once(app, db, data):
    update = {
        "update_id": 44,
        "message": {
            "from": {"id": 201},
            "chat": {"id": 201},
            "text": f"/ack {data['assignment'].id}",
        },
    }
    first = app.state.telegram_service.process(db, update)
    db.commit()
    second = app.state.telegram_service.process(db, update)
    db.commit()
    assert first["ok"] is True and second["duplicate"] is True
    assert db.scalar(select(func.count()).select_from(ProcessedTelegramUpdate)) == 1
    assert db.scalar(select(func.count()).select_from(ProgressEvent)) == 1


def test_repeated_submission_update_creates_one_submission(app, db, data):
    update = {
        "update_id": 45,
        "message": {
            "from": {"id": 201},
            "chat": {"id": 201},
            "text": f"/submit {data['assignment'].id} my work",
        },
    }
    app.state.telegram_service.process(db, update)
    db.commit()
    app.state.telegram_service.process(db, update)
    db.commit()
    assert db.scalar(select(func.count()).select_from(Submission)) == 1


def test_student_can_open_demo_published_work_and_submit_in_chat(app, db, data, monkeypatch):
    service = app.state.telegram_service
    assignment = service.assignments.create(
        db, data["teacher1"], data["class1"].id, "Weather worksheet", "Measure rainfall.",
        data["assignment"].due_at, data["assignment"].timezone, "telegram-student-menu",
        [data["student1"].id],
    )
    simulated = latest_delivery(db)
    assert simulated.kind == "assignment_created" and simulated.status == "logged"

    monkeypatch.setattr(app.state.settings, "telegram_mode", "real")
    assert service.process(db, message(61, 201, "/start"))["result"] == "start_linked"
    menu = latest_delivery(db)
    assert "secure Telegram connection link" not in menu.body
    assert [button["text"] for row in menu.reply_markup["inline_keyboard"] for button in row] == [
        "My assignments", "Feedback", "My classes", "My reminders", "Join a class",
        "Student help", "Cancel pending action",
    ]
    assert service.process(db, message(62, 201, "/assign"))["result"] == "error:AuthorizationError"
    assert "Tap My assignments" in latest_delivery(db).body

    assert service.process(db, tap(63, 201, "assignments:1"))["result"] == "assignments"
    listed = latest_delivery(db)
    assert "Weather worksheet" in listed.body and "Class One" in listed.body and "IST" in listed.body
    callbacks = [row[0]["callback_data"] for row in listed.reply_markup["inline_keyboard"]]
    assert f"assignment:{assignment.id}" in callbacks
    assert service.process(db, tap(64, 201, f"assignment:{assignment.id}"))["result"] == (
        f"assignment_details:{assignment.id}"
    )
    details = latest_delivery(db)
    assert "Measure rainfall." in details.body and details.status == "pending"
    assert simulated.status == "logged"
    submit = details.reply_markup["inline_keyboard"][1][0]["callback_data"]
    assert service.process(db, tap(65, 201, submit))["result"] == (
        f"pending_submission:{assignment.id}"
    )
    assert service.process(db, message(66, 201, "We measured 14 mm."))["result"].startswith(
        "submission:"
    )
    submitted = db.scalar(select(Submission).where(Submission.assignment_id == assignment.id))
    assert submitted.text_content == "We measured 14 mm."
    assert submitted.student_id == data["student1"].id

    service.process(db, message(67, 201, f"/assignment {assignment.id}"))
    assert "Submitted" in latest_delivery(db).body
    assert latest_delivery(db).reply_markup is None
    service.process(db, tap(68, 201, submit))
    assert "closed to submissions" in latest_delivery(db).body


def test_assignment_list_and_buttons_preserve_recipient_boundaries(app, db, data):
    service = app.state.telegram_service
    draft = AssignmentService().create(
        db, data["teacher1"], data["class1"].id, "Hidden draft", "Draft instructions",
        data["assignment"].due_at, data["assignment"].timezone, "telegram-draft",
        [data["student2"].id], draft=True,
    )
    limited = service.assignments.create(
        db, data["teacher1"], data["class1"].id, "Student one only", "Private instructions",
        data["assignment"].due_at, data["assignment"].timezone, "telegram-targeted",
        [data["student1"].id],
    )
    service.process(db, message(71, 202, "/assignments"))
    body = latest_delivery(db).body
    assert "Essay" in body and "Hidden draft" not in body and "Student one only" not in body
    assert service.process(db, tap(72, 202, f"assignment:{limited.id}"))["result"] == (
        "error:AuthorizationError"
    )
    assert service.process(db, tap(73, 202, f"assignment:{draft.id}"))["result"] == (
        "error:NotFoundError"
    )


def test_teacher_menu_uses_their_class_and_a_working_command_example(app, db, data):
    service = app.state.telegram_service
    assert service.process(db, message(81, 101, "/start"))["result"] == "start_linked"
    keyboard = latest_delivery(db).reply_markup["inline_keyboard"]
    assert keyboard[0][0]["text"] == "Create assignment"
    assert service.process(db, tap(82, 101, keyboard[0][0]["callback_data"]))["result"] == (
        "assign_help"
    )
    body = latest_delivery(db).body
    assert "Class One" in body and "Class Two" not in body
    assert f"/assign {data['class1'].id} Read chapter 3" in body
