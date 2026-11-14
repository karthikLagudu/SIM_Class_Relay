from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Assignment,
    AssignmentTarget,
    ClassMembership,
    Classroom,
    NotificationDelivery,
    School,
    SchoolMembership,
    StudentSupportReply,
    Submission,
    User,
)
from app.telegram.client import TelegramClient, telegram_text_parts


class NotificationService:
    """Canonical assignment notification boundary for web, Telegram, and workers."""

    def __init__(self, telegram: TelegramClient):
        self.telegram = telegram

    @staticmethod
    def format_deadline(value: datetime, timezone_name: str) -> str:
        aware = value.replace(tzinfo=value.tzinfo or UTC)
        return aware.astimezone(ZoneInfo(timezone_name)).strftime("%A, %d %B %Y at %I:%M %p %Z")

    def students(self, db: Session, assignment: Assignment) -> list[User]:
        return list(
            db.scalars(
                select(User)
                .join(AssignmentTarget, AssignmentTarget.student_id == User.id)
                .join(ClassMembership, ClassMembership.user_id == User.id)
                .where(
                    AssignmentTarget.assignment_id == assignment.id,
                    ClassMembership.classroom_id == assignment.classroom_id,
                    ClassMembership.role == "student",
                )
            )
        )

    def assignment_message(self, assignment: Assignment, classroom: Classroom) -> str:
        instructions = assignment.instructions
        if len(instructions) > 1500:
            instructions = instructions[:1500] + "… Open Class Relay for the full instructions."
        return (
            f"{assignment.title}\n"
            f"{classroom.name} · Due {self.format_deadline(assignment.due_at, assignment.timezone)}"
            f"\n\n{instructions}"
        )

    @staticmethod
    def assignment_actions(assignment_id: int, status: str = "assigned") -> dict | None:
        rows = []
        if status == "assigned":
            rows.append([{"text": "Mark as read", "callback_data": f"ack:{assignment_id}"}])
        actions = []
        if status not in {"submitted", "completed", "cancelled"}:
            actions.append({"text": "Turn in work", "callback_data": f"submit:{assignment_id}"})
        if status in {"assigned", "acknowledged", "in_progress", "blocked", "overdue"}:
            actions.append({"text": "Ask for help", "callback_data": f"ask:{assignment_id}"})
        if actions:
            rows.append(actions)
        if status in {"assigned", "acknowledged", "in_progress", "blocked", "overdue", "needs_revision"}:
            rows.append([{"text": "Update progress", "callback_data": f"progress:{assignment_id}"}])
        return {"inline_keyboard": rows} if rows else None

    def _send(
        self,
        db: Session,
        assignment: Assignment,
        student: User,
        kind: str,
        body: str,
        key: str,
        *,
        reply_markup: dict | None = None,
    ) -> NotificationDelivery:
        return self.telegram.send(
            db,
            student,
            None,
            body,
            kind=kind,
            reply_markup=reply_markup,
            school_id=assignment.school_id,
            classroom_id=assignment.classroom_id,
            assignment_id=assignment.id,
            idempotency_key=key,
        )

    def notify_assignment_created(
        self, db: Session, assignment: Assignment
    ) -> list[NotificationDelivery]:
        classroom = db.get(Classroom, assignment.classroom_id)
        body = "📚 New Homework Assigned\n\n" + self.assignment_message(assignment, classroom)
        deliveries = []
        for student in self.students(db, assignment):
            deliveries.append(
                self._send(
                    db,
                    assignment,
                    student,
                    "assignment_created",
                    body,
                    f"assignment-created:{assignment.id}:{student.id}",
                    reply_markup=self.assignment_actions(assignment.id),
                )
            )
        return deliveries

    def notify_deadline_changed(
        self, db: Session, assignment: Assignment, old_deadline: datetime
    ) -> list[NotificationDelivery]:
        body = (
            f"Deadline updated for {assignment.title}:\n"
            f"Old: {self.format_deadline(old_deadline, assignment.timezone)}\n"
            f"New: {self.format_deadline(assignment.due_at, assignment.timezone)}"
        )
        return [
            self._send(
                db,
                assignment,
                student,
                "deadline_updated",
                body,
                f"deadline:{assignment.id}:v{assignment.schedule_version}:{student.id}",
            )
            for student in self.students(db, assignment)
        ]

    def notify_instructions_changed(
        self, db: Session, assignment: Assignment, operation_key: str
    ) -> list[NotificationDelivery]:
        body = f"Your teacher clarified {assignment.title}:\n{assignment.instructions}"
        return [
            self._send(
                db,
                assignment,
                student,
                "instructions_updated",
                body,
                f"instructions:{operation_key}:{student.id}",
            )
            for student in self.students(db, assignment)
        ]

    def notify_assignment_cancelled(
        self, db: Session, assignment: Assignment
    ) -> list[NotificationDelivery]:
        body = f"Assignment cancelled: {assignment.title}. No further action is required."
        return [
            self._send(
                db,
                assignment,
                student,
                "assignment_cancelled",
                body,
                f"cancelled:{assignment.id}:{student.id}",
            )
            for student in self.students(db, assignment)
        ]

    def notify_feedback(
        self, db: Session, assignment: Assignment, student: User, message: str, key: str,
        *, complete: bool,
    ) -> NotificationDelivery:
        outcome = "Marked complete" if complete else "Revision requested"
        return self._send(
            db,
            assignment,
            student,
            "feedback",
            f"{outcome} · {assignment.title}\n\n{message}",
            key,
            reply_markup={
                "inline_keyboard": [[{
                    "text": "Open assignment", "callback_data": f"assignment:{assignment.id}",
                }]]
            },
        )

    def notify_student_support(
        self, db: Session, assignment: Assignment, student: User, teacher: User,
        classroom: Classroom, reply: StudentSupportReply,
    ) -> NotificationDelivery:
        school = db.get(School, assignment.school_id)
        deadline = self.format_deadline(assignment.due_at, assignment.timezone)
        key = f"support-reply:{reply.id}"
        body = (
            f"Help from {teacher.name}\n"
            f"For {student.name}\n"
            f"School: {school.name}\n"
            f"Class: {classroom.name}\n"
            f"Assignment: {assignment.title}\n"
            f"Due: {deadline}"
        )
        if reply.original_filename:
            body += f"\nAttachment: {reply.original_filename}"
        if reply.message:
            body += f"\n\nTeacher’s note\n{reply.message}"
        first, *remaining = telegram_text_parts(body)
        actions = {"inline_keyboard": [[{
            "text": "Open assignment", "callback_data": f"assignment:{assignment.id}",
        }]]}
        delivery = self._send(
            db, assignment, student, "student_support", first, key,
            reply_markup=None if remaining else actions,
        )
        for index, part in enumerate(remaining, start=2):
            self._send(
                db, assignment, student, "student_support", part, f"{key}:text:{index}",
                reply_markup=actions if index == len(remaining) + 1 else None,
            )
        if reply.stored_file_path:
            caption = f"Help attachment from {teacher.name}\n{classroom.name} · {assignment.title}"
            self._send(
                db, assignment, student, "student_support",
                next(telegram_text_parts(caption, limit=1024)), f"{key}:attachment",
            )
        return delivery

    def notify_staff(
        self, db: Session, assignment: Assignment, student: User, kind: str, event_id: int,
    ) -> None:
        teachers = select(ClassMembership.user_id).where(
            ClassMembership.classroom_id == assignment.classroom_id, ClassMembership.role == "teacher"
        )
        coordinators = select(SchoolMembership.user_id).where(
            SchoolMembership.school_id == assignment.school_id, SchoolMembership.role == "coordinator"
        )
        recipients = db.scalars(
            select(User).where(
                User.id.in_(teachers.union(coordinators)), User.id != student.id,
                User.telegram_chat_id.is_not(None),
            )
        ).all()
        classroom = db.get(Classroom, assignment.classroom_id)
        if kind == "staff_submission":
            label = f"Work turned in · {student.name}"
            detail = f"Open the review queue in Class Relay to read submission #{event_id}."
        else:
            label = f"Help requested · {student.name}"
            detail = "Open Class Relay to read the student's note."
        for recipient in recipients:
            self._send(
                db, assignment, recipient, kind,
                f"{label}\n{assignment.title} · {classroom.name}\n\n{detail}",
                f"{kind}:{event_id}:{recipient.id}",
            )

    def notify_submission_receipt(
        self, db: Session, assignment: Assignment, student: User, submission: Submission,
    ) -> None:
        if student.telegram_chat_id:
            self._send(
                db, assignment, student, "submission_receipt",
                f"Work turned in · {assignment.title}\nYour submission #{submission.id} was received.",
                f"submission-receipt:{submission.id}",
            )

    def notify_reminder(
        self,
        db: Session,
        assignment: Assignment,
        student: User,
        reminder_type: str,
        body: str,
        key: str,
    ) -> NotificationDelivery:
        return self._send(db, assignment, student, reminder_type, body, key)
