import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import (
    ActivityEvent,
    Assignment,
    ClassMembership,
    Classroom,
    Feedback,
    NotificationDelivery,
    Reminder,
    School,
    SchoolMembership,
    Submission,
    User,
)
from app.services.submission import FeedbackService, SubmissionService
from tests.conftest import authenticate


def test_multi_school_management_and_cross_role_scopes(client, app, db, data):
    first, second = data["school1"], data["school2"]
    mira, nell = data["student2"], data["teacher2"]
    third = School(name="Third School", timezone="Asia/Singapore")
    db.add(third)
    db.flush()
    db.add_all(
        [
            SchoolMembership(school_id=first.id, user_id=mira.id, role="coordinator"),
            SchoolMembership(school_id=second.id, user_id=mira.id, role="coordinator"),
            SchoolMembership(school_id=third.id, user_id=nell.id, role="coordinator"),
        ]
    )
    db.commit()
    csrf = authenticate(client, app, mira.id)
    first_page = client.get(
        f"/coordinator?school_id={first.id}&q=Teacher%20One&class_filter={data['class1'].id}"
    )
    assert first_page.status_code == 200 and 'aria-label="Switch schools"' in first_page.text
    assert 'aria-label="School totals"' in first_page.text
    assert "Teacher One" in first_page.text and "Teacher Two" not in first_page.text
    second_page = client.get(f"/coordinator?school_id={second.id}")
    assert "Teacher Two" in second_page.text and "Teacher One" not in second_page.text
    assert 'id="teacher-query" name="q" type="search" value=""' in second_page.text
    assert third.name not in second_page.text
    assert client.get(f"/coordinator?school_id={third.id}").status_code == 403
    assert client.get(f"/coordinator/schools/{third.id}/students").status_code == 403
    assert client.get(f"/coordinator/classes/{data['class2'].id}").status_code == 200
    assert (
        client.get(f"/coordinator/schools/{second.id}/teachers/{data['teacher1'].id}").status_code
        == 403
    )

    blocked_edit = client.post(
        f"/coordinator/schools/{third.id}/edit",
        data={
            "csrf_token": csrf,
            "name": "Wrong school",
            "timezone": "Asia/Kolkata",
        },
    )
    assert blocked_edit.status_code == 403
    edited = client.post(
        f"/coordinator/schools/{first.id}/edit",
        data={
            "csrf_token": csrf,
            "name": "Updated School",
            "timezone": "Asia/Manila",
        },
    )
    assert "School details saved" in edited.text and "School details updated" in edited.text
    assert first.timezone == "Asia/Manila" and data["assignment"].timezone == "Asia/Kolkata"
    assert second.name == "School Two"
    assert (
        client.post(
            "/coordinator/staffing/preview",
            data={
                "csrf_token": csrf,
                "school_id": first.id,
                "class_id": data["class2"].id,
                "selected_ids": [data["teacher1"].id],
            },
        ).status_code
        == 403
    )
    student = data["student1"]
    assert (
        client.post(
            f"/coordinator/schools/{first.id}/students/{student.id}/enroll",
            data={
                "csrf_token": csrf,
                "class_id": data["class2"].id,
            },
        ).status_code
        == 403
    )
    assert student.name in client.get(f"/coordinator/schools/{first.id}/students").text
    assert student.name not in client.get(f"/coordinator/schools/{second.id}/students").text

    signup = {
        "csrf_token": csrf,
        "name": "New School Two Student",
        "email": "new-school-two@example.test",
        "temporary_password": "temporary-demo-password",
    }
    assert client.post(f"/coordinator/schools/{third.id}/students", data=signup).status_code == 403
    created = client.post(f"/coordinator/schools/{second.id}/students", data=signup)
    assert created.status_code == 200 and "Choose a class to enroll them" in created.text
    new_student = db.scalar(select(User).where(User.email == signup["email"]))
    assert (
        new_student.name
        in client.get(f"/coordinator/schools/{second.id}/students?class_filter=unassigned").text
    )
    assert new_student.name not in client.get(f"/coordinator/schools/{first.id}/students").text
    assert (
        client.post(
            f"/coordinator/schools/{second.id}/students/{new_student.id}/enroll",
            data={
                "csrf_token": csrf,
                "class_id": data["class1"].id,
            },
        ).status_code
        == 403
    )
    assert (
        "is enrolled in"
        in client.post(
            f"/coordinator/schools/{second.id}/students/{new_student.id}/enroll",
            data={
                "csrf_token": csrf,
                "class_id": data["class2"].id,
            },
        ).text
    )
    class_path = f"/coordinator/classes/{data['class2'].id}/students/{new_student.id}/membership"
    confirmation = client.post(
        class_path + "/preview", data={"csrf_token": csrf, "destination_id": "remove"}
    )
    assert "School Two" in confirmation.text and data["class2"].name in confirmation.text
    token = re.search(r'name="change_token" value="([^"]+)"', confirmation.text)[1]
    wrong_path = f"/coordinator/classes/{data['class1'].id}/students/{new_student.id}/membership"
    assert (
        client.post(wrong_path, data={"csrf_token": csrf, "change_token": token}).status_code == 403
    )
    membership = db.scalar(select(ClassMembership).where(ClassMembership.user_id == new_student.id))
    membership.created_at += timedelta(seconds=1)
    db.commit()
    stale = client.post(
        class_path,
        data={"csrf_token": csrf, "change_token": token},
        headers={"Accept": "text/html"},
    )
    assert (
        stale.status_code == 422
        and "enrollment changed" in stale.text
        and "School Two" in stale.text
    )

    authenticate(client, app, nell.id)
    assert client.get(f"/coordinator?school_id={third.id}").status_code == 200
    assert client.get(f"/coordinator/classes/{data['class2'].id}").status_code == 403
    assert client.get(f"/teacher?class_id={data['class2'].id}").status_code == 200

    shared = data["teacher1"]
    db.add_all(
        [
            SchoolMembership(school_id=second.id, user_id=shared.id, role="teacher"),
            ClassMembership(classroom_id=data["class2"].id, user_id=shared.id, role="teacher"),
        ]
    )
    db.commit()
    csrf = authenticate(client, app, mira.id)
    _, revoke = preview(client, csrf, first, teacher_id=shared.id, revoke="true")
    assert (
        "Teaching access removed"
        in client.post(
            "/coordinator/staffing/confirm",
            data={
                "csrf_token": csrf,
                "plan_token": revoke,
            },
        ).text
    )
    authenticate(client, app, shared.id)
    assert client.get(f"/teacher?class_id={data['class1'].id}").status_code == 403
    assert client.get(f"/teacher?class_id={data['class2'].id}").status_code == 200


