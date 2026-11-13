import re

import pytest
from sqlalchemy import select

from app.models import (
    ClassMembership,
    NotificationDelivery,
    SchoolMembership,
    StudentSupportReply,
)
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
    delivery = db.scalars(
        select(NotificationDelivery).where(NotificationDelivery.kind == "student_support")
    ).one()
    assert delivery.user_id == student.id and delivery.status == "logged"
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
