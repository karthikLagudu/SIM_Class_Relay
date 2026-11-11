from __future__ import annotations

import logging
import shlex
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.exceptions import DomainError, InviteError, TelegramLinkError
from app.llm.service import LLMService
from app.models import (
    Assignment,
    ProcessedTelegramUpdate,
    Reminder,
    School,
    StudentAssignmentState,
    User,
)
from app.services.assignment import AssignmentService
from app.services.authorization import require_student_assignment, require_teacher_class
from app.services.invite import InviteService
from app.services.notification import NotificationService
from app.services.progress import ProgressService
from app.services.submission import SubmissionService
from app.services.telegram_link import TelegramLinkService
from app.telegram.client import TelegramClient
from app.telegram.files import TelegramFileService
from app.telegram.router import TelegramIntentRouter

logger = logging.getLogger(__name__)
HELP = (
    "Connect with the secure link provided by Classroom Companion or your teacher.\n"
    "/join CODE - join a class from a linked student account\n\nCommands:\n"
    "/assign CLASS_ID instructions and deadline - teacher creates work\n"
    "/ack ASSIGNMENT_ID\n/progress ASSIGNMENT_ID 50 (or a written update)\n/blocked ASSIGNMENT_ID reason\n"
    "/submit ASSIGNMENT_ID text (or attach a file with this caption)\n/status\n/classes\n/reminders"
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
        self.files = TelegramFileService(client.settings, client)
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
        parts = shlex.split(text) if text.startswith("/") else []
        command = parts[0].lower().split("@", 1)[0] if parts else ""
        if command == "/start":
            if len(parts) == 1:
                if user:
                    self.client.send(
                        db,
                        user,
                        chat_id,
                        f"Classroom Companion is connected as {user.name}.\n\n{HELP}",
                        kind="help",
                    )
                    return "start_linked"
                self.client.send(
                    db,
                    None,
                    chat_id,
                    "Welcome to Classroom Companion.\n\n"
                    "To receive school notifications, use the secure Telegram connection "
                    "link provided through Classroom Companion or your teacher.",
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
            if not user or len(parts) != 2:
                raise InviteError("Connect your student account using its private link, then send /join CODE.")
            classroom = self.invites.join(db, user, parts[1], telegram_user_id, chat_id)
            self.client.send(db, user, chat_id, f"You are in {classroom.name}.", kind="joined")
            return f"joined:{classroom.id}"
        if not user:
            self.client.send(db, None, chat_id, HELP, kind="help")
            return "unlinked"
        if not user.telegram_chat_id:
            user.telegram_chat_id = chat_id
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
                kind="assignment_created",
            )
            return f"assignment:{assignment.id}"
        if command in {"/ack", "/progress", "/blocked"}:
            if len(parts) < 2:
                return "missing_assignment_id"
            assignment_id = int(parts[1])
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
            self.client.send(
                db,
                user,
                chat_id,
                f"Updated assignment #{assignment_id}: {status.replace('_', ' ')}.",
                kind="progress",
            )
            return f"progress:{assignment_id}:{status}"
        if command == "/submit":
            if len(parts) < 2:
                return "missing_assignment_id"
            assignment_id = int(parts[1])
            require_student_assignment(db, user, assignment_id)
            document = message.get("document")
            photos = message.get("photo") or []
            photo = photos[-1] if photos else None
            file_data = document or photo
            body = " ".join(parts[2:]) or None
            if not body and not file_data:
                self.intent_router.context.set(db, user, chat_id, assignment_id=assignment_id, pending_action="submission")
                self.client.send(db, user, chat_id, f"Send a photo or file, or type /submit {assignment_id} followed by your answer.", kind="submission_prompt")
                return f"pending_submission:{assignment_id}"
            stored_path = None
            original_filename = None
            mime_type = None
            content = None
            if file_data:
                stored = self.files.download(
                    str(file_data.get("file_id")),
                    document.get("file_name") if document else "telegram-photo.jpg",
                    document.get("mime_type") if document else "image/jpeg",
                )
                stored_path = stored.path
                original_filename = stored.original_filename
                mime_type = stored.mime_type
                content = stored.content
            submission = self.submissions.submit(
                db,
                user,
                assignment_id,
                f"telegram:{update_id}:submission",
                text_content=body if not file_data else None,
                telegram_file_id=str(file_data.get("file_id")) if file_data else None,
                stored_file_path=stored_path,
                original_filename=original_filename,
                mime_type=mime_type,
                content=content,
            )
            self.client.send(
                db, user, chat_id, f"Submission #{submission.id} received.", kind="submission"
            )
            return f"submission:{submission.id}"
        if command in {"/classes", "/reminders", "/status"}:
            classes = self.intent_router.teacher_classes(db, user)
            if command == "/classes":
                body = "\n".join(f"#{item.id} {item.name}" for item in classes)
                self.client.send(db, user, chat_id, body or "No teaching classes.", kind="classes")
                return "classes"
            if command == "/reminders":
                rows = db.execute(
                    select(Assignment.title, Reminder.status, Reminder.scheduled_for, Assignment.timezone)
                    .join(Assignment, Assignment.id == Reminder.assignment_id)
                    .where(Assignment.classroom_id.in_([item.id for item in classes]))
                    .order_by(Reminder.scheduled_for.desc()).limit(12)
                )
                body = "\n".join(
                    f"{title}: {status} · {self.notifications.format_deadline(due, tz)}"
                    for title, status, due, tz in rows
                )
                self.client.send(db, user, chat_id, body or "No teaching reminders.", kind="reminders")
                return "reminders"
            if classes:
                return self.intent_router.send_risk_summary(db, user, chat_id)
        if command == "/status":
            rows = db.execute(
                select(Assignment, StudentAssignmentState)
                .join(StudentAssignmentState, StudentAssignmentState.assignment_id == Assignment.id)
                .where(StudentAssignmentState.student_id == user.id)
            ).all()
            summary = (
                "\n".join(f"#{a.id} {a.title}: {s.status}, due {self.notifications.format_deadline(a.due_at, a.timezone)}" for a, s in rows)
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