def setup_staff(db, data):
    outgoing, school, first = data["teacher1"], data["school1"], data["class1"]
    coordinator = User(
        name="Coordinator", email="coord@example.test", password_hash=outgoing.password_hash
    )
    incoming = User(
        name="Incoming teacher", email="incoming@example.test", password_hash=outgoing.password_hash
    )
    second = Classroom(school_id=school.id, name="Another Class", grade="Grade 8")
    db.add_all([coordinator, incoming, second])
    db.flush()
    db.add_all(
        [
            SchoolMembership(school_id=school.id, user_id=coordinator.id, role="coordinator"),
            SchoolMembership(school_id=school.id, user_id=coordinator.id, role="teacher"),
            SchoolMembership(school_id=school.id, user_id=incoming.id, role="teacher"),
            ClassMembership(classroom_id=first.id, user_id=coordinator.id, role="teacher"),
            ClassMembership(classroom_id=second.id, user_id=outgoing.id, role="teacher"),
            ClassMembership(classroom_id=second.id, user_id=data["student1"].id, role="student"),
        ]
    )
    db.commit()
    return coordinator, incoming, second


def preview(client, csrf, school, **selection):
    response = client.post(
        "/coordinator/staffing/preview",
        data={"csrf_token": csrf, "school_id": school.id, **selection},
    )
    assert response.status_code == 200, response.text
    token = re.search(r'name="plan_token" value="([^"]+)"', response.text)
    assert token
    return response.text, token[1]


