from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import (
    AmbiguousReferenceError,
    AuthorizationError,
    LLMInterpretationError,
    NotFoundError,
)
from app.llm.schemas import StudentIntent
from app.llm.service import LLMService
from app.models import (
    Assignment,
    Classroom,
    ConversationContext,
    Feedback,
    NotificationDelivery,
    Reminder,
    StudentAssignmentState,
    User,
)
from app.services.assignment import get_student_state
from app.services.assignment_reference import AssignmentReferenceService, model_reference
from app.services.authorization import (
    class_membership_scope,
    require_student_assignment,
    student_work_scope,
    workspace_roles,
)
from app.services.notification import NotificationService
from app.services.progress import ProgressService
from app.services.submission import SubmissionService
from app.status_labels import student_status
from app.telegram.client import TelegramClient
from app.telegram.conversation import ConversationService
from app.telegram.files import TelegramAttachment, TelegramFileService

ASSIGNMENTS_PER_PAGE = 8
MAIN_MENU = {"text": "Main menu", "callback_data": "menu"}


class StudentConversation:
    def __init__(
        self, llm: LLMService, client: TelegramClient, notifications: NotificationService,
        context: ConversationService,
    ):
        self.llm = llm
        self.client = client
        self.notifications = notifications
        self.context = context
        self.progress = ProgressService(notifications)
        self.submissions = SubmissionService(notifications)
        self.references = AssignmentReferenceService()
        self.files = TelegramFileService(client.settings, client, self.submissions)

    def send_workspace_item(self, db: Session, user: User, chat_id: str, command: str) -> str:
        if "student" not in workspace_roles(db, user):
            raise AuthorizationError("A student account is required to open this view.")
        if command == "/student_classes":
            names = db.scalars(
                select(Classroom.name)
                .where(
                    Classroom.id.in_(class_membership_scope(user.id, "student")),
                    Classroom.archived_at.is_(None),
                )
                .order_by(Classroom.name)
                .limit(20)
            )
            body = "My classes\n\n" + ("\n".join(names) or "You have no active classes yet.")
        elif command == "/feedback":
            rows = db.execute(
                select(Feedback, Assignment, StudentAssignmentState.status)
                .join(Assignment, Assignment.id == Feedback.assignment_id)
                .join(StudentAssignmentState, StudentAssignmentState.assignment_id == Assignment.id)
                .where(
                    Feedback.student_id == user.id,
                    StudentAssignmentState.student_id == user.id,
                    Assignment.id.in_(student_work_scope(user.id)),
                )
                .order_by(Feedback.id.desc())
                .limit(5)
            ).all()
            body = "Latest feedback\n\n" + (
                "\n\n".join(
                    f"{assignment.title[:90]} · {student_status(status)}\n{feedback.message[:250]}"
                    for feedback, assignment, status in rows
                ) or "Your teacher’s feedback will appear here."
            )
        elif command == "/student_reminders":
            rows = db.execute(
                select(Assignment, Reminder)
                .join(Assignment, Assignment.id == Reminder.assignment_id)
                .join(StudentAssignmentState, StudentAssignmentState.assignment_id == Assignment.id)
                .join(Classroom, Classroom.id == Assignment.classroom_id)
                .where(
                    Reminder.student_id == user.id,
                    StudentAssignmentState.student_id == user.id,
                    StudentAssignmentState.status.not_in({"submitted", "completed", "cancelled"}),
                    Reminder.status.in_({"pending", "deferred"}),
                    Assignment.status == "assigned",
                    Classroom.archived_at.is_(None),
                    Assignment.id.in_(student_work_scope(user.id)),
                )
                .order_by(Reminder.scheduled_for)
                .limit(8)
            ).all()
            body = "Scheduled reminders\n\n" + (
                "\n".join(
                    f"{assignment.title[:90]} · "
                    f"{self.notifications.format_deadline(reminder.scheduled_for, assignment.timezone)}"
                    for assignment, reminder in rows
                ) or "No upcoming reminders."
            )
        else:
            body = (
                "Student help\n\nOpen My assignments and choose your work. Tap Mark as read, "
                "Update progress, Ask for help, or Turn in work. You can send written answers, "
                "photos, or files. Feedback shows the latest teacher notes. "
                "Use /cancel to stop a pending action."
            )
        self.client.send(
            db, user, chat_id, body, kind="help", reply_markup={"inline_keyboard": [[MAIN_MENU]]},
        )
        return command.removeprefix("/")

    def send_assignments(self, db: Session, user: User, chat_id: str, page: int = 1) -> str:
        rows = db.execute(
            select(Assignment, Classroom.name, StudentAssignmentState.status)
            .join(Classroom, Classroom.id == Assignment.classroom_id)
            .join(StudentAssignmentState, StudentAssignmentState.assignment_id == Assignment.id)
            .where(
                Assignment.id.in_(student_work_scope(user.id)),
                Assignment.status == "assigned",
                Classroom.archived_at.is_(None),
                StudentAssignmentState.student_id == user.id,
            )
            .order_by(Assignment.due_at, Assignment.id)
            .offset((page - 1) * ASSIGNMENTS_PER_PAGE)
            .limit(ASSIGNMENTS_PER_PAGE + 1)
        ).all()
        if not rows:
            body = (
                "No assignments yet. Published work for your classes will show up here."
                if page == 1
                else "There are no more assignments on this page. Send /assignments to start over."
            )
            self.client.send(
                db, user, chat_id, body, kind="status",
                reply_markup={"inline_keyboard": [[MAIN_MENU]]},
            )
            return "assignments"
        lines = ["📚 My assignments", "Tap an assignment below to see the instructions."]
        keyboard = []
        for assignment, class_name, status in rows[:ASSIGNMENTS_PER_PAGE]:
            deadline = NotificationService.format_deadline(assignment.due_at, assignment.timezone)
            lines.append(
                f"#{assignment.id} {assignment.title[:75]}\n"
                f"{class_name[:75]} · {student_status(status)}\nDue {deadline}"
            )
            keyboard.append([{
                "text": f"#{assignment.id} {assignment.title[:45]}",
                "callback_data": f"assignment:{assignment.id}",
            }])
        navigation = []
        if page > 1:
            navigation.append({"text": "Previous", "callback_data": f"assignments:{page - 1}"})
        if len(rows) > ASSIGNMENTS_PER_PAGE:
            navigation.append({"text": "Next", "callback_data": f"assignments:{page + 1}"})
        if navigation:
            keyboard.append(navigation)
        keyboard.append([MAIN_MENU])
        self.client.send(
            db, user, chat_id, "\n\n".join(lines), kind="status",
            reply_markup={"inline_keyboard": keyboard},
        )
        return "assignments"

    def send_assignment(self, db: Session, user: User, chat_id: str, assignment_id: int) -> str:
        assignment = require_student_assignment(db, user, assignment_id)
        classroom = db.get(Classroom, assignment.classroom_id)
        if assignment.status != "assigned" or classroom.archived_at:
            raise NotFoundError("This assignment is closed. Send /assignments to see available work.")
        state = get_student_state(db, assignment_id, user.id)
        body = (
            f"{self.notifications.assignment_message(assignment, classroom)}"
            f"\n\nYour status · {student_status(state.status)}"
        )
        self.client.send(
            db, user, chat_id, body, kind="assignment_details",
            reply_markup=self.notifications.assignment_actions(assignment_id, state.status),
            school_id=assignment.school_id,
            classroom_id=assignment.classroom_id,
            assignment_id=assignment.id,
        )
        return f"assignment_details:{assignment_id}"

    def _reply_assignment(self, db: Session, user: User, chat_id: str, message: dict) -> int | None:
        reply = message.get("reply_to_message")
        if not isinstance(reply, dict) or type(reply.get("message_id")) is not int:
            return None
        return db.scalar(
            select(NotificationDelivery.assignment_id).where(
                NotificationDelivery.chat_id == chat_id,
                NotificationDelivery.user_id == user.id,
                NotificationDelivery.external_message_id == str(reply["message_id"]),
                NotificationDelivery.status == "sent",
                NotificationDelivery.assignment_id.is_not(None),
            )
        )

    def _ask_student_assignment(
        self,
        db: Session,
        user: User,
        chat_id: str,
        choices: list[Assignment],
        *,
        action: str,
        payload: dict,
    ) -> str:
        if not choices:
            raise NotFoundError("You have no active assignments for this action.")
        self.context.set(
            db,
            user,
            chat_id,
            pending_action=action,
            payload={**payload, "choice_ids": [item.id for item in choices]},
        )
        self.client.send(
            db,
            user,
            chat_id,
            f"Which assignment is this for? {self.references.describe(choices)}. "
            "Reply with #ID or the full title. Use /cancel to clear this request.",
            kind="clarification",
        )
        return "file_needs_context" if action == "file_assignment" else "student_needs_context"

    def route(
        self,
        db: Session,
        user: User,
        chat_id: str,
        text: str,
        message: dict,
        update_id: int,
        context: ConversationContext | None,
    ) -> str:
        assignments = self.references.for_student(db, user)
        reply_id = self._reply_assignment(db, user, chat_id, message)
        attachment = TelegramAttachment.from_message(message)
        if attachment:
            if context and context.pending_action == "file_assignment":
                raise AmbiguousReferenceError(
                    "I am already holding an attachment. Reply with the assignment #ID or use /cancel before sending another."
                )
            bound_id = reply_id or (
                context.active_assignment_id
                if context and context.pending_action == "submission"
                else None
            )
            payload = {"attachment": attachment.model_dump(mode="json"), "update_id": update_id}
            if bound_id is None:
                return self._ask_student_assignment(
                    db,
                    user,
                    chat_id,
                    assignments,
                    action="file_assignment",
                    payload=payload,
                )
            assignment = self.references.choose(assignments, f"#{bound_id}", exact=True)
            self.context.set(
                db,
                user,
                chat_id,
                pending_action="file_assignment",
                payload={**payload, "choice_ids": [assignment.id]},
            )
            return self.submit_attachment(db, user, chat_id, assignment.id, attachment, update_id)

        if context and context.pending_action in {"file_assignment", "student_intent"}:
            return self._resume_student(db, user, chat_id, text, context, assignments)
        if context and context.pending_action in {"progress", "help_request"}:
            assignment = self.references.choose(
                assignments, f"#{context.active_assignment_id}", exact=True
            )
            percent_text = text.removesuffix("%").strip()
            help_request = context.pending_action == "help_request"
            percent = None
            if not help_request and percent_text.isascii() and percent_text.isdigit():
                if len(percent_text) > 3 or int(percent_text) > 100:
                    raise LLMInterpretationError("Use a percentage between 0 and 100.")
                percent = int(percent_text)
            if not text or len(text) > 2000:
                raise LLMInterpretationError("Send a short note so your teacher can see the update.")
            status = "blocked" if help_request else "in_progress"
            self.progress.update(db, user, assignment.id, status, text, percent)
            self.context.set(db, user, chat_id, assignment_id=assignment.id)
            reply = "Help requested. Your teacher can see your note." if help_request else "Progress saved."
            self.client.send(db, user, chat_id, reply, kind="progress")
            return f"progress:{assignment.id}:{status}"
        if (
            context and context.pending_action == "submission"
            and (context.pending_payload_json or {}).get("accept_text") and text
        ):
            assignment = self.references.choose(
                assignments, f"#{context.active_assignment_id}", exact=True
            )
            submission = self.submissions.submit(
                db, user, assignment.id, f"telegram:{update_id}:submission", text_content=text
            )
            self.context.set(db, user, chat_id, assignment_id=assignment.id)
            self.client.send(
                db, user, chat_id, f"Submission #{submission.id} received.", kind="submission"
            )
            return f"submission:{submission.id}"

        parsed = self.llm.student_intent(db, user.id, text)
        if parsed.intent == "status":
            summary = (
                "\n".join(
                    f"#{item.id} {item.title}: due {NotificationService.format_deadline(item.due_at, item.timezone)}"
                    for item in assignments
                )
                or "No active assignments."
            )
            self.client.send(db, user, chat_id, summary, kind="status")
            return "status"
        if parsed.intent in {"unknown", "clarify"}:
            raise LLMInterpretationError(
                parsed.clarification_question
                or "Are you sharing progress, asking for help, or submitting?"
            )
        if parsed.intent == "submit_text" and (
            not parsed.message.strip() or " ".join(parsed.message.split()) not in " ".join(text.split())
        ):
            raise LLMInterpretationError("Include your answer or use /submit ID followed by the work.")
        reference = model_reference(parsed.assignment_reference, text)
        if not reference:
            focus_id = reply_id or (context.active_assignment_id if context else None)
            if focus_id is not None and any(item.id == focus_id for item in assignments):
                reference = f"#{focus_id}"
        try:
            assignment = self.references.choose(assignments, reference)
        except (AmbiguousReferenceError, NotFoundError):
            return self._ask_student_assignment(
                db,
                user,
                chat_id,
                assignments,
                action="student_intent",
                payload={"intent": parsed.model_dump(mode="json"), "update_id": update_id},
            )
        return self._apply_student(db, user, chat_id, assignment, parsed, update_id)

    def _resume_student(
        self,
        db: Session,
        user: User,
        chat_id: str,
        text: str,
        context: ConversationContext,
        assignments: list[Assignment],
    ) -> str:
        payload = context.pending_payload_json or {}
        choices = [item for item in assignments if item.id in payload.get("choice_ids", [])]
        if not choices:
            self.context.clear(db, user, chat_id)
            raise NotFoundError("The assignments for that request are no longer available.")
        try:
            assignment = self.references.choose(choices, text, exact=True)
        except (AmbiguousReferenceError, NotFoundError) as exc:
            raise AmbiguousReferenceError(
                f"Reply with one of these assignment IDs: {self.references.describe(choices)}. Use /cancel to clear the request."
            ) from exc
        update_id = payload["update_id"]
        if context.pending_action == "file_assignment":
            attachment = TelegramAttachment.model_validate(payload["attachment"])
            return self.submit_attachment(db, user, chat_id, assignment.id, attachment, update_id)
        parsed = StudentIntent.model_validate(payload["intent"])
        return self._apply_student(db, user, chat_id, assignment, parsed, update_id)

    def submit_attachment(
        self,
        db: Session,
        user: User,
        chat_id: str,
        assignment_id: int,
        attachment: TelegramAttachment,
        update_id: int,
    ) -> str:
        submission = self.files.submit(db, user, assignment_id, attachment, update_id)
        self.context.set(db, user, chat_id, assignment_id=assignment_id)
        self.client.send(
            db, user, chat_id, f"Submission #{submission.id} received.", kind="submission"
        )
        return f"submission:{submission.id}"

    def _apply_student(
        self,
        db: Session,
        user: User,
        chat_id: str,
        assignment: Assignment,
        parsed: StudentIntent,
        update_id: int,
    ) -> str:
        if parsed.intent == "submit_attachment":
            self.context.set(
                db, user, chat_id, assignment_id=assignment.id, pending_action="submission"
            )
            self.client.send(
                db, user, chat_id, "Send the file or photo next.", kind="submission_prompt"
            )
            return f"pending_submission:{assignment.id}"
        if parsed.intent == "submit_text":
            submission = self.submissions.submit(
                db,
                user,
                assignment.id,
                f"telegram:{update_id}:submission",
                text_content=parsed.message,
            )
            self.client.send(
                db, user, chat_id, f"Submission #{submission.id} received.", kind="submission"
            )
            result = f"submission:{submission.id}"
        else:
            status = {
                "acknowledge": "acknowledged",
                "progress_update": "in_progress",
                "blocked": "blocked",
                "ask_for_help": "blocked",
            }[parsed.intent]
            self.progress.update(
                db,
                user,
                assignment.id,
                status,
                parsed.block_reason or parsed.message or status,
                parsed.progress_percent,
            )
            self.client.send(db, user, chat_id, "Update saved.", kind="progress")
            result = f"progress:{assignment.id}:{status}"
        self.context.set(db, user, chat_id, assignment_id=assignment.id)
        return result
