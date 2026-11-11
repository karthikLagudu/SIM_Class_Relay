from __future__ import annotations

from sqlalchemy.orm import Session

from app.llm.service import LLMService
from app.models import User
from app.services.authorization import list_teacher_classes
from app.services.notification import NotificationService
from app.telegram.client import TelegramClient
from app.telegram.conversation import ConversationService
from app.telegram.student import StudentConversation
from app.telegram.teacher import TeacherConversation


class TelegramIntentRouter:
    """Route private free-form messages to the appropriate conversation."""

    def __init__(self, llm: LLMService, client: TelegramClient, notifications: NotificationService):
        self.context = ConversationService(client.settings.conversation_context_minutes)
        self.teacher = TeacherConversation(llm, client, notifications, self.context)
        self.student = StudentConversation(llm, client, self.context)

    def route(
        self,
        db: Session,
        user: User,
        chat_id: str,
        text: str,
        message: dict,
        update_id: int,
    ) -> str:
        context = self.context.get(db, user, chat_id)
        pending_student = context and context.pending_action in {
            "submission",
            "file_assignment",
            "student_intent",
        }
        if not pending_student and not (message.get("document") or message.get("photo")):
            classes = list_teacher_classes(db, user)
            if classes:
                return self.teacher.route(db, user, chat_id, text, update_id, classes, context)
        return self.student.route(db, user, chat_id, text, message, update_id, context)
