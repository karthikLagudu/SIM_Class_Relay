from __future__ import annotations

import logging
import shlex

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.exceptions import (
    AuthorizationError,
    DomainError,
    InviteError,
    TelegramLinkError,
    ValidationError,
)
from app.llm.service import LLMService
from app.models import (
    Assignment,
    Classroom,
    IdempotencyKey,
    NotificationDelivery,
    ProcessedTelegramUpdate,
    Reminder,
    SchoolMembership,
    StudentAssignmentState,
    User,
)
from app.services.assignment import get_student_state
from app.services.assignment_reference import explicit_id
from app.services.authorization import (
    coordinated_schools,
    coordinator_class_scope,
    list_teacher_classes,
    require_active_class,
    require_student_assignment,
    require_teacher_class,
    student_work_scope,
    workspace_roles,
)
from app.services.invite import InviteService
from app.services.notification import NotificationService
from app.services.progress import ProgressService
from app.services.submission import SubmissionService
from app.services.telegram_link import TelegramLinkService
from app.services.workload import pending_reviews
from app.status_labels import reminder_status, student_status
from app.telegram.client import TelegramClient
from app.telegram.files import TelegramAttachment
from app.telegram.router import TelegramIntentRouter

logger = logging.getLogger(__name__)
CONNECT_HELP = (
    "Welcome to Class Relay.\n\n"
    "To receive school notifications, use the secure Telegram connection "
    "link provided through Class Relay or your teacher."
)
MENU_COMMANDS = {
    "assign", "status", "classes", "reminders", "feedback", "reviews", "schools",
    "student_classes", "student_reminders", "menu", "cancel", "join", "teacher_help",
    "student_help", "teachers", "students",
}
NUMBERED_COMMANDS = {
    "ack", "blocked", "submit", "assignment", "assignments", "progress", "assign_to", "ask",
}
WORK_CALLBACKS = {"choose_work", "work_page", "cancel_work"}