def test_teacher_replacement_preserves_existing_review_and_rechecks_permissions(
    client, app, db, data, tmp_path, monkeypatch
):
    coordinator, incoming, second = setup_staff(db, data)
    outgoing, school, first = data["teacher1"], data["school1"], data["class1"]
    student, assignment = data["student1"], data["assignment"]
    previous = SubmissionService().submit(
        db, student, assignment.id, "previous-review", text_content="First version"
    )
    FeedbackService().create(db, outgoing, previous.id, "Please add an example.")
    file = tmp_path / "revision.txt"
    file.write_text("Second version", encoding="utf-8")
    monkeypatch.setattr(app.state.settings, "upload_dir", tmp_path)
    submission = SubmissionService().submit(
        db,
        student,
        assignment.id,
        "pending-review",
        stored_file_path=str(file),
        original_filename=file.name,
    )
    queued = NotificationDelivery(
        user_id=outgoing.id,
        chat_id=outgoing.telegram_chat_id,
        kind="risk_summary",
        body="Pending teaching summary",
        status="pending",
    )
    db.add(queued)
    db.commit()

    csrf = authenticate(client, app, coordinator.id)
    response = client.get(f"/coordinator?school_id={school.id}&q=teacher%20one")
    assert response.status_code == 200 and "Co-taught" in response.text
    (directory_row,) = response.context["directory"]
    assert directory_row["teacher"].id == outgoing.id
    assert directory_row["student_count"] == 2
    assert directory_row["review_count"] == 1
    unassigned = client.get(f"/coordinator?school_id={school.id}&class_filter=unassigned")
    assert incoming.email in unassigned.text
    assert outgoing.email not in unassigned.text
    assert client.get(f"/coordinator/schools/{school.id}/teachers/{outgoing.id}").status_code == 200
    assert client.get(f"/coordinator/classes/{data['class2'].id}/work").status_code == 403

    confirmation, token = preview(
        client, csrf, school, class_id=first.id, selected_ids=[coordinator.id, incoming.id]
    )
    assert "Current teaching team" in confirmation and "Proposed teaching team" in confirmation
    assert "2 enrolled · 1 awaiting review" in confirmation
    assert (
        client.post(
            "/coordinator/staffing/confirm", data={"csrf_token": "wrong", "plan_token": token}
        ).status_code
        == 403
    )
    response = client.post(
        "/coordinator/staffing/confirm",
        data={"csrf_token": csrf, "plan_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    event = db.scalar(
        select(ActivityEvent).where(ActivityEvent.event_type == "teaching_team_changed")
    )
    assert event.actor_user_id == coordinator.id and event.classroom_id == first.id
    assert event.created_at and str(outgoing.id) in event.metadata_json["removed_memberships"]
    assert str(incoming.id) in event.metadata_json["added_memberships"]
    db.refresh(queued)
    assert queued.status == "skipped" and queued.error == "Teaching access changed"
    assert db.get(Assignment, assignment.id).created_by_user_id == outgoing.id
    coordinator_view = client.get(f"/coordinator/classes/{first.id}/assignments/{assignment.id}")
    assert (
        "Please add an example." in coordinator_view.text and outgoing.name in coordinator_view.text
    )
    assert client.get(f"/coordinator/submissions/{submission.id}/file").text == "Second version"

    authenticate(client, app, outgoing.id)
    assert client.get(f"/teacher/classes/{first.id}/assignments/{assignment.id}").status_code == 403
    assert client.get(f"/teacher/submissions/{submission.id}/file").status_code == 403
    assert client.get(f"/teacher?class_id={second.id}").status_code == 200
    assert (
        app.state.telegram_service.process(
            db,
            {
                "update_id": 80601,
                "message": {
                    "from": {"id": 101},
                    "chat": {"id": 101, "type": "private"},
                    "text": f"/assign {first.id} Write a page by tomorrow evening",
                },
            },
        )["result"]
        == "error:AuthorizationError"
    )

    csrf = authenticate(client, app, incoming.id)
    assert client.get(f"/teacher/submissions/{submission.id}/file").text == "Second version"
    response = client.post(
        f"/teacher/submissions/{submission.id}/feedback",
        data={
            "csrf_token": csrf,
            "message": "The revised work is complete.",
            "outcome": "complete",
            "idempotency_key": "handover-feedback",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (
        db.scalar(
            select(Feedback.teacher_id).where(Feedback.idempotency_key == "handover-feedback")
        )
        == incoming.id
    )
    assert db.get(Submission, previous.id).text_content == "First version"


def test_staffing_preview_rejects_changes_and_school_removal_preserves_other_roles(
    client, app, db, data
):
    coordinator, incoming, second = setup_staff(db, data)
    first, school = data["class1"], data["school1"]
    csrf = authenticate(client, app, coordinator.id)
    _, token = preview(client, csrf, school, class_id=first.id, selected_ids=[coordinator.id])
    db.add(ClassMembership(classroom_id=first.id, user_id=incoming.id, role="teacher"))
    db.commit()
    stale = client.post(
        "/coordinator/staffing/confirm", data={"csrf_token": csrf, "plan_token": token}
    )
    assert stale.status_code == 409
    assert "workload changed" in stale.text
    assert "Return to staffing" in stale.text
    assert "Incoming teacher" in client.get(f"/coordinator/classes/{first.id}").text

    _, token = preview(client, csrf, school, teacher_id=coordinator.id, selected_ids=[second.id])
    assert (
        client.post(
            "/coordinator/staffing/confirm",
            data={"csrf_token": csrf, "plan_token": token},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert client.get(f"/teacher?class_id={first.id}").status_code == 403
    assert client.get(f"/teacher?class_id={second.id}").status_code == 200
    _, token = preview(client, csrf, school, teacher_id=coordinator.id, revoke="true")
    response = client.post(
        "/coordinator/staffing/confirm", data={"csrf_token": csrf, "plan_token": token}
    )
    assert "Teaching access removed" in response.text
    assert client.get("/teacher").status_code == 403
    assert client.get(f"/coordinator/classes/{first.id}").status_code == 200


def test_coordinator_class_changes_and_assignment_actions_are_school_scoped(client, app, db, data):
    coordinator, _, second = setup_staff(db, data)
    school, first, foreign = data["school1"], data["class1"], data["class2"]
    student = data["student1"]
    assignment = data["assignment"]
    csrf = authenticate(client, app, coordinator.id)
    changed = client.post(
        f"/coordinator/classes/{first.id}/edit",
        data={
            "csrf_token": csrf,
            "name": "Updated class",
            "grade": "Grade 8",
        },
    )
    assert "Updated class" in changed.text
    assert (
        client.post(
            f"/coordinator/classes/{first.id}/students",
            data={
                "csrf_token": csrf,
                "name": "Foreign teacher",
                "email": data["teacher2"].email,
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/coordinator/classes/{foreign.id}/archive",
            data={"csrf_token": csrf, "confirmed": "true"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/coordinator/classes/{first.id}/students/{student.id}/membership/preview",
            data={
                "csrf_token": csrf,
                "destination_id": foreign.id,
            },
        ).status_code
        == 403
    )
    path = f"/coordinator/classes/{first.id}/students/{student.id}/membership"
    preview_response = client.post(
        f"{path}/preview",
        data={
            "csrf_token": csrf,
            "destination_id": second.id,
        },
    )
    assert (
        "Confirm student transfer" in preview_response.text and second.name in preview_response.text
    )
    token = re.search(r'name="change_token" value="([^"]+)"', preview_response.text)[1]
    moved = client.post(path, data={"csrf_token": csrf, "change_token": token})
    assert "Student transferred" in moved.text
    assert (
        db.scalar(
            select(ClassMembership.id).where(
                ClassMembership.classroom_id == first.id, ClassMembership.user_id == student.id
            )
        )
        is None
    )
    assert db.get(Assignment, assignment.id) is not None
    moved_deadline = client.post(
        f"/coordinator/assignments/{assignment.id}/deadline",
        data={
            "csrf_token": csrf,
            "idempotency_key": "after-transfer",
            "due_at": (datetime.now(UTC) + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert "Deadline updated" in moved_deadline.text
    assert (
        db.scalar(
            select(Reminder.id).where(
                Reminder.assignment_id == assignment.id,
                Reminder.student_id == student.id,
                Reminder.schedule_version == assignment.schedule_version,
            )
        )
        is None
    )
    draft = client.post(
        "/coordinator/assignments",
        data={
            "csrf_token": csrf,
            "classroom_id": second.id,
            "entry": "manual",
            "draft": "true",
            "title": "Coordinator assignment",
            "instructions": "Write a short report.",
            "due_at": (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"),
            "idempotency_key": "coordinator-draft",
        },
    )
    assert draft.status_code == 200 and "Publish assignment" in draft.text
    item = db.scalar(select(Assignment).where(Assignment.title == "Coordinator assignment"))
    published = client.post(
        f"/coordinator/assignments/{item.id}/publish", data={"csrf_token": csrf}
    )
    assert "Assignment published" in published.text
    assert item.created_by_user_id == coordinator.id and item.school_id == school.id
    pending = SubmissionService().submit(
        db, student, item.id, "unstaffed-review", text_content="My report"
    )
    db.commit()
    _, token = preview(client, csrf, school, class_id=second.id)
    cleared = client.post(
        "/coordinator/staffing/confirm", data={"csrf_token": csrf, "plan_token": token}
    )
    assert "No assigned teacher" in cleared.text
    review = client.post(
        f"/coordinator/submissions/{pending.id}/feedback",
        data={
            "csrf_token": csrf,
            "message": "Well done.",
            "outcome": "complete",
            "idempotency_key": "coordinator-review",
        },
    )
    assert review.status_code == 200 and "Marked complete" in review.text
    assert (
        db.scalar(
            select(Feedback.teacher_id).where(Feedback.idempotency_key == "coordinator-review")
        )
        == coordinator.id
    )
    archived = client.post(
        f"/coordinator/classes/{first.id}/archive", data={"csrf_token": csrf, "confirmed": "true"}
    )
    assert "Class archived" in archived.text
    history = client.get(f"/coordinator/classes/{first.id}/work")
    assert assignment.title in history.text and "read-only" in history.text
    assert (
        client.post(
            f"/coordinator/assignments/{assignment.id}/cancel",
            data={"csrf_token": csrf, "idempotency_key": "archive-test"},
        ).status_code
        == 422
    )
    authenticate(client, app, data["teacher1"].id)
    assert client.get(f"/teacher?class_id={first.id}").status_code == 403
