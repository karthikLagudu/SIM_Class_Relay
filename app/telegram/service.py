from __future__ import annotations

import logging
import shlex
from datetime import UTC, datetime

from sqlalchemy import literal, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.exceptions import DomainError, InviteError, TelegramLinkError
from app.llm.service import LLMService
from app.models import (
    Assignment,
    NotificationDelivery,
    ProcessedTelegramUpdate,
    Reminder,
    School,
    StudentAssignmentState,
    User,
)
from app.services.assignment import AssignmentService
from app.services.authorization import (
    list_teacher_classes,
    require_student_assignment,
    require_teacher_class,
    student_work_scope,
)
from app.services.invite import InviteService
from app.services.notification import NotificationService
from app.services.progress import ProgressService
from app.services.submission import SubmissionService
from app.services.telegram_link import TelegramLinkService
from app.status_labels import reminder_status, student_status
from app.telegram.client import TelegramClient
from app.telegram.files import TelegramAttachment
from app.telegram.router import TelegramIntentRouter

logger = logging.getLogger(__name__)
HELP = (
    "Connect with the secure link provided by Class Relay or your teacher.\n"
    "/join CODE [EMAIL] - join a class from a linked student account\n\nCommands:\n"
    "/assign CLASS_ID instructions and deadline - teacher creates work\n"
    "/ack ASSIGNMENT_ID\n/progress ASSIGNMENT_ID 50 (or a written update)\n/blocked ASSIGNMENT_ID reason\n"
    "/submit ASSIGNMENT_ID text (or attach a file with this caption)\n/status\n/classes\n/reminders\n/cancel - clear a pending chat request"
)