class TelegramService:
    def __init__(self, llm: LLMService, client: TelegramClient):
        self.llm = llm
        self.client = client
        self.notifications = NotificationService(client)
        self.progress = ProgressService(self.notifications)
        self.submissions = SubmissionService(self.notifications)
        self.invites = InviteService()
        self.links = TelegramLinkService(client.settings)
        self.intent_router = TelegramIntentRouter(llm, client, self.notifications)
        self.assignments = self.intent_router.teacher.assignments

    def _send_menu(
        self, db: Session, user: User | None, chat_id: str, *, prefix: str = "",
        kind: str = "help", idempotency_key: str | None = None,
    ) -> None:
        keyboard = []
        if user:
            roles = workspace_roles(db, user)
            classes = list_teacher_classes(db, user) if "teacher" in roles else []
            lines = [f"Class Relay is connected as {user.name}."]
            if "student" in roles:
                lines.append(
                    "Student · Open an assignment to read instructions, update progress, "
                    "ask for help, or turn in work."
                )
                keyboard.extend([
                    [
                        {"text": "My assignments", "callback_data": "assignments:1"},
                        {"text": "Feedback", "callback_data": "feedback"},
                    ],
                    [
                        {"text": "My classes", "callback_data": "student_classes"},
                        {"text": "My reminders", "callback_data": "student_reminders"},
                    ],
                    [
                        {"text": "Join a class", "callback_data": "join"},
                        {"text": "Student help", "callback_data": "student_help"},
                    ],
                ])
            if classes:
                lines.append(
                    "Teacher · Create work, monitor your classes, and see submissions awaiting review."
                )
                keyboard.extend([
                    [
                        {"text": "Create assignment", "callback_data": "assign"},
                        {"text": "Class progress", "callback_data": "status"},
                    ],
                    [
                        {"text": "Teaching classes", "callback_data": "classes"},
                        {"text": "Teaching reminders", "callback_data": "reminders"},
                    ],
                    [{"text": "Teacher help", "callback_data": "teacher_help"}],
                ])
            if "coordinator" in roles:
                lines.append("Coordinator · See your schools and review queue. Manage rosters and staffing on the website.")
                keyboard.extend([
                    [
                        {"text": "My schools", "callback_data": "schools"},
                        {"text": "Teachers", "callback_data": "teachers"},
                        {"text": "Students", "callback_data": "students"},
                    ],
                ])
            if classes or "coordinator" in roles:
                keyboard.append([{"text": "Review queue", "callback_data": "reviews"}])
            if "teacher" in roles and not classes:
                lines.append("You can create assignments once you have an active teaching class.")
            lines.append("Send /menu any time to return here.")
            keyboard.append([{"text": "Cancel pending action", "callback_data": "cancel"}])
            body = "\n\n".join(lines)
        else:
            body = CONNECT_HELP
        self.client.send(
            db, user, chat_id, prefix + body, kind=kind,
            reply_markup={"inline_keyboard": keyboard} if keyboard else None,
            idempotency_key=idempotency_key,
        )

    def _send_staff_item(self, db: Session, user: User, chat_id: str, command: str) -> str:
        classes = list_teacher_classes(db, user)
        schools = coordinated_schools(db, user)
        if command == "/teacher_help" and classes:
            body = (
                "Teacher help\n\nTap Create assignment for a command using your class. "
                "You can also describe a change in chat, such as "
                "“Move the Weather Worksheet deadline to Friday at 6 pm” or "
                "“Cancel the Weather Worksheet.” Review and grade work in Class Relay."
            )
        elif command == "/reviews" and (classes or schools):
            class_ids = [item.id for item in classes]
            if schools:
                class_ids.extend(db.scalars(coordinator_class_scope(user.id)))
            rows = db.execute(pending_reviews(class_ids).limit(10)).all()
            body = "\n".join(
                f"#{submission.id} · {student.name[:65]} · {assignment.title[:90]}"
                for submission, assignment, student in rows
            )
            body = f"Awaiting review\n\n{body}\n\nOpen Class Relay to read or review the work." if body else (
                "There is no work awaiting review."
            )
        elif command == "/schools" and schools:
            body = "My schools\n\n" + "\n".join(school.name for school in schools[:15])
        elif command in {"/teachers", "/students"} and schools:
            role = "teacher" if command == "/teachers" else "student"
            counts = dict(db.execute(
                select(SchoolMembership.school_id, func.count(SchoolMembership.user_id))
                .where(
                    SchoolMembership.school_id.in_([school.id for school in schools]),
                    SchoolMembership.role == role,
                )
                .group_by(SchoolMembership.school_id)
            ).all())
            title = "Teachers" if role == "teacher" else "Students"
            body = f"{title} by school\n\n" + "\n".join(
                f"{school.name} · {counts.get(school.id, 0)}" for school in schools[:15]
            ) + "\n\nOpen the coordinator dashboard to see the directory and make changes."
        else:
            raise AuthorizationError("Your account does not have access to that staff view.")
        self.client.send(
            db, user, chat_id, body, kind="help",
            reply_markup={"inline_keyboard": [[{"text": "Main menu", "callback_data": "menu"}]]},
        )
        return command.removeprefix("/")

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
            message = {**callback_message, "from": callback.get("from"), "menu_callback": True}
            action, _, value = data.partition(":")
            if action in NUMBERED_COMMANDS or action in WORK_CALLBACKS or action in {"publish_draft", "cancel_draft"}:
                message["text"] = f"/{action} {value}"
            elif action in MENU_COMMANDS:
                message["text"] = f"/{action}"
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
                if user and not user.telegram_chat_id:
                    user.telegram_chat_id = chat_id
                self._send_menu(db, user, chat_id)
                return "start_linked" if user else "start_unlinked"
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
            self._send_menu(
                db, target, chat_id, prefix="✅ Telegram connected successfully.\n\n",
                kind="telegram_connected",
                idempotency_key=f"telegram-connected:{update_id}",
            )
            return f"telegram_link:connected:{target.id}"
        if command == "/join":
            if user and len(parts) == 1:
                if "student" not in workspace_roles(db, user):
                    raise InviteError("A student account is required to join a class.")
                self.client.send(
                    db, user, chat_id,
                    "Ask your teacher for a class code. Then send /join followed by the code. "
                    "For example, /join SCIENCE8.",
                    kind="help", reply_markup={"inline_keyboard": [[
                        {"text": "Main menu", "callback_data": "menu"}
                    ]]},
                )
                return "join_help"
            if not user or len(parts) not in {2, 3}:
                raise InviteError("Connect your student account using its private link, then send /join CODE.")
            if len(parts) == 3 and parts[2].casefold() != user.email.casefold():
                raise InviteError("The email must match your connected classroom account.")
            classroom = self.invites.join(db, user, parts[1], telegram_user_id, chat_id)
            self.client.send(db, user, chat_id, f"You are in {classroom.name}.", kind="joined")
            return f"joined:{classroom.id}"
        if not user:
            self._send_menu(db, None, chat_id)
            return "unlinked"
        if not user.telegram_chat_id:
            user.telegram_chat_id = chat_id
        if command.removeprefix("/") in WORK_CALLBACKS and message.get("menu_callback"):
            fields = parts[1].split(":") if len(parts) == 2 else []
            if len(fields) != 2 or any(
                not field.isascii() or not field.isdigit() or len(field) > 19 for field in fields
            ):
                raise ValidationError("Tap an assignment in the latest list.")
            return self.intent_router.student.choose_pending_work(
                db, user, chat_id, command[1:], int(fields[0]), int(fields[1]),
            )
        group = message.get("media_group_id")
        if isinstance(group, str) and group.isascii() and group.isdigit() and len(group) <= 32 and (
            message.get("photo") or message.get("document")
        ):
            claimed = db.execute(
                insert(IdempotencyKey)
                .values(key=f"telegram:album:{user.id}:{group}", operation="telegram_album")
                .on_conflict_do_nothing(index_elements=["key"])
            )
            if not claimed.rowcount:
                return "album_extra"
        if command in {"/publish_draft", "/cancel_draft"} and message.get("menu_callback"):
            if len(parts) != 2:
                raise ValidationError("Use the buttons on your latest assignment preview.")
            return self.intent_router.teacher.confirm_assignment(
                db, user, chat_id, parts[1], publish=command == "/publish_draft"
            )
        if command == "/cancel" and len(parts) == 1:
            self.intent_router.context.clear(db, user, chat_id)
            self.client.send(db, user, chat_id, "Cleared the pending chat request.", kind="help")
            return "context_cleared"
        if command in {"/menu", "/help"}:
            self._send_menu(db, user, chat_id)
            return "help"
        if command in {"/feedback", "/student_classes", "/student_reminders", "/student_help"}:
            return self.intent_router.student.send_workspace_item(db, user, chat_id, command)
        if command in {"/schools", "/teachers", "/students", "/reviews", "/teacher_help"}:
            return self._send_staff_item(db, user, chat_id, command)
        if command == "/assign_to" and message.get("menu_callback"):
            class_id = explicit_id(parts[1]) if len(parts) == 2 else None
            if not class_id:
                raise ValidationError("Open Create assignment and choose one of your classes.")
            classroom = require_teacher_class(db, user, class_id)
            self.intent_router.context.set(
                db, user, chat_id, pending_action="new_assignment", payload={"class_id": class_id},
            )
            self.client.send(
                db, user, chat_id,
                f"New assignment for {classroom.name}\n\n"
                "Send the instructions and the due date in one message. "
                "For example, “Read chapter 3 and answer questions 1–4 by tomorrow at 5 pm.” "
                "You’ll see a draft before publishing. Send /cancel to stop.",
                kind="help",
            )
            return "pending_assignment"
        if command == "/assignments":
            page = explicit_id(parts[1]) if len(parts) == 2 else 1
            if not page or len(parts) > 2:
                raise ValueError("Invalid page")
            return self.intent_router.student.send_assignments(db, user, chat_id, page)
        if command == "/assignment":
            assignment_id = explicit_id(parts[1]) if len(parts) == 2 else None
            if not assignment_id:
                self.client.send(
                    db, user, chat_id, "Send /assignments and tap an assignment to open it.",
                    kind="help",
                )
                return "assignment_help"
            return self.intent_router.student.send_assignment(db, user, chat_id, assignment_id)
        if command == "/ask" and message.get("menu_callback"):
            assignment_id = explicit_id(parts[1]) if len(parts) == 2 else None
            if not assignment_id:
                raise ValidationError("Open an assignment and choose Ask for help.")
            assignment = require_student_assignment(db, user, assignment_id)
            require_active_class(db.get(Classroom, assignment.classroom_id))
            state = get_student_state(db, assignment_id, user.id)
            if assignment.status != "assigned" or state.status in {"submitted", "completed", "cancelled", "needs_revision"}:
                raise ValidationError("This assignment is closed to help requests.")
            conversation = self.intent_router.context
            context = conversation.get(db, user, chat_id)
            if not (
                context and context.active_assignment_id == assignment_id
                and context.pending_action == "help_request"
                and (context.pending_payload_json or {}).get("event_id")
                and state.status in {"blocked", "overdue"}
            ):
                event = self.progress.update(
                    db, user, assignment_id, "blocked",
                    "The student requested help in Telegram. A note may follow.",
                )
                conversation.set(
                    db, user, chat_id, assignment_id=assignment_id,
                    pending_action="help_request", payload={"event_id": event.id},
                )
            self.client.send(
                db, user, chat_id,
                "Help request sent to your teacher. Reply here to add what you’re stuck on. "
                "Send /cancel to skip adding a note.",
                kind="help",
            )
            return f"progress:{assignment_id}:blocked"
        if command == "/assign":
            classes = list_teacher_classes(db, user)
            if not classes:
                student = "student" in workspace_roles(db, user)
                self._send_menu(
                    db, user, chat_id,
                    prefix=(
                        "Only teachers can assign work here. Tap My assignments to see yours.\n\n"
                        if student else "You need an active teaching class to assign work here.\n\n"
                    ),
                )
                return "error:AuthorizationError"
            if len(parts) < 3:
                class_list = "\n".join(f"{item.id} · {item.name}" for item in classes[:12])
                self.client.send(
                    db, user, chat_id,
                    f"Your teaching classes\n{class_list}\n\n"
                    "Enter the class number, what students should do, and when it is due.\n"
                    f"Example\n/assign {classes[0].id} Read chapter 3 and answer questions "
                    "1–4 by tomorrow at 5 pm.\n\n"
                    "This creates a preview. Send /classes for the full list.",
                    kind="help",
                    reply_markup={"inline_keyboard": [
                        [{"text": item.name[:60], "callback_data": f"assign_to:{item.id}"}]
                        for item in classes[:12]
                    ]},
                )
                return "assign_help"
            class_id = int(parts[1])
            natural_text = " ".join(parts[2:])
            classroom = require_teacher_class(db, user, class_id)
            return self.intent_router.teacher.preview_assignment(db, user, chat_id, classroom, natural_text)
        if command in {"/ack", "/progress", "/blocked"}:
            if len(parts) < 2:
                return "missing_assignment_id"
            assignment_id = int(parts[1])
            assignment = require_student_assignment(db, user, assignment_id)
            status = {"/ack": "acknowledged", "/progress": "in_progress", "/blocked": "blocked"}[
                command
            ]
            body = " ".join(parts[2:]) or status.replace("_", " ")
            percent = None
            if command == "/progress" and len(parts) == 2 and message.get("menu_callback"):
                require_active_class(db.get(Classroom, assignment.classroom_id))
                state = get_student_state(db, assignment_id, user.id)
                if assignment.status != "assigned" or state.status in {
                    "submitted", "completed", "cancelled",
                }:
                    raise ValidationError("This assignment is closed to progress updates.")
                self.intent_router.context.set(
                    db, user, chat_id, assignment_id=assignment_id, pending_action="progress"
                )
                self.client.send(
                    db, user, chat_id,
                    "Send your progress as a percentage (for example, 50%) or a short note. "
                    "Send /cancel to stop.",
                    kind="progress",
                )
                return f"pending_progress:{assignment_id}"
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
            assignment = require_student_assignment(db, user, assignment_id)
            attachment = TelegramAttachment.from_message(message)
            body = parts[2] if len(parts) > 2 else None
            if attachment:
                if not message.get("text"):
                    attachment = attachment.model_copy(update={"caption": body})
                return self.intent_router.student.submit_attachment(
                    db, user, chat_id, assignment_id, attachment, update_id,
                    grouped=bool(message.get("media_group_id")),
                )
            if not body:
                require_active_class(db.get(Classroom, assignment.classroom_id))
                state = get_student_state(db, assignment.id, user.id)
                if assignment.status != "assigned" or state.status in {
                    "submitted", "completed", "cancelled",
                }:
                    self.client.send(
                        db, user, chat_id,
                        "This assignment is closed to submissions. Send /assignments to see your work.",
                        kind="help",
                    )
                    return "submission_closed"
                self.intent_router.context.set(
                    db, user, chat_id, assignment_id=assignment_id,
                    pending_action="submission", payload={"accept_text": True},
                )
                self.client.send(
                    db, user, chat_id,
                    f"Send your answer or one attachment for {assignment.title}. "
                    "Your next message will be turned in. Send /cancel to stop.",
                    kind="submission_prompt",
                )
                return f"pending_submission:{assignment_id}"
            submission = self.submissions.submit(
                db, user, assignment_id, f"telegram:{update_id}:submission", text_content=body,
            )
            self.intent_router.context.set(db, user, chat_id, assignment_id=assignment_id)
            self.client.send(db, user, chat_id, f"Submission #{submission.id} received.", kind="submission")
            return f"submission:{submission.id}"
        if command in {"/classes", "/reminders", "/status"}:
            classes = list_teacher_classes(db, user)
            if not classes and command in {"/classes", "/reminders"}:
                student_command = "/student_classes" if command == "/classes" else "/student_reminders"
                return self.intent_router.student.send_workspace_item(db, user, chat_id, student_command)
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
            self._send_menu(db, user, chat_id, prefix="I did not recognize that command.\n\n")
            return "unknown"
        context = self.intent_router.context.get(db, user, chat_id)
        if context and context.pending_action in {"new_assignment", "assignment_preview"}:
            classroom = require_teacher_class(db, user, context.pending_payload_json["class_id"])
            if not text or TelegramAttachment.from_message(message):
                raise ValidationError("Send the assignment instructions and deadline as a message.")
            return self.intent_router.teacher.preview_assignment(db, user, chat_id, classroom, text)
        return self.intent_router.route(db, user, chat_id, text, message, update_id)
