from datetime import UTC, datetime, timedelta

from app.models import Assignment, SchoolMembership, StudentAssignmentState
from app.services.progress import ProgressService
from tests.conftest import authenticate


def test_classwork_preview_and_scoped_queues(client, app, db, data):
    classroom = data["class1"]
    teacher = data["teacher1"]
    student = data["student1"]
    original = data["assignment"]
    ProgressService().update(db, student, original.id, "blocked", "I need help getting started.")
    for index in range(13):
        assignment = Assignment(
            school_id=classroom.school_id,
            classroom_id=classroom.id,
            created_by_user_id=teacher.id,
            title=f"Queue work {index:02}",
            instructions="Show your work.",
            due_at=datetime.now(UTC) + timedelta(hours=5),
            timezone="Asia/Kolkata",
            status="assigned",
        )
        db.add(assignment)
        db.flush()
        db.add(StudentAssignmentState(assignment_id=assignment.id, student_id=student.id))
    db.add(SchoolMembership(school_id=classroom.school_id, user_id=teacher.id, role="coordinator"))
    db.commit()
    authenticate(client, app, teacher.id)

    overview = client.get("/teacher").text
    assert overview.index('id="assigned-classes-title"') < overview.index('id="attention-title"')
    preview = overview.split('id="attention"')[1].split("</section>")[0]
    assert preview.count('class="list-row priority-row"') == 3
    assert "Help requested" in preview and "View all 14 attention items" in preview
    support_url = f"/teacher/classes/{classroom.id}/students/{student.id}?assignment_id={original.id}"
    assert support_url in preview
    assert 'href="/teacher/submissions" class="metric review-metric"' in overview

    queue = f"/teacher?class_id={classroom.id}&view="
    help_view = client.get(queue + "help").text
    assert support_url in help_view and "Queue work" not in help_view
    assert 'name="view" value="help"' in help_view
    attention = client.get(queue + "attention").text
    assert "Page 1 of 2" in attention and "Queue work 12" not in attention
    last_page = client.get(queue + "attention&page=2").text
    assert "Page 2 of 2" in last_page and "Queue work 12" in last_page
    due = client.get(queue + "due").text
    assert "13 assignments" in due and "Essay" not in due
    assert client.get(f"/teacher?class_id={data['class2'].id}&view=help").status_code == 403

    coordinator_help = f"/coordinator/classes/{classroom.id}/work?view=help"
    response = client.get(coordinator_help)
    assert response.status_code == 200 and support_url.replace("teacher", "coordinator", 1) in response.text
    assert client.get(f"/coordinator/classes/{data['class2'].id}/work?view=help").status_code == 403
