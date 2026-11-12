from base64 import b64decode

from sqlalchemy import select

from app.models import SchoolMembership, Submission, User
from tests.conftest import authenticate

PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB0YAAAAASUVORK5CYII="
)


def test_staff_queues_share_review_and_submission_history(
    client, app, db, data, tmp_path, monkeypatch
):
    monkeypatch.setattr(app.state.settings, "upload_dir", tmp_path)
    coordinator = User(name="Local Coordinator", email="local@example.test", password_hash="unused")
    outsider = User(name="Other Coordinator", email="other@example.test", password_hash="unused")
    db.add_all([coordinator, outsider])
    db.flush()
    school = data["school1"].id
    class_id = data["class1"].id
    student = data["student1"]
    assignment = data["assignment"]
    student_url = f"/student/classes/{class_id}/assignments/{assignment.id}"
    school_queue = f"/coordinator/submissions?school_id={school}"
    db.add_all(
        [
            SchoolMembership(school_id=school, user_id=coordinator.id, role="coordinator"),
            SchoolMembership(school_id=data["school2"].id, user_id=outsider.id, role="coordinator"),
        ]
    )
    db.commit()
    authenticate(client, app, data["teacher1"].id)
    assert "No submissions are awaiting review" in client.get("/teacher/submissions").text

    original = "  Measured 12 mm\n  Second reading: 16 mm.  "
    student_token = authenticate(client, app, student.id)
    receipt = client.post(
        f"/student/assignments/{assignment.id}/submit",
        data={
            "csrf_token": student_token,
            "idempotency_key": "web-first",
            "text_content": original,
        },
        files={"file": ("measurements.png", PNG, "image/png")},
    )
    assert receipt.status_code == 200 and "Work submitted" in receipt.text
    first = db.scalar(select(Submission).where(Submission.idempotency_key == "web-first"))
    original_instructions = assignment.instructions
    assignment.instructions = "Revised directions for future work"
    db.commit()
    teacher_url = f"/teacher/submissions/{first.id}"
    coordinator_url = f"/coordinator/submissions/{first.id}"

    for role, actor, queue, detail in (
        ("teacher", data["teacher1"], "/teacher/submissions", teacher_url),
        ("coordinator", coordinator, f"{school_queue}&status=awaiting", coordinator_url),
    ):
        authenticate(client, app, actor.id)
        listing = client.get(queue)
        assert listing.status_code == 200
        assert all(
            text in listing.text
            for text in (detail, student.name, "Essay", "Class One", "Submitted", "Awaiting review")
        )
        page = client.get(detail)
        assert page.status_code == 200
        assert f'<div class="preserve-lines work-content">{original}</div>' in page.text
        assert original_instructions in page.text and assignment.instructions not in page.text
        assert "Latest attempt" in page.text and "measurements.png" in page.text
        assert f'src="/{role}/submissions/{first.id}/file"' in page.text
        assert client.get(f"{detail}/file").content == PNG
        download = client.get(f"{detail}/file?download=true")
        assert (
            download.content == PNG
            and 'attachment; filename="measurements.png"' in download.headers["content-disposition"]
        )

    coordinator_token = authenticate(client, app, coordinator.id)
    filter_url = (
        f"{school_queue}&class_id={class_id}&assignment_id={assignment.id}&student_id={student.id}"
    )
    assert coordinator_url in client.get(f"{filter_url}&status=awaiting").text
    assert "No submissions match these filters" in client.get(f"{filter_url}&status=complete").text
    assert client.get(f"{school_queue}&class_id={data['class2'].id}").status_code == 403
    feedback_form = {
        "csrf_token": coordinator_token,
        "message": "Show your units.",
        "idempotency_key": "coordinator-revision",
        "outcome": "revision",
        "from_detail": "true",
    }
    saved = client.post(f"{coordinator_url}/feedback", data=feedback_form)
    assert "Revision requested" in saved.text and "Reviewed by Local Coordinator" in saved.text
    assert coordinator_url in client.get(f"{filter_url}&status=revision").text
    assert coordinator_url not in client.get(f"{filter_url}&status=awaiting").text

    student_token = authenticate(client, app, student.id)
    page = client.get(student_url)
    assert all(
        text in page.text
        for text in ("Revision requested", "Reviewed by Local Coordinator", "Show your units.")
    )
    updated = client.post(
        f"/student/assignments/{assignment.id}/submit",
        data={
            "csrf_token": student_token,
            "idempotency_key": "web-second",
            "text_content": "Final reading: 16 millimetres.",
        },
        files={"file": ("notes.txt", b"Final notes", "text/plain")},
    )
    assert updated.status_code == 200 and "Attempt 2 · Latest" in updated.text
    second = db.scalar(select(Submission).where(Submission.idempotency_key == "web-second"))

    for role, actor, queue in (
        ("teacher", data["teacher1"], "/teacher/submissions"),
        ("coordinator", coordinator, f"{filter_url}&status=awaiting"),
    ):
        authenticate(client, app, actor.id)
        latest = f"/{role}/submissions/{second.id}"
        earlier = f"/{role}/submissions/{first.id}"
        listing = client.get(queue)
        assert latest in listing.text and earlier not in listing.text
        page = client.get(latest)
        assert all(
            text in page.text
            for text in (
                "Final reading: 16 millimetres.",
                "notes.txt",
                "Show your units.",
                "Attempt 2 · Latest",
                earlier,
            )
        )
        assert f'src="{latest}/file"' not in page.text
        assert client.get(f"{latest}/file?download=true").content == b"Final notes"
        old_page = client.get(earlier)
        assert original in old_page.text and "Earlier attempt" in old_page.text
        assert "Request revision</button>" not in old_page.text

    teacher_token = authenticate(client, app, data["teacher1"].id)
    stale = client.post(
        f"{teacher_url}/feedback",
        data={**feedback_form, "csrf_token": teacher_token, "idempotency_key": "stale-review"},
    )
    assert stale.status_code == 422 and "Show your units.</textarea>" in stale.text
    complete = client.post(
        f"/teacher/submissions/{second.id}/feedback",
        data={
            **feedback_form,
            "csrf_token": teacher_token,
            "outcome": "complete",
            "message": "Units are correct.",
            "idempotency_key": "teacher-complete",
        },
    )
    assert "Marked complete" in complete.text and "Reviewed by Teacher One" in complete.text
    assert f"/teacher/submissions/{second.id}" not in client.get("/teacher/submissions").text
    authenticate(client, app, coordinator.id)
    assert (
        f"/coordinator/submissions/{second.id}" in client.get(f"{filter_url}&status=complete").text
    )
    authenticate(client, app, student.id)
    assert "Units are correct." in client.get(student_url).text
    assert client.get(f"/student/submissions/{first.id}/file").content == PNG

    authenticate(client, app, data["teacher2"].id)
    assert teacher_url not in client.get("/teacher/submissions").text
    assert client.get(teacher_url).status_code == 403
    assert client.get(f"{teacher_url}/file").status_code == 403
    authenticate(client, app, outsider.id)
    assert client.get(school_queue).status_code == 403
    assert client.get(coordinator_url).status_code == 403
    assert client.get(f"{coordinator_url}/file?download=true").status_code == 403
    authenticate(client, app, data["student2"].id)
    assert original not in client.get(student_url).text
    assert client.get(teacher_url).status_code == 403
    assert client.get(f"/api/student/submissions/{first.id}").status_code == 403
    assert client.get(f"/student/submissions/{first.id}/file?download=true").status_code == 403


