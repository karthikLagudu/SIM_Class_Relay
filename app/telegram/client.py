from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx2 as httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.exceptions import TelegramDeliveryError
from app.models import NotificationDelivery, User

logger = logging.getLogger(__name__)


def telegram_text_parts(body: str, limit: int = 4096) -> Iterator[str]:
    """Split plain text without losing characters, preferring line and word boundaries."""
    while body:
        encoded = body.encode("utf-16-le")
        if len(encoded) <= limit * 2:
            yield body
            return
        part = encoded[:limit * 2].decode("utf-16-le", errors="ignore")
        boundary = max(part.rfind("\n"), part.rfind(" "))
        if boundary >= len(part) // 2:
            part = part[: boundary + 1]
        yield part
        body = body[len(part):]


class TelegramClient:
    """Small Bot API adapter which always leaves durable delivery evidence."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.Client(timeout=httpx.Timeout(15, connect=5))

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.settings.telegram_bot_token}"

    def close(self) -> None:
        self.http.close()

    def _safe_error(self, exc: Exception) -> str:
        message = f"{type(exc).__name__}: {exc}"
        token = self.settings.telegram_bot_token
        if token:
            message = message.replace(token, "[redacted]")
        return message[:500]

    def send(
        self,
        db: Session,
        user: User | None,
        chat_id: str | None,
        body: str,
        kind: str = "message",
        reply_markup: dict | None = None,
        *,
        school_id: int | None = None,
        classroom_id: int | None = None,
        assignment_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> NotificationDelivery:
        if idempotency_key:
            existing = db.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.idempotency_key == idempotency_key
                )
            )
            if existing:
                return existing
        if len(body.encode("utf-16-le")) > 8192:
            body = body.encode("utf-16-le")[:8190].decode("utf-16-le", errors="ignore") + "…"
        target = chat_id or (user.telegram_chat_id if user else None)
        delivery = NotificationDelivery(
            user_id=user.id if user else None,
            school_id=school_id,
            classroom_id=classroom_id,
            assignment_id=assignment_id,
            chat_id=target,
            kind=kind,
            body=body,
            status="pending",
            idempotency_key=idempotency_key,
            reply_markup=reply_markup,
        )
        db.add(delivery)
        db.flush()
        if not target:
            delivery.status = "skipped"
            delivery.error = "User has not linked Telegram"
            return delivery
        if self.settings.telegram_mode == "log":
            logger.info(
                "telegram_delivery_logged school_id=%s classroom_id=%s assignment_id=%s user_id=%s kind=%s body=%r",
                school_id, classroom_id, assignment_id, delivery.user_id, kind,
                "[private teacher reply]" if kind == "student_support" else body,
            )
            delivery.status = "logged"
            delivery.attempt_count = 1
            delivery.last_attempt_at = datetime.now(UTC)
            return delivery
        return delivery

    def send_message(self, chat_id: str, body: str, reply_markup: dict | None) -> str:
        """Perform one Bot API attempt. The caller owns durable attempt state."""
        if self.settings.telegram_mode != "real" or not self.settings.telegram_bot_token:
            raise TelegramDeliveryError("Real Telegram delivery requires a configured bot token")
        try:
            payload: dict[str, object] = {"chat_id": chat_id, "text": body}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            response = self.http.post(f"{self.api_base}/sendMessage", json=payload)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise TelegramDeliveryError("Telegram rejected the message")
            return str(data["result"]["message_id"])
        except Exception as exc:
            raise TelegramDeliveryError(self._safe_error(exc)) from exc

    def send_document(
        self, chat_id: str, path: Path, filename: str, mime_type: str, caption: str,
    ) -> str:
        if self.settings.telegram_mode != "real" or not self.settings.telegram_bot_token:
            raise TelegramDeliveryError("Real Telegram delivery requires a configured bot token")
        try:
            root = self.settings.upload_dir.resolve()
            path = path.resolve()
            if root not in path.parents or not path.is_file():
                raise TelegramDeliveryError("The saved attachment is unavailable")
            with path.open("rb") as document:
                response = self.http.post(
                    f"{self.api_base}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"document": (filename, document, mime_type)},
                )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise TelegramDeliveryError("Telegram rejected the attachment")
            return str(data["result"]["message_id"])
        except TelegramDeliveryError:
            raise
        except OSError as exc:
            raise TelegramDeliveryError("The saved attachment could not be opened") from exc
        except Exception as exc:
            raise TelegramDeliveryError(self._safe_error(exc)) from exc

    def get_file_path(self, file_id: str) -> str:
        if self.settings.telegram_mode != "real" or not self.settings.telegram_bot_token:
            raise TelegramDeliveryError("Telegram file download requires TELEGRAM_MODE=real")
        try:
            response = self.http.get(f"{self.api_base}/getFile", params={"file_id": file_id})
            response.raise_for_status()
            payload = response.json()
            path = payload.get("result", {}).get("file_path")
            if (
                not payload.get("ok") or not isinstance(path, str)
                or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/"))
            ):
                raise TelegramDeliveryError("Telegram file is unavailable")
            return path
        except TelegramDeliveryError:
            raise
        except Exception as exc:
            raise TelegramDeliveryError("Telegram getFile failed") from exc

    def download_file(self, file_id: str, max_bytes: int) -> bytes:
        path = self.get_file_path(file_id)
        try:
            with self.http.stream(
                "GET",
                f"https://api.telegram.org/file/bot{self.settings.telegram_bot_token}/{quote(path, safe='/')}",
            ) as response:
                response.raise_for_status()
                expected = int(response.headers.get("content-length", "0") or 0)
                if expected > max_bytes:
                    raise TelegramDeliveryError("Telegram file exceeds upload size limit")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise TelegramDeliveryError("Telegram file exceeds upload size limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        except TelegramDeliveryError:
            raise
        except Exception as exc:
            raise TelegramDeliveryError("Telegram file download failed") from exc
