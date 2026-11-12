from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from itertools import count
from time import time_ns
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings


class HiddenFields(HTMLParser):
    def __init__(self, html: str, action: str):
        super().__init__()
        self.action = action
        self.inside = False
        self.fields: dict[str, str] = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.inside = attributes.get("action", "").partition("#")[0] == self.action
        if tag == "input" and self.inside and attributes.get("type") == "hidden":
            self.fields[attributes["name"]] = attributes["value"]

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


def login(client: httpx.Client, email: str, password: str) -> str:
    response = client.post("/login", data={"email": email, "password": password})
    if response.status_code != 303:
        raise RuntimeError(f"Sign-in failed for the seeded account {email}")
    return client.get("/", follow_redirects=True).raise_for_status().text


def run_demo(base_url: str) -> None:
    settings = get_settings()
    with (
        httpx.Client(base_url=base_url, timeout=15) as teacher,
        httpx.Client(base_url=base_url, timeout=15) as student,
        httpx.Client(base_url=base_url, timeout=15) as silent,
    ):
        health = teacher.get("/health").raise_for_status().json()
        if health != {"status": "ok", "llm_mode": "fake", "telegram_mode": "log"}:
            raise RuntimeError("The offline demo requires a seeded fake-LLM/log-Telegram server.")
        dashboard = login(teacher, "teacher@demo.local", "Teacher123!")
        login(student, "student1@demo.local", "Student123!")
        login(silent, "student2@demo.local", "Student123!")
        assert "Grade 8 - A" in dashboard and "Grade 9 Mathematics" not in dashboard
        assert teacher.get("/api/teacher/classes/2").status_code == 403
        print("Verified health, sign-ins, and teacher scope.")

        update_ids = count(time_ns() // 1000)
        endpoint = f"/telegram/webhook/{settings.telegram_webhook_secret}"

        def send(actor: int, message: str, update_id: int | None = None) -> dict:
            return (
                teacher.post(
                    endpoint,
                    json={
                        "update_id": update_id or next(update_ids),
                        "message": {
                            "from": {"id": actor},
                            "chat": {"id": actor, "type": "private"},
                            "text": message,
                        },
                    },
                )
                .raise_for_status()
                .json()
            )

        create_id = next(update_ids)
        response = send(
            900001,
            "Give Grade 8-A questions 1-10 from the energy worksheet by tomorrow evening.",
            create_id,
        )
        result = response.get("result", "")
        assert result.startswith("assignment:"), response
        assignment_id = int(result.split(":")[-1])
        assert send(
            900001,
            "Give Grade 8-A questions 1-10 from the energy worksheet by tomorrow evening.",
            create_id,
        )["duplicate"]
        local_zone = ZoneInfo("Asia/Kolkata")
        tomorrow = datetime.now(local_zone) + timedelta(days=1)
        expected = tomorrow.replace(hour=18, minute=0, second=0, microsecond=0)
        detail_url = f"/teacher/assignments/{assignment_id}"
        detail = teacher.get(detail_url).raise_for_status().text
        assert expected.strftime("%d %B %Y at %I:%M %p %Z") in detail
        assert all(label in detail for label in ("Assignment created", "Simulated", "Skipped"))
        print(
            "Verified relative deadline, student delivery records, and webhook replay protection."
        )

        moved = send(900001, "Move the energy worksheet to next Friday at 6 PM.")
        if moved["result"] == "teacher_needs_context":
            moved = send(900001, f"#{assignment_id}")
        assert moved["result"] == f"update_assignment_deadline:{assignment_id}", moved
        detail = teacher.get(detail_url).raise_for_status().text
        assert "Deadline updated" in detail and "Cancelled" in detail
        deadline_path = f"{detail_url}/deadline"
        due = (datetime.now(UTC) + timedelta(minutes=75)).astimezone(local_zone)
        form = HiddenFields(detail, deadline_path).fields
        form["due_at"] = due.strftime("%Y-%m-%dT%H:%M")
        updated = (
            teacher.post(deadline_path, data=form, follow_redirects=True).raise_for_status().text
        )
        expected = due.strftime("%d %B %Y at %I:%M %p %Z")
        student_url = f"/student/assignments/{assignment_id}"
        assert expected in updated and expected in student.get(student_url).raise_for_status().text
        assert "Deadline updated" in updated and "Cancelled" in updated
        assert send(900101, "/cancel")["result"] == "context_cleared"
        for message, state in (
            ("Got it", "acknowledged"),
            ("I'm halfway done", "in_progress"),
            ("I'm stuck on question 4", "blocked"),
        ):
            outcome = send(900101, message)
            if outcome["result"] == "student_needs_context":
                outcome = send(900101, f"#{assignment_id}")
            assert outcome["result"] == f"progress:{assignment_id}:{state}", outcome

        reminder_path = "/teacher/reminders/run"
        dashboard = teacher.get("/teacher").raise_for_status().text
        form = HiddenFields(dashboard, reminder_path).fields
        teacher.post(reminder_path, data=form, follow_redirects=True).raise_for_status()
        detail = teacher.get(detail_url).raise_for_status().text
        assert "blocked support" in detail.lower() and "silent due soon" in detail.lower(), (
            "For a time-independent offline run, set QUIET_HOUR_START=0 and QUIET_HOUR_END=0."
        )
        print(
            "Verified deadline rescheduling, progress, and distinct blocked and silent reminders."
        )

        submit_id = next(update_ids)
        message = f"/submit {assignment_id} My solution is x = 5."
        submission_id = int(send(900101, message, submit_id)["result"].split(":")[-1])
        assert send(900101, message, submit_id)["duplicate"]
        assert silent.get(f"/submissions/{submission_id}/file").status_code == 403
        detail = teacher.get(detail_url).raise_for_status().text
        feedback_path = f"/teacher/submissions/{submission_id}/feedback"
        form = HiddenFields(detail, feedback_path).fields
        form.update({"message": "Good work. Your reasoning checks out.", "outcome": "complete"})
        finished = (
            teacher.post(feedback_path, data=form, follow_redirects=True).raise_for_status().text
        )
        own_page = student.get(student_url).raise_for_status().text
        assert "Marked complete" in finished and "Good work. Your reasoning checks out." in own_page
        assert (
            "Good work. Your reasoning checks out."
            not in silent.get(student_url).raise_for_status().text
        )
        print(
            f"Verified submission, feedback, final status, and student isolation for assignment #{assignment_id}."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the local Class Relay acceptance scenario."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    run_demo(args.base_url)


if __name__ == "__main__":
    main()
