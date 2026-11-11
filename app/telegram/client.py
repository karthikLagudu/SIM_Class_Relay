from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.exceptions import TelegramDeliveryError
from app.models import Assignment, NotificationDelivery, Reminder, StudentAssignmentState, User
from app.reminders.policy import FINAL_STATES, decide_reminder, in_quiet_hours, next_permitted_time

logger = logging.getLogger(__name__)
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))


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
                school_id,
                classroom_id,
                assignment_id,
                delivery.user_id,
                kind,
                body,
            )
            delivery.status = "logged"
            delivery.attempt_count = 1
            delivery.last_attempt_at = datetime.now(UTC)
            return delivery
        self._attempt_send(delivery, reply_markup)
        return delivery

    def _attempt_send(
        self, delivery: NotificationDelivery, reply_markup: dict | None = None
    ) -> None:
        now = datetime.now(UTC)
        delivery.attempt_count += 1
        delivery.last_attempt_at = now
        if not self.settings.telegram_bot_token:
            delivery.status = "failed"
            delivery.error = "TELEGRAM_BOT_TOKEN is not configured"
            return
        try:
            payload: dict[str, object] = {"chat_id": delivery.chat_id, "text": delivery.body}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            response = self.http.post(f"{self.api_base}/sendMessage", json=payload)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise TelegramDeliveryError(
                    str(data.get("description", "Telegram rejected message"))
                )
            delivery.status = "sent"
            delivery.error = None
            delivery.next_attempt_at = None
            delivery.external_message_id = str(data["result"]["message_id"])
        except Exception as exc:  # noqa: BLE001 - delivery boundary must preserve assignment data
            delivery.status = "failed"
            delivery.error = self._safe_error(exc)
            if delivery.attempt_count < self.settings.notification_max_attempts:
                delay = RETRY_DELAYS[min(delivery.attempt_count - 1, len(RETRY_DELAYS) - 1)]
                delivery.next_attempt_at = now + delay
            logger.warning(
                "notification_failed delivery_id=%s attempt=%s error_type=%s",
                delivery.id,
                delivery.attempt_count,
                type(exc).__name__,
            )

    def retry_due(self, db: Session, now: datetime | None = None) -> list[NotificationDelivery]:
        if self.settings.telegram_mode != "real":
            return []
        now = now or datetime.now(UTC)
        rows = list(
            db.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.status == "failed",
                    NotificationDelivery.next_attempt_at <= now,
                    NotificationDelivery.attempt_count < self.settings.notification_max_attempts,
                )
            )
        )
        for delivery in rows:
            assignment = db.get(Assignment, delivery.assignment_id) if delivery.assignment_id else None
            user = db.get(User, delivery.user_id) if delivery.user_id else None
            key = delivery.idempotency_key or ""
            reminder = None
            if key.startswith("reminder:"):
                reminder = db.scalar(select(Reminder).where(Reminder.dedupe_key == key.removeprefix("reminder:")))
            stale = user is not None and delivery.chat_id != user.telegram_chat_id
            if assignment:
                stale = stale or (assignment.status == "cancelled" and delivery.kind != "assignment_cancelled")
                if delivery.kind == "deadline_updated":
                    stale = stale or not key.startswith(f"deadline:{assignment.id}:v{assignment.schedule_version}:")
            if reminder and assignment:
                state = db.scalar(select(StudentAssignmentState).where(
                    StudentAssignmentState.assignment_id == assignment.id,
                    StudentAssignmentState.student_id == reminder.student_id,
                ))
                stale = stale or reminder.schedule_version != assignment.schedule_version or not state or state.status in FINAL_STATES
                if not stale and state:
                    stale = decide_reminder(state, assignment, now).reminder_type != reminder.reminder_type
                local_now = now.astimezone(ZoneInfo(assignment.timezone))
                start, end = self.settings.quiet_hour_start, self.settings.quiet_hour_end
                if not stale and in_quiet_hours(local_now, start, end):
                    delivery.next_attempt_at = next_permitted_time(now, assignment.timezone, start, end)
                    continue
            if stale:
                delivery.status = "skipped"
                delivery.error = "Recipient or assignment context has changed"
                delivery.next_attempt_at = None
            else:
                self._attempt_send(delivery)
            if reminder:
                reminder.status = delivery.status
                reminder.sent_at = now if delivery.status == "sent" else None
        return rows

    def get_file_path(self, file_id: str) -> str:
        if self.settings.telegram_mode != "real" or not self.settings.telegram_bot_token:
            raise TelegramDeliveryError("Telegram file download requires TELEGRAM_MODE=real")
        try:
            response = self.http.get(f"{self.api_base}/getFile", params={"file_id": file_id})
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok") or not payload.get("result", {}).get("file_path"):
                raise TelegramDeliveryError("Telegram file is unavailable")
            return str(payload["result"]["file_path"])
        except TelegramDeliveryError:
            raise
        except Exception as exc:
            raise TelegramDeliveryError("Telegram getFile failed") from exc

    def download_file(self, file_id: str, max_bytes: int) -> bytes:
        path = self.get_file_path(file_id)
        try:
            with self.http.stream(
                "GET",
                f"https://api.telegram.org/file/bot{self.settings.telegram_bot_token}/{path}",
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
