import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    ActivityEvent,
    Assignment,
    AssignmentTarget,
    ClassMembership,
    Classroom,
    Feedback,
    NotificationDelivery,
    Reminder,
    SchoolMembership,
    StudentAssignmentState,
    Submission,
    TelegramLinkToken,
)
from tests.conftest import authenticate


@pytest.mark.parametrize("keep_other_school", [False, True])
def test_remove_student_from_school(
    client, app, db, data, tmp_path, monkeypatch, keep_other_school
):
    student = data["student1"]
    coordinator = data["teacher1"]
    other_coordinator = data["teacher2"]
    school, other_school = data["school1"], data["school2"]
    classroom, other_class = data["class1"], data["class2"]
    original = data["assignment"]
    now = datetime.now(UTC)
    extra_class = Classroom(school_id=school.id, name="Archived studio", grade="8", archived_at=now)
    db.add(extra_class)
    db.flush()
    db.add_all(
        [
            ClassMembership(classroom_id=extra_class.id, user_id=student.id, role="student"),
            SchoolMembership(school_id=school.id, user_id=coordinator.id, role="coordinator"),
            SchoolMembership(
                school_id=other_school.id, user_id=other_coordinator.id, role="coordinator"
            ),
        ]
    )
    if keep_other_school:
        db.add_all(
            [
                SchoolMembership(school_id=other_school.id, user_id=student.id, role="student"),
                ClassMembership(classroom_id=other_class.id, user_id=student.id, role="student"),
                SchoolMembership(school_id=school.id, user_id=student.id, role="teacher"),
                ClassMembership(classroom_id=classroom.id, user_id=student.id, role="teacher"),
            ]
        )
    other_work = Assignment(
        school_id=other_school.id,
        classroom_id=other_class.id,
        created_by_user_id=other_coordinator.id,
        title="Other school assignment",
        instructions="Read the introduction.",
        due_at=now + timedelta(days=3),
        timezone=other_school.timezone,
        status="assigned",
    )
    db.add(other_work)
    db.flush()
    if keep_other_school:
        db.add_all(
            [
                AssignmentTarget(assignment_id=other_work.id, student_id=student.id),
                StudentAssignmentState(
                    assignment_id=other_work.id, student_id=student.id, status="assigned"
                ),
            ]
        )

    file = tmp_path / "example.txt"
    file.write_text("Historical coursework", encoding="utf-8")
    monkeypatch.setattr(app.state.settings, "upload_dir", tmp_path)
    submission = Submission(
        assignment_id=original.id,
        student_id=student.id,
        submission_type="file",
        text_content="Historical coursework",
        stored_file_path=str(file),
        original_filename="example.txt",
        mime_type="text/plain",
        idempotency_key="removal-test-submission",
    )
    db.add(submission)
    db.flush()
    db.add(
        Feedback(
            assignment_id=original.id,
            student_id=student.id,
            teacher_id=coordinator.id,
            submission_id=submission.id,
            message="Historical feedback",
        )
    )
    rows = {}
    for label, school_work, issuer in (
        ("removed", original, coordinator),
        ("other", other_work, other_coordinator),
    ):
        token = TelegramLinkToken(
            user_id=student.id,
            created_by_user_id=issuer.id,
            classroom_id=school_work.classroom_id,
            token_hash=label,
            expires_at=now + timedelta(days=1),
        )
        reminder = Reminder(
            assignment_id=school_work.id,
            student_id=student.id,
            reminder_type="deadline_2h",
            scheduled_for=now + timedelta(hours=2),
            status="failed" if label == "removed" else "pending",
            reason="Scheduled",
            dedupe_key=f"removal-test:{label}",
        )
        delivery = NotificationDelivery(
            user_id=student.id,
            assignment_id=school_work.id,
            chat_id=student.telegram_chat_id,
            kind="reminder",
            body="Example queued message",
            status=reminder.status,
            next_attempt_at=now + timedelta(minutes=10),
            idempotency_key=f"reminder:{reminder.dedupe_key}",
        )
        rows[label] = (token, reminder, delivery)
        db.add_all(rows[label])
    db.commit()

    authenticate(client, app, student.id)
    old_session = client.cookies.get(app.state.sessions.cookie_name)
    student_file = f"/student/submissions/{submission.id}/file"
    assert client.get(student_file).content == file.read_bytes()
    route = f"/coordinator/schools/{school.id}/students/{student.id}/remove"
    other_csrf = authenticate(client, app, other_coordinator.id)
    assert client.get(route).status_code == 403
    assert (
        client.post(
            route, data={"csrf_token": other_csrf, "confirmed": "true", "removal_token": "bad"}
        ).status_code
        == 403
    )

    csrf = authenticate(client, app, coordinator.id)
    preview = client.get(route)
    assert preview.status_code == 200
    for term in (student.name, school.name, classroom.name, extra_class.name, "Cancel"):
        assert term in preview.text
    token = re.search(r'name="removal_token" value="([^"]+)"', preview.text)[1]
    form = {"csrf_token": csrf, "removal_token": token, "confirmed": "true"}
    assert client.post(route, data={**form, "csrf_token": "wrong"}).status_code == 403
    assert client.post(route, data={**form, "confirmed": "false"}).status_code == 422
    assert "was removed from this school" in client.post(route, data=form).text
    assert "already removed" in client.post(route, data=form).text
    audit = db.scalars(
        select(ActivityEvent).where(
            ActivityEvent.event_type == "student_removed_from_school",
        )
    ).one()
    assert audit.actor_user_id == coordinator.id and audit.created_at
    assert set(audit.metadata_json["classroom_ids"]) == {classroom.id, extra_class.id}
    assert "Historical feedback" in client.get(f"/coordinator/submissions/{submission.id}").text
    assert client.get(f"/coordinator/submissions/{submission.id}/file").content == file.read_bytes()
    for row in rows["removed"] + rows["other"]:
        db.refresh(row)
    revoked, cancelled, skipped = rows["removed"]
    assert revoked.revoked_at and cancelled.status == "cancelled" and skipped.status == "skipped"
    if keep_other_school:
        token_row, reminder, delivery = rows["other"]
        assert not token_row.revoked_at and reminder.status == delivery.status == "pending"

    client.cookies.set(app.state.sessions.cookie_name, old_session)
    status = 403 if keep_other_school else 401
    assert client.get(f"/student/assignments/{original.id}").status_code == status
    assert client.get(student_file).status_code == status
    db.refresh(student)
    if keep_other_school:
        assert client.get(f"/student/assignments/{other_work.id}").status_code == 200
        assert client.get(f"/teacher?class_id={classroom.id}").status_code == 200
        assert student.telegram_user_id == "201"
    else:
        assert student.telegram_chat_id is None and student.telegram_user_id is None
        assert (
            client.post(
                "/login", data={"email": student.email, "password": "test-password"}
            ).status_code
            == 403
        )

    result = app.state.telegram_service.process(
        db,
        {
            "update_id": 12001,
            "callback_query": {
                "id": "old-button",
                "from": {"id": 201},
                "data": f"assignment:{original.id}",
                "message": {"chat": {"id": 201, "type": "private"}},
            },
        },
    )
    assert result["result"] in {"error:AuthorizationError", "unlinked"}
    if keep_other_school:
        assert (
            app.state.telegram_service.process(
                db,
                {
                    "update_id": 12002,
                    "message": {
                        "from": {"id": 201},
                        "chat": {"id": 201, "type": "private"},
                        "text": f"/assignment {other_work.id}",
                    },
                },
            )["result"]
            == f"assignment_details:{other_work.id}"
        )
