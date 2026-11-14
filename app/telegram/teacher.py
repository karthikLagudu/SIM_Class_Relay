from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.exceptions import (
    AmbiguousReferenceError,
    LLMInterpretationError,
    NotFoundError,
    ValidationError,
)
from app.llm.schemas import TeacherIntent
from app.llm.service import LLMService
from app.models import Assignment, Classroom, ConversationContext, School, User
from app.services.assignment import AssignmentService
from app.services.assignment_draft import (
    class_reference_matches,
    class_students,
    resolve_natural_draft,
)
from app.services.assignment_reference import (
    AssignmentReferenceService,
    explicit_id,
    model_reference,
    normalize,
)
from app.services.authorization import require_teacher_class
from app.services.notification import NotificationService
from app.services.risk import RiskService
from app.telegram.client import TelegramClient
from app.telegram.conversation import ConversationService


class TeacherConversation:
    def __init__(
        self,
        llm: LLMService,
        client: TelegramClient,
        notifications: NotificationService,
        context: ConversationService,
    ):
        self.llm = llm
        self.client = client
        self.notifications = notifications
        self.context = context
        self.assignments = AssignmentService(notifications)
        self.references = AssignmentReferenceService()
        self.risks = RiskService()

    def class_options(self, classes: list[Classroom], reference: str | None) -> list[Classroom]:
        if reference is None:
            return classes
        if (number := explicit_id(reference)) is not None:
            return [item for item in classes if item.id == number]
        clean = normalize(reference)
        if not clean:
            return []
        exact = [item for item in classes if class_reference_matches(item, reference)]
        return exact or [
            item
            for item in classes
            if f" {clean} " in f" {normalize(item.grade)} {normalize(item.name)} "
            or clean == f"grade {normalize(item.grade).removeprefix('grade ')}"
        ]

    def preview_assignment(
        self, db: Session, user: User, chat_id: str, classroom: Classroom, text: str,
        *, now: datetime | None = None,
    ) -> str:
        classroom = require_teacher_class(db, user, classroom.id)
        school = db.get(School, classroom.school_id)
        now = now or datetime.now(UTC)
        self.context.set(
            db, user, chat_id, pending_action="new_assignment",
            payload={"class_id": classroom.id, "original": text},
        )
        parsed = self.llm.assignment(db, user.id, text, school.timezone, now)
        students = class_students(db, classroom.id)
        due_at, named_ids = resolve_natural_draft(parsed, text, classroom, school, students, None, now)
        ids = named_ids if named_ids is not None else [student.id for student in students]
        if not ids:
            raise ValidationError("Enroll a student before creating an assignment for this class.")
        recipients = (
            ", ".join(student.name for student in students if student.id in ids)
            if named_ids is not None else f"Entire class ({len(ids)} students)"
        )
        body = (
            f"Assignment draft\n{parsed.title}\n\n{parsed.instructions}\n\n"
            f"Class: {classroom.name}\nRecipients: {recipients}\n"
            f"Due: {self.notifications.format_deadline(due_at, school.timezone)}\n\n"
            "Tap Publish or Cancel, or send revised wording for a new preview."
        )
        if len(body.encode("utf-16-le")) > 8192:
            raise ValidationError("This draft is too long to review in Telegram. Shorten it or use the Class Relay assignment form.")
        nonce = secrets.token_urlsafe(16)
        self.context.set(
            db, user, chat_id, pending_action="assignment_preview",
            payload={
                "nonce": nonce, "class_id": classroom.id, "original": text, "title": parsed.title,
                "instructions": parsed.instructions, "due_at": due_at.isoformat(),
                "timezone": school.timezone, "student_ids": ids, "whole_class": named_ids is None,
            },
        )
        self.client.send(
            db, user, chat_id, body, kind="teacher_draft_preview",
            reply_markup={"inline_keyboard": [[
                {"text": "Publish", "callback_data": f"publish_draft:{nonce}"},
                {"text": "Cancel", "callback_data": f"cancel_draft:{nonce}"},
            ]]},
        )
        return "assignment_preview"

    def confirm_assignment(
        self, db: Session, user: User, chat_id: str, nonce: str, *, publish: bool,
    ) -> str:
        context = self.context.get(db, user, chat_id)
        plan = context.pending_payload_json if context and context.pending_action == "assignment_preview" else None
        if not plan or plan["nonce"] != nonce:
            raise ValidationError("This preview is no longer active. Use the latest preview or start a new draft.")
        if not publish:
            self.context.clear(db, user, chat_id)
            self.client.send(db, user, chat_id, "Draft cancelled.", kind="help")
            return "assignment_preview_cancelled"
        classroom = require_teacher_class(db, user, plan["class_id"])
        school = db.get(School, classroom.school_id)
        current_ids = {student.id for student in class_students(db, classroom.id)}
        reviewed_ids = set(plan["student_ids"])
        if (
            school.timezone != plan["timezone"]
            or not reviewed_ids.issubset(current_ids)
            or (plan["whole_class"] and current_ids != reviewed_ids)
        ):
            raise ValidationError("The recipients or school time zone changed. Send revised wording to review a fresh draft.")
        assignment = self.assignments.create(
            db, user, classroom.id, plan["title"], plan["instructions"],
            datetime.fromisoformat(plan["due_at"]), plan["timezone"],
            f"telegram-preview:{user.id}:{nonce}", plan["student_ids"], draft=True,
            original_request=plan["original"],
        )
        self.assignments.publish(db, user, assignment.id)
        self.context.set(db, user, chat_id, assignment_id=assignment.id)
        self.client.send(
            db, user, chat_id, f"Published #{assignment.id}: {assignment.title}.",
            kind="teacher_confirmation", school_id=school.id, classroom_id=classroom.id,
            assignment_id=assignment.id,
        )
        return f"assignment:{assignment.id}"

    def route(
        self,
        db: Session,
        user: User,
        chat_id: str,
        text: str,
        update_id: int,
        classes: list[Classroom],
        context: ConversationContext | None,
    ) -> str:
        classroom = None
        assignment = None
        if context and context.pending_action in {"teacher_class", "teacher_assignment"}:
            payload = context.pending_payload_json or {}
            chosen_ids = payload.get("choice_ids", [])
            if context.pending_action == "teacher_class":
                options = [item for item in classes if item.id in chosen_ids]
                number = explicit_id(text)
                matches = [
                    item
                    for item in options
                    if (
                        item.id == number
                        if number is not None
                        else normalize(item.name) == normalize(text)
                    )
                ]
                if len(matches) != 1:
                    raise AmbiguousReferenceError(
                        "Reply with one of these class IDs: "
                        + self._describe_classes(options)
                        + ". Use /cancel to clear the request."
                    )
                classroom = matches[0]
            else:
                options = [
                    item for item in self.references.for_teacher(db, user) if item.id in chosen_ids
                ]
                try:
                    assignment = self.references.choose(options, text, exact=True)
                except (AmbiguousReferenceError, NotFoundError) as exc:
                    raise AmbiguousReferenceError(
                        "Reply with one of these assignment IDs: "
                        + self.references.describe(options)
                        + ". Use /cancel to clear the request."
                    ) from exc
            parsed = TeacherIntent.model_validate(payload["intent"])
            text, update_id = payload["text"], payload["update_id"]
            now = datetime.fromisoformat(payload["received_at"])
            timezone_name = payload["timezone"]
        else:
            school = db.get(School, classes[0].school_id)
            now = datetime.now(UTC)
            timezone_name = school.timezone
            parsed = self.llm.teacher_intent(db, user.id, text, timezone_name, now)

        if parsed.intent in {"help", "unknown", "clarify"}:
            self.client.send(
                db,
                user,
                chat_id,
                parsed.clarification_question
                or "Tell me what to assign, change, cancel, or review.",
                kind="help",
            )
            return "teacher_help"
        pending = {
            "intent": parsed.model_dump(mode="json"),
            "text": text,
            "received_at": now.isoformat(),
            "timezone": timezone_name,
            "update_id": update_id,
        }
        if (
            classroom is None
            and assignment is None
            and (parsed.class_reference or parsed.intent == "create_assignment")
        ):
            class_reference = model_reference(parsed.class_reference, text)
            options = self.class_options(classes, class_reference)
            if not options:
                raise NotFoundError(
                    "No authorized class matches that description. Use /classes to see your classes."
                )
            if len(options) != 1:
                return self._hold_teacher(
                    db,
                    user,
                    chat_id,
                    "teacher_class",
                    [item.id for item in options],
                    pending,
                    "Which class do you mean? " + self._describe_classes(options),
                )
            classroom = options[0]

        if parsed.intent == "create_assignment":
            return self.preview_assignment(db, user, chat_id, classroom, text, now=now)
        if parsed.intent in {"risk_summary", "class_status"}:
            if context and context.pending_action == "teacher_class":
                self.context.clear(db, user, chat_id)
            return self.send_risk_summary(
                db, user, chat_id, classroom_id=classroom.id if classroom else None
            )

        if assignment is None:
            options = self.references.for_teacher(db, user, classroom.id if classroom else None)
            reference = model_reference(parsed.assignment_reference, text)
            if reference and normalize(reference) in {
                "it",
                "that",
                "this assignment",
                "the assignment",
                "assignment",
                "homework",
            }:
                reference = None
            today = bool(re.search(r"\btoday['’]s\b", text, re.IGNORECASE))
            if today:
                options = [item for item in options if self._created_today(item, now)]
                if reference:
                    reference = (
                        re.sub(r"\btoday['’]s\s*", "", reference, flags=re.IGNORECASE).strip()
                        or None
                    )
            if (
                not reference
                and context
                and not today
                and any(item.id == context.active_assignment_id for item in options)
            ):
                reference = f"#{context.active_assignment_id}"
            try:
                assignment = self.references.choose(options, reference)
            except (AmbiguousReferenceError, NotFoundError) as exc:
                if not options:
                    raise NotFoundError(
                        "No active assignment matches that request in your classes."
                    ) from exc
                return self._hold_teacher(
                    db,
                    user,
                    chat_id,
                    "teacher_assignment",
                    [item.id for item in options],
                    pending,
                    "Which assignment do you mean? " + self.references.describe(options),
                )
        if parsed.intent == "update_assignment_deadline":
            due_at = self._teacher_deadline(
                db, user, text, parsed, timezone_name, assignment.timezone, now
            )
            self.assignments.update_deadline(
                db, user, assignment.id, due_at, f"telegram:{update_id}:deadline"
            )
            due_label = self.notifications.format_deadline(assignment.due_at, assignment.timezone)
            response = f"Moved #{assignment.id} to {due_label}."
        elif parsed.intent == "update_assignment_instructions":
            if not parsed.instructions:
                raise LLMInterpretationError("What clarification should students receive?")
            self.assignments.clarify(
                db, user, assignment.id, parsed.instructions, f"telegram:{update_id}:instructions"
            )
            response = f"Clarified #{assignment.id}."
        else:
            self.assignments.cancel(db, user, assignment.id, f"telegram:{update_id}:cancel")
            response = f"Cancelled #{assignment.id}."
        self.context.set(db, user, chat_id, assignment_id=assignment.id)
        self.client.send(
            db,
            user,
            chat_id,
            response,
            kind="teacher_confirmation",
            school_id=assignment.school_id,
            classroom_id=assignment.classroom_id,
            assignment_id=assignment.id,
        )
        return f"{parsed.intent}:{assignment.id}"

    def _teacher_deadline(
        self,
        db: Session,
        user: User,
        text: str,
        parsed: TeacherIntent,
        parsed_timezone: str,
        target_timezone: str,
        received_at: datetime,
    ) -> datetime:
        due_at = parsed.due_at
        if target_timezone != parsed_timezone:
            corrected = self.llm.teacher_intent(db, user.id, text, target_timezone, received_at)
            due_at = corrected.due_at if corrected.intent == parsed.intent else None
        if due_at is None:
            raise LLMInterpretationError("Please give the deadline, such as Friday at 6 PM.")
        return due_at

    @staticmethod
    def _created_today(assignment: Assignment, now: datetime) -> bool:
        zone = ZoneInfo(assignment.timezone)
        created = assignment.created_at.replace(tzinfo=assignment.created_at.tzinfo or UTC)
        return created.astimezone(zone).date() == now.astimezone(zone).date()

    @staticmethod
    def _describe_classes(classes: list[Classroom]) -> str:
        return ", ".join(f"#{item.id} {item.name}" for item in classes[:5])

    def _hold_teacher(
        self,
        db: Session,
        user: User,
        chat_id: str,
        action: str,
        choices: list[int],
        payload: dict,
        prompt: str,
    ) -> str:
        self.context.set(
            db, user, chat_id, pending_action=action, payload={**payload, "choice_ids": choices}
        )
        self.client.send(
            db,
            user,
            chat_id,
            prompt + ". Reply with #ID or the full title, or use /cancel.",
            kind="teacher_clarification",
        )
        return "teacher_needs_context"

    def send_risk_summary(
        self, db: Session, user: User, chat_id: str, *, classroom_id: int | None = None
    ) -> str:
        risks = self.risks.for_actor(db, user, classroom_id=classroom_id)
        facts = [item.summary_fact() for item in risks[:25]]
        summary = self.llm.risk_summary(db, user.id, facts)
        self.client.send(db, user, chat_id, summary, kind="risk_summary")
        return "risk_summary"
