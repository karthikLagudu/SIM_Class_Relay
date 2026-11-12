from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import (
    AssignmentTarget,
    ClassMembership,
    Classroom,
    NotificationDelivery,
    SchoolMembership,
)
from app.services.assignment import AssignmentService
from app.services.submission import SubmissionService
from tests.conftest import authenticate


def test_dual_role_grants_school_management_and_assigned_teaching_separately(client, app, db, data):
    teacher = data["teacher1"]
    school, assigned, foreign = data["school1"], data["class1"], data["class2"]
    other = Classroom(school_id=school.id, name="Another teacher's class", grade="8")
    db.add_all(
        [other, SchoolMembership(school_id=school.id, user_id=teacher.id, role="coordinator")]
    )
    db.commit()
    token = authenticate(client, app, teacher.id)

    school_home = client.get("/")
    assert school_home.url.path == "/coordinator"
    assert 'aria-label="Current role: Coordinator"' in school_home.text
    assert 'aria-label="Switch workspace"' in school_home.text
    assert f"/coordinator/classes/{other.id}" in school_home.text
    people = client.get(f"/coordinator/classes/{assigned.id}")
    assert "Assigned teachers" in people.text and teacher.name in people.text
    assert data["student1"].name in people.text
    assert "New assignment" not in people.text
    assert client.get(f"/coordinator/classes/{foreign.id}").status_code == 403
    assert client.get(f"/coordinator?school_id={foreign.school_id}").status_code == 403

    teaching = client.get("/teacher")
    assert 'aria-label="Current role: Teacher"' in teaching.text
    assert f"/teacher?class_id={assigned.id}" in teaching.text
    assert other.name not in teaching.text
    assert client.get(f"/teacher/classes/{other.id}").status_code == 403
    assert client.get(f"/teacher/classes/{other.id}/assignments/new").status_code == 403
    assert (
        client.post(f"/teacher/classes/{other.id}/invites", data={"csrf_token": token}).status_code
        == 403
    )
    assert (
        client.post(
            f"/coordinator/classes/{other.id}/invites", data={"csrf_token": "wrong"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/coordinator/classes/{other.id}/invites", data={"csrf_token": token}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/coordinator/classes/{foreign.id}/invites", data={"csrf_token": token}
        ).status_code
        == 403
    )
    assert (
        app.state.telegram_service.process(
            db,
            {
                "update_id": 90901,
                "message": {
                    "from": {"id": 101},
                    "chat": {"id": 101, "type": "private"},
                    "text": f"/assign {other.id} Write a page by tomorrow evening",
                },
            },
        )["result"]
        == "error:AuthorizationError"
    )


def test_student_filter_and_telegram_status_use_current_targets(client, app, db, data):
    student, teacher = data["student1"], data["teacher1"]
    second = Classroom(school_id=data["school1"].id, name="Science Lab", grade="8")
    db.add(second)
    db.flush()
    db.add_all(
        [
            ClassMembership(classroom_id=second.id, user_id=student.id, role="student"),
            ClassMembership(classroom_id=second.id, user_id=teacher.id, role="teacher"),
        ]
    )
    db.flush()
    assignment = AssignmentService().create(
        db,
        teacher,
        second.id,
        "Lab report",
        "Write the report.",
        datetime.now(UTC) + timedelta(days=2),
        "Asia/Kolkata",
    )
    db.commit()
    authenticate(client, app, student.id)
    assert "Lab report" in client.get("/student?class_id=").text
    page = client.get(f"/student?class_id={second.id}")
    assert "Lab report" in page.text and "Essay" not in page.text
    assert data["student2"].name not in page.text and "Switch workspace" not in page.text
    assert client.get(f"/student?class_id={data['class2'].id}").status_code == 403
    assert (
        client.get(f"/student/classes/{data['class1'].id}/assignments/{assignment.id}").status_code
        == 404
    )

    target = db.scalar(
        select(AssignmentTarget).where(
            AssignmentTarget.assignment_id == assignment.id,
            AssignmentTarget.student_id == student.id,
        )
    )
    db.delete(target)
    db.commit()
    assert "Lab report" not in client.get("/student").text
    assert client.get(f"/student/assignments/{assignment.id}").status_code == 403
    assert (
        app.state.telegram_service.process(
            db,
            {
                "update_id": 90902,
                "message": {
                    "from": {"id": 201},
                    "chat": {"id": 201, "type": "private"},
                    "text": "/status",
                },
            },
        )["result"]
        == "status"
    )
    body = db.scalar(select(NotificationDelivery.body).where(NotificationDelivery.kind == "status"))
    assert "Essay" in body and "Lab report" not in body


def test_student_download_uses_ownership_even_with_teaching_role(
    client, app, db, data, tmp_path, monkeypatch
):
    monkeypatch.setattr(app.state.settings, "upload_dir", tmp_path)
    file = tmp_path / "peer.txt"
    file.write_text("Peer work", encoding="utf-8")
    submission = SubmissionService().submit(
        db,
        data["student2"],
        data["assignment"].id,
        "peer-file",
        stored_file_path=str(file),
        original_filename=file.name,
    )
    db.commit()
    student = data["student1"]
    authenticate(client, app, student.id)
    assert client.get(f"/api/student/submissions/{submission.id}").status_code == 403
    assert client.get(f"/submissions/{submission.id}/file").status_code == 403
    db.add(ClassMembership(classroom_id=data["class1"].id, user_id=student.id, role="teacher"))
    db.commit()
    assert client.get(f"/student/submissions/{submission.id}/file?download=true").status_code == 403
    assert client.get(f"/teacher/submissions/{submission.id}/file").text == "Peer work"