def test_telegram_work_preserves_text_and_attachment_caption(
    client, app, db, data, tmp_path, monkeypatch
):
    monkeypatch.setattr(app.state.settings, "upload_dir", tmp_path)
    monkeypatch.setattr(app.state.telegram_client, "download_file", lambda _file_id, _limit: PNG)
    assignment_id = data["assignment"].id
    text = 'First "reading"\n  12 mm. A quote: "'
    caption = "The image shows\n  the second reading."
    messages = [
        (201, {"text": f"/submit {assignment_id} {text}"}),
        (
            202,
            {
                "caption": f"/submit {assignment_id} {caption}",
                "document": {
                    "file_id": "rain-data",
                    "file_name": "gauge.png",
                    "mime_type": "image/png",
                },
            },
        ),
    ]
    ids = []
    for update_id, (sender, fields) in enumerate(messages, start=872):
        result = app.state.telegram_service.process(
            db,
            {
                "update_id": update_id,
                "message": {"from": {"id": sender}, "chat": {"id": sender}, **fields},
            },
        )
        ids.append(int(result["result"].removeprefix("submission:")))
    db.commit()
    assert db.get(Submission, ids[0]).text_content == text
    assert db.get(Submission, ids[1]).text_content == caption
    authenticate(client, app, data["teacher1"].id)
    queue = client.get("/teacher/submissions")
    assert all(f"/teacher/submissions/{item}" in queue.text for item in ids)
    assert caption in client.get(f"/teacher/submissions/{ids[1]}").text
    assert client.get(f"/teacher/submissions/{ids[1]}/file").content == PNG
