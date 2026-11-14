from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import begin_sqlite_write
from app.exceptions import AuthorizationError, NotFoundError, TelegramDeliveryError
from app.models import (
    Assignment,
    Classroom,
    NotificationDelivery,
    ProgressEvent,
    Reminder,
    School,
    StudentAssignmentState,
    StudentSupportReply,
    Submission,
    User,
)
from app.reminders.policy import aware_utc, decide_reminder, in_quiet_hours, next_permitted_time
from app.services.authorization import (
    has_school_role,
    require_student_assignment,
    require_teacher_assignment,
)
from app.telegram.client import TelegramClient

logger = logging.getLogger(__name__)
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))
CLAIM_TIME = timedelta(minutes=2)
STAFF_ALERTS = {"staff_submission", "staff_help"}


@dataclass(frozen=True)
class DeliveryAttempt:
    id: int
    chat_id: str
    body: str
    reply_markup: dict | None
    reserved_until: datetime
    document: tuple[Path, str, str] | None = None


class DeliveryProcessor:
    """Read committed delivery intent and run Bot API calls outside the SQLite writer lock."""

    def __init__(self, telegram: TelegramClient, session_factory: sessionmaker[Session]):
        self.telegram = telegram
        self.session_factory = session_factory

    def _due(self, now: datetime):
        return (
            NotificationDelivery.status.in_(["pending", "failed"]),
            NotificationDelivery.attempt_count < self.telegram.settings.notification_max_attempts,
            or_(
                NotificationDelivery.next_attempt_at <= now,
                (NotificationDelivery.status == "pending")
                & NotificationDelivery.next_attempt_at.is_(None),
            ),
        )

    def run(self, now: datetime | None = None) -> list[int]:
        if self.telegram.settings.telegram_mode != "real":
            return []
        with self.session_factory() as db:
            ids = list(
                db.scalars(
                    select(NotificationDelivery.id)
                    .where(*self._due(now or datetime.now(UTC)))
                    .order_by(NotificationDelivery.id)
                    .limit(20)
                )
            )
        attempted: list[int] = []
        for delivery_id in ids:
            attempt = self._claim(delivery_id, now or datetime.now(UTC))
            if not attempt or not self._still_reserved(attempt):
                continue
            try:
                if attempt.document:
                    path, filename, mime_type = attempt.document
                    external_id = self.telegram.send_document(
                        attempt.chat_id, path, filename, mime_type, attempt.body,
                    )
                else:
                    external_id = self.telegram.send_message(
                        attempt.chat_id, attempt.body, attempt.reply_markup
                    )
            except TelegramDeliveryError as exc:
                self._finish(attempt, None, str(exc), now or datetime.now(UTC))
            else:
                self._finish(attempt, external_id, None, now or datetime.now(UTC))
            attempted.append(delivery_id)
        return attempted

    def _still_reserved(self, attempt: DeliveryAttempt) -> bool:
        # A removal can cancel a claim while the worker is between transactions.
        with self.session_factory() as db:
            row = db.get(NotificationDelivery, attempt.id)
            return bool(
                row and row.next_attempt_at
                and aware_utc(row.next_attempt_at) == attempt.reserved_until
            )

    @staticmethod
    def _reminder(db: Session, delivery: NotificationDelivery) -> Reminder | None:
        key = delivery.idempotency_key or ""
        if key.startswith("reminder:"):
            return db.scalar(
                select(Reminder).where(Reminder.dedupe_key == key.removeprefix("reminder:"))
            )
        return None

    def _eligible(self, db: Session, delivery: NotificationDelivery) -> Assignment | None:
        user = db.get(User, delivery.user_id) if delivery.user_id else None
        if not delivery.chat_id or (user and user.telegram_chat_id != delivery.chat_id):
            raise AuthorizationError("The Telegram recipient has changed")
        if not delivery.assignment_id:
            return None
        if not user:
            raise AuthorizationError("The Telegram recipient no longer exists")
        if delivery.kind in STAFF_ALERTS:
            assignment = self._staff_assignment(db, user, delivery)
        elif delivery.kind == "teacher_confirmation":
            assignment = require_teacher_assignment(db, user, delivery.assignment_id)
        else:
            assignment = require_student_assignment(db, user, delivery.assignment_id)
        if db.get(Classroom, assignment.classroom_id).archived_at:
            raise AuthorizationError("The class was archived")
        if assignment.status == "cancelled" and delivery.kind not in {
            "assignment_cancelled",
            "teacher_confirmation",
        }:
            raise AuthorizationError("The assignment was cancelled")
        if delivery.kind == "deadline_updated" and not (delivery.idempotency_key or "").startswith(
            f"deadline:{assignment.id}:v{assignment.schedule_version}:"
        ):
            raise AuthorizationError("The deadline has changed")
        return assignment

    @staticmethod
    def _support_document(db: Session, delivery: NotificationDelivery) -> tuple[Path, str, str] | None:
        key = delivery.idempotency_key or ""
        if delivery.kind != "student_support" or not key.endswith(":attachment"):
            return None
        prefix, _, suffix = key.partition(":")
        reply_id, _, _ = suffix.partition(":")
        if prefix != "support-reply" or not reply_id.isascii() or not reply_id.isdigit() or len(reply_id) > 19:
            raise NotFoundError("The help attachment was not found")
        reply = db.get(StudentSupportReply, int(reply_id))
        if not reply or not reply.stored_file_path or (reply.student_id, reply.assignment_id) != (
            delivery.user_id, delivery.assignment_id,
        ):
            raise NotFoundError("The help attachment was not found for this student")
        return (
            Path(reply.stored_file_path), reply.original_filename or "Help attachment",
            reply.mime_type or "application/octet-stream",
        )

    @staticmethod
    def _staff_assignment(
        db: Session, recipient: User, delivery: NotificationDelivery,
    ) -> Assignment:
        assignment = db.get(Assignment, delivery.assignment_id)
        if not assignment:
            raise NotFoundError("Assignment not found")
        if not has_school_role(db, recipient.id, assignment.school_id, {"coordinator"}):
            require_teacher_assignment(db, recipient, assignment.id)
        _, _, suffix = (delivery.idempotency_key or "").partition(":")
        event_id, _, _ = suffix.partition(":")
        if not event_id.isascii() or not event_id.isdigit() or len(event_id) > 19:
            raise NotFoundError("Class activity not found")
        model = Submission if delivery.kind == "staff_submission" else ProgressEvent
        event = db.get(model, int(event_id))
        if not event or event.assignment_id != assignment.id:
            raise NotFoundError("Class activity not found")
        student = db.get(User, event.student_id)
        if not student:
            raise NotFoundError("Student not found")
        require_student_assignment(db, student, assignment.id)
        return assignment

    def _validate_reminder(
        self, db: Session, reminder: Reminder, assignment: Assignment, now: datetime
    ) -> datetime | None:
        state = db.scalar(
            select(StudentAssignmentState).where(
                StudentAssignmentState.assignment_id == assignment.id,
                StudentAssignmentState.student_id == reminder.student_id,
            )
        )
        if (
            reminder.schedule_version != assignment.schedule_version
            or reminder.status == "cancelled"
            or not state
            or decide_reminder(state, assignment, now).reminder_type != reminder.reminder_type
        ):
            raise AuthorizationError("The reminder is obsolete")
        timezone = db.get(School, assignment.school_id).timezone
        settings = self.telegram.settings
        start, end = settings.quiet_hour_start, settings.quiet_hour_end
        if in_quiet_hours(now.astimezone(ZoneInfo(timezone)), start, end):
            return next_permitted_time(now, timezone, start, end)
        return None

    def _claim(self, delivery_id: int, now: datetime) -> DeliveryAttempt | None:
        with self.session_factory.begin() as db:
            begin_sqlite_write(db)
            delivery = db.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.id == delivery_id, *self._due(now)
                )
            )
            if not delivery:
                return None
            reminder = self._reminder(db, delivery)
            try:
                assignment = self._eligible(db, delivery)
                document = self._support_document(db, delivery)
                if reminder and assignment:
                    deferred_until = self._validate_reminder(db, reminder, assignment, now)
                    if deferred_until:
                        delivery.next_attempt_at = deferred_until
                        return None
            except (AuthorizationError, NotFoundError) as exc:
                delivery.status = "skipped"
                delivery.error = str(exc)
                delivery.next_attempt_at = None
                self._record_reminder(reminder, delivery.status, now)
                return None
            reserved_until = now + CLAIM_TIME
            delivery.next_attempt_at = reserved_until
            return DeliveryAttempt(
                delivery.id, delivery.chat_id, delivery.body, delivery.reply_markup, reserved_until,
                document,
            )

    def _finish(
        self, attempt: DeliveryAttempt, external_id: str | None, error: str | None, now: datetime
    ) -> None:
        with self.session_factory.begin() as db:
            begin_sqlite_write(db)
            row = db.get(NotificationDelivery, attempt.id)
            if (
                not row
                or not row.next_attempt_at
                or aware_utc(row.next_attempt_at) != attempt.reserved_until
            ):
                return
            row.attempt_count += 1
            row.last_attempt_at = now
            row.error = error
            row.external_message_id = external_id
            row.status = "failed" if error else "sent"
            row.next_attempt_at = None
            if error and row.attempt_count < self.telegram.settings.notification_max_attempts:
                delay = RETRY_DELAYS[min(row.attempt_count - 1, len(RETRY_DELAYS) - 1)]
                row.next_attempt_at = now + delay
            self._record_reminder(self._reminder(db, row), row.status, now)
            logger.info(
                "notification_%s delivery_id=%s assignment_id=%s attempt=%s",
                row.status,
                row.id,
                row.assignment_id,
                row.attempt_count,
            )

    @staticmethod
    def _record_reminder(reminder: Reminder | None, status: str, now: datetime) -> None:
        if reminder and reminder.status != "cancelled":
            reminder.status = status
            reminder.sent_at = now if status == "sent" else None
