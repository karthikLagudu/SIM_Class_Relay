from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import AmbiguousReferenceError, LLMInterpretationError, NotFoundError
from app.llm.schemas import StudentIntent
from app.llm.service import LLMService
from app.models import Assignment, ConversationContext, NotificationDelivery, User
from app.services.assignment_reference import AssignmentReferenceService, model_reference
from app.services.notification import NotificationService
from app.services.progress import ProgressService
from app.services.submission import SubmissionService
from app.telegram.client import TelegramClient
from app.telegram.conversation import ConversationService
from app.telegram.files import TelegramAttachment, TelegramFileService


class StudentConversation:
    def __init__(self, llm: LLMService, client: TelegramClient, context: ConversationService):
        self.llm = llm
        self.client = client
        self.context = context
        self.progress = ProgressService()
        self.submissions = SubmissionService()
        self.references = AssignmentReferenceService()
        self.files = TelegramFileService(client.settings, client)

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
