from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic import ValidationError as SchemaError
from sqlalchemy.orm import Session

from app.config import Settings
from app.exceptions import TelegramDeliveryError, ValidationError
from app.models import Submission, User
from app.services.assignment import get_student_state
from app.services.authorization import require_student_assignment
from app.services.submission import SubmissionService
from app.telegram.client import TelegramClient


class TelegramAttachment(BaseModel):
    file_id: str = Field(min_length=1, max_length=1024)
    filename: str | None = Field(default=None, max_length=1024)
    mime_type: str | None = Field(default=None, max_length=255)
    file_size: int | None = Field(default=None, ge=0)
    caption: str | None = Field(default=None, max_length=10000)

    @classmethod
    def from_message(cls, message: dict) -> TelegramAttachment | None:
        document = message.get("document")
        photos = message.get("photo") or []
        if not document and not photos:
            return None
        item = document or photos[-1]
        try:
            return cls(
                file_id=item["file_id"],
                filename=item.get("file_name") if document else "telegram-photo.jpg",
                mime_type=item.get("mime_type") if document else "image/jpeg",
                file_size=item.get("file_size"),
                caption=message.get("caption"),
            )
        except SchemaError as exc:
            raise ValidationError("The attachment details are incomplete or invalid") from exc


@dataclass(frozen=True)
class StoredTelegramFile:
    path: str
    original_filename: str
    mime_type: str
    content: bytes


class TelegramFileService:
    def __init__(self, settings: Settings, client: TelegramClient, submissions: SubmissionService):
        self.settings = settings
        self.client = client
        self.submissions = submissions

    def download(
        self, file_id: str, original_filename: str | None, mime_type: str | None
    ) -> StoredTelegramFile:
        content = self.client.download_file(file_id, self.settings.max_upload_bytes)
        if not content:
            raise TelegramDeliveryError("Telegram returned an empty file")
        original = (original_filename or "telegram-upload").replace("\\", "/").rsplit("/", 1)[-1]
        original = (
            "".join(char for char in original if char.isprintable())[:240] or "telegram-upload"
        )
        root = self.settings.upload_dir.resolve()
        destination = root / uuid.uuid4().hex
        try:
            root.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                output.write(content)
        except OSError as exc:
            raise TelegramDeliveryError(
                "The attachment could not be stored. Please try again."
            ) from exc
        detected = mime_type or mimetypes.guess_type(original)[0] or "application/octet-stream"
        return StoredTelegramFile(str(destination), original, detected, content)

    def submit(
        self,
        db: Session,
        user: User,
        assignment_id: int,
        attachment: TelegramAttachment,
        update_id: int,
    ) -> Submission:
        require_student_assignment(db, user, assignment_id)
        state = get_student_state(db, assignment_id, user.id)
        if state.status in {"submitted", "completed", "cancelled"}:
            raise ValidationError(
                "This assignment is no longer accepting a submission. A teacher can request revision."
            )
        if (
            attachment.file_size is not None
            and attachment.file_size > self.settings.max_upload_bytes
        ):
            raise ValidationError("The attachment exceeds the upload size limit")
        stored = self.download(attachment.file_id, attachment.filename, attachment.mime_type)
        try:
            return self.submissions.submit(
                db,
                user,
                assignment_id,
                f"telegram:{update_id}:submission",
                text_content=attachment.caption,
                telegram_file_id=attachment.file_id,
                stored_file_path=stored.path,
                original_filename=stored.original_filename,
                mime_type=stored.mime_type,
                content=stored.content,
            )
        except Exception:
            Path(stored.path).unlink(missing_ok=True)
            raise