class TelegramService:
    def __init__(self, llm: LLMService, client: TelegramClient):
        self.llm = llm
        self.client = client
        self.notifications = NotificationService(client)
        self.assignments = AssignmentService(self.notifications)
        self.progress = ProgressService()
        self.submissions = SubmissionService()
        self.invites = InviteService()
        self.links = TelegramLinkService(client.settings)
        self.intent_router = TelegramIntentRouter(llm, client, self.notifications)

    def process(self, db: Session, update: dict) -> dict[str, str | bool]:
        update_id = update.get("update_id")
        if type(update_id) is not int or not 0 <= update_id <= 2**63 - 1:
            return {"ok": True, "result": "ignored_missing_update_id"}
        claimed = db.execute(
            insert(ProcessedTelegramUpdate)
            .values(telegram_update_id=update_id, result_reference="ignored")
            .on_conflict_do_nothing(index_elements=["telegram_update_id"])
        )
        if not claimed.rowcount:
            logger.info("duplicate_update_ignored update_id=%s", update_id)
            return {"ok": True, "duplicate": True}
        record = db.get(ProcessedTelegramUpdate, update_id)
        message = update.get("message")
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            callback_message = callback.get("message")
            data = callback.get("data")
            if not isinstance(callback_message, dict) or not isinstance(data, str):
                return {"ok": True, "result": "ignored"}
            message = {**callback_message, "from": callback.get("from")}
            action, _, value = data.partition(":")
            if action in {"ack", "blocked", "submit"}:
                message["text"] = f"/{action} {value}"
            elif action == "help":
                message["text"] = f"/blocked {value} I need help"
            else:
                message["text"] = "/help"
        if not isinstance(message, dict):
            return {"ok": True, "result": "ignored"}
        sender, chat = message.get("from"), message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return {"ok": True, "result": "ignored"}
        telegram_user_id = str(sender.get("id", ""))
        chat_id = str(chat.get("id", ""))
        if chat.get("type", "private") != "private" or not chat_id.isdigit() or chat_id != telegram_user_id:
            return {"ok": True, "result": "ignored_non_private"}
        raw_text = message.get("text") or message.get("caption") or ""
        if not isinstance(raw_text, str):
            return {"ok": True, "result": "ignored"}
        text = raw_text.strip()
        document, photos = message.get("document"), message.get("photo", [])
        if (document is not None and not isinstance(document, dict)) or (
            not isinstance(photos, list) or any(not isinstance(photo, dict) for photo in photos)
        ):
            return {"ok": True, "result": "ignored"}
        attachments = photos if document is None else [document, *photos]
        if any(not isinstance(item.get("file_id"), str) or not item["file_id"] for item in attachments):
            return {"ok": True, "result": "ignored"}
        user = db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        try:
            result = self._dispatch(db, user, telegram_user_id, chat_id, text, message, update_id)
        except DomainError as exc:
            self.client.send(db, user, chat_id, str(exc), kind="error")
            result = f"error:{type(exc).__name__}"
        except ValueError:
            self.client.send(
                db,
                user,
                chat_id,
                "Check the command format and use numeric assignment/class IDs. Send /help for examples.",
                kind="error",
            )
            result = "error:invalid_command_format"
        record.result_reference = result
        logger.info("telegram_update_processed update_id=%s result=%s", update_id, result)
        return {"ok": True, "result": result}

    def _dispatch(
        self,
        db: Session,
        user: User | None,
        telegram_user_id: str,
        chat_id: str,
        text: str,
        message: dict,
        update_id: int,
    ) -> str:
        command_word = text.split(maxsplit=1)[0].lower().split("@", 1)[0] if text else ""
        parts = text.split(maxsplit=2) if command_word == "/submit" else (
            shlex.split(text) if text.startswith("/") else []
        )
        command = parts[0].lower().split("@", 1)[0] if parts else ""
        if command == "/start":
            if len(parts) == 1:
                if user:
                    self.client.send(
                        db,
                        user,
                        chat_id,
                        f"Class Relay is connected as {user.name}.\n\n{HELP}",
                        kind="help",
                    )
                    return "start_linked"
                self.client.send(
                    db,
                    None,
                    chat_id,
                    "Welcome to Class Relay.\n\n"
                    "To receive school notifications, use the secure Telegram connection "
                    "link provided through Class Relay or your teacher.",
                    kind="help",
                )
                return "start_unlinked"
            if len(parts) != 2:
                self.client.send(
                    db,
                    user,
                    chat_id,
                    "❌ This Telegram connection link is invalid.\n\n"
                    "Please request a new connection link.",
                    kind="telegram_link_error",
                )
                return "telegram_link:invalid"
            if message.get("chat", {}).get("type") not in {None, "private"}:
                self.client.send(
                    db,
                    user,
                    chat_id,
                    "For your privacy, open this connection link in a private chat with the bot.",
                    kind="telegram_link_error",
                )
                return "telegram_link:non_private"
            try:
                target = self.links.consume_token(
                    db, parts[1], telegram_user_id, chat_id
                )
            except TelegramLinkError as exc:
                if exc.code == "expired":
                    response = (
                        "⏰ This Telegram connection link has expired.\n\n"
                        "Please request a new connection link."
                    )
                elif exc.code == "used":
                    response = (
                        "❌ This Telegram connection link has already been used.\n\n"
                        "Please request a new connection link."
                    )
                elif exc.code in {"telegram_conflict", "student_conflict"}:
                    response = f"❌ {exc}"
                else:
                    response = (
                        "❌ This Telegram connection link is invalid.\n\n"
                        "Please request a new connection link."
                    )
                self.client.send(
                    db, user, chat_id, response, kind="telegram_link_error"
                )
                return f"telegram_link:{exc.code}"
            self.client.send(
                db,
                target,
                chat_id,
                "✅ Telegram connected successfully.\n\n"
                f"Hello {target.name}.\n\n"
                "You will now receive homework, assignment and reminder notifications here.",
                kind="telegram_connected",
                idempotency_key=f"telegram-connected:{update_id}",
            )
            return f"telegram_link:connected:{target.id}"
        if command == "/join":
            if not user or len(parts) not in {2, 3}:
                raise InviteError("Connect your student account using its private link, then send /join CODE.")
            if len(parts) == 3 and parts[2].casefold() != user.email.casefold():
                raise InviteError("The email must match your connected classroom account.")
            classroom = self.invites.join(db, user, parts[1], telegram_user_id, chat_id)
            self.client.send(db, user, chat_id, f"You are in {classroom.name}.", kind="joined")
            return f"joined:{classroom.id}"
        if not user:
            self.client.send(db, None, chat_id, HELP, kind="help")
            return "unlinked"
        if not user.telegram_chat_id:
            user.telegram_chat_id = chat_id
        if command == "/cancel" and len(parts) == 1:
            self.intent_router.context.clear(db, user, chat_id)
            self.client.send(db, user, chat_id, "Cleared the pending chat request.", kind="help")
            return "context_cleared"
        if command == "/help":
            self.client.send(db, user, chat_id, HELP, kind="help")
            return "help"
        if command == "/assign":
            if len(parts) < 3:
                self.client.send(
                    db, user, chat_id, "Use /assign CLASS_ID task with deadline", kind="help"
                )
                return "assign_help"
            class_id = int(parts[1])
            natural_text = " ".join(parts[2:])
            classroom = require_teacher_class(db, user, class_id)
            school = db.get(School, classroom.school_id)
            parsed = self.llm.assignment(db, user.id, natural_text, school.timezone, datetime.now(UTC))
            assignment = self.assignments.create(
                db,
                user,
                class_id,
                parsed.title,
                parsed.instructions,
                parsed.due_at,
                school.timezone,
                f"telegram:{update_id}:assignment",
            )
            self.client.send(
                db,
                user,
                chat_id,
                f"Created #{assignment.id}: {assignment.title}, due {assignment.due_at}.",
                kind="teacher_confirmation",
                school_id=assignment.school_id,
                classroom_id=assignment.classroom_id,
                assignment_id=assignment.id,
            )
            return f"assignment:{assignment.id}"
        if command in {"/ack", "/progress", "/blocked"}:
            if len(parts) < 2:
                return "missing_assignment_id"
            assignment_id = int(parts[1])
            require_student_assignment(db, user, assignment_id)
            status = {"/ack": "acknowledged", "/progress": "in_progress", "/blocked": "blocked"}[
                command
            ]
            body = " ".join(parts[2:]) or status.replace("_", " ")
            percent = None
            if command == "/progress" and parts[2:]:
                if len(parts) == 3 and parts[2].isascii() and parts[2].isdigit():
                    percent = int(parts[2])
                    if not 0 <= percent <= 100:
                        raise ValueError("Percentage must be 0 to 100")
                else:
                    interpreted = self.llm.progress(db, user.id, body)
                    status, percent = interpreted.status, interpreted.progress_percent
            self.progress.update(db, user, assignment_id, status, body, percent)
            context = self.intent_router.context.get(db, user, chat_id)
            if not context or not context.pending_action:
                self.intent_router.context.set(db, user, chat_id, assignment_id=assignment_id)
            self.client.send(
                db,
                user,
                chat_id,
                f"Updated assignment #{assignment_id}: {student_status(status)}.",
                kind="progress",
            )
            return f"progress:{assignment_id}:{status}"
        if command == "/submit":
            if len(parts) < 2:
                return "missing_assignment_id"
            assignment_id = int(parts[1])
            require_student_assignment(db, user, assignment_id)
            attachment = TelegramAttachment.from_message(message)
            body = parts[2] if len(parts) > 2 else None
            if attachment:
                if not message.get("text"):
                    attachment = attachment.model_copy(update={"caption": body})
                return self.intent_router.student.submit_attachment(
                    db, user, chat_id, assignment_id, attachment, update_id
                )
            if not body:
                self.intent_router.context.set(db, user, chat_id, assignment_id=assignment_id, pending_action="submission")
                self.client.send(db, user, chat_id, f"Send a photo or file, or type /submit {assignment_id} followed by your answer.", kind="submission_prompt")
                return f"pending_submission:{assignment_id}"
            submission = self.submissions.submit(
                db, user, assignment_id, f"telegram:{update_id}:submission", text_content=body,
            )
            self.intent_router.context.set(db, user, chat_id, assignment_id=assignment_id)
            self.client.send(db, user, chat_id, f"Submission #{submission.id} received.", kind="submission")
            return f"submission:{submission.id}"
        if command in {"/classes", "/reminders", "/status"}:
            classes = list_teacher_classes(db, user)
            if command == "/classes":
                body = "\n".join(f"#{item.id} {item.name}" for item in classes)
                self.client.send(db, user, chat_id, body or "No teaching classes.", kind="classes")
                return "classes"
            if command == "/reminders":
                rows = db.execute(
                    select(Assignment, Reminder, NotificationDelivery.status)
                    .select_from(Reminder)
                    .join(Assignment, Assignment.id == Reminder.assignment_id)
                    .outerjoin(
                        NotificationDelivery,
                        NotificationDelivery.idempotency_key == literal("reminder:") + Reminder.dedupe_key,
                    )
                    .where(Assignment.classroom_id.in_([item.id for item in classes]))
                    .order_by(Reminder.scheduled_for.desc()).limit(12)
                )
                body = "\n".join(
                    f"{assignment.title}: {reminder_status(reminder.status, bool(reminder.processed_at), delivery)}"
                    f" · {self.notifications.format_deadline(reminder.scheduled_for, assignment.timezone)}"
                    for assignment, reminder, delivery in rows
                )
                self.client.send(db, user, chat_id, body or "No teaching reminders.", kind="reminders")
                return "reminders"
            if classes:
                return self.intent_router.teacher.send_risk_summary(db, user, chat_id)
        if command == "/status":
            rows = db.execute(
                select(Assignment, StudentAssignmentState)
                .join(StudentAssignmentState, StudentAssignmentState.assignment_id == Assignment.id)
                .where(StudentAssignmentState.student_id == user.id, Assignment.id.in_(student_work_scope(user.id)))
            ).all()
            summary = (
                "\n".join(f"#{a.id} {a.title}: {student_status(s.status)}, due {self.notifications.format_deadline(a.due_at, a.timezone)}" for a, s in rows)
                or "No assignments."
            )
            self.client.send(db, user, chat_id, summary, kind="status")
            return "status"
        if command.startswith("/"):
            self.client.send(
                db, user, chat_id, "I did not recognize that command.\n\n" + HELP, kind="help"
            )
            return "unknown"
        return self.intent_router.route(db, user, chat_id, text, message, update_id)
