from __future__ import annotations

import argparse
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx2 as httpx

from app.config import get_settings


def redact_webhook_url(value: str) -> str:
    if not value:
        return "Not configured"
    parsed = urlsplit(value)
    parts = parsed.path.rstrip("/").split("/")
    if parts:
        parts[-1] = "***"
    return urlunsplit((parsed.scheme, parsed.netloc, "/".join(parts), "", ""))


def safe_text(value: object, token: str, webhook_secret: str) -> str:
    text = str(value or "None")
    return text.replace(token, "[redacted]").replace(webhook_secret, "[redacted]")


def format_timestamp(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value), UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return "None"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Check the Telegram bot’s webhook registration.")
    parser.add_argument(
        "--clear-for-polling", action="store_true",
        help="Clear this Class Relay instance’s webhook and keep pending updates.",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if settings.telegram_mode != "real":
        raise SystemExit("TELEGRAM_MODE must be set to real before checking the webhook")
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    api_url = (
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/getWebhookInfo"
    )
    try:
        response = httpx.get(api_url, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SystemExit(f"Telegram webhook check failed ({type(exc).__name__})") from None
    if not payload.get("ok"):
        description = safe_text(
            payload.get("description"),
            settings.telegram_bot_token,
            settings.telegram_webhook_secret,
        )
        raise SystemExit(f"Telegram rejected webhook check: {description}")
    info = payload.get("result", {})
    if args.clear_for_polling:
        if settings.telegram_updates != "polling":
            raise SystemExit("Set TELEGRAM_UPDATES=polling before clearing the webhook")
        registered = info.get("url")
        expected = f"{settings.base_url.rstrip('/')}/telegram/webhook/{settings.telegram_webhook_secret}"
        if not registered:
            print("No webhook is registered. Telegram polling can start.")
            return
        if registered != expected:
            raise SystemExit("The registered webhook belongs to a different configuration. Check BASE_URL and TELEGRAM_WEBHOOK_SECRET.")
        try:
            response = httpx.post(
                api_url.removesuffix("getWebhookInfo") + "deleteWebhook",
                json={"drop_pending_updates": False},
                timeout=20,
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                raise ValueError("Telegram declined the request")
        except (httpx.HTTPError, ValueError) as exc:
            raise SystemExit(f"Could not clear the webhook ({type(exc).__name__})") from None
        print("Webhook cleared. Pending updates were retained.")
        return
    print("Telegram webhook status")
    print(f"Webhook URL: {redact_webhook_url(str(info.get('url', '')))}")
    print(f"Pending updates: {int(info.get('pending_update_count', 0) or 0)}")
    print(f"Last error date: {format_timestamp(info.get('last_error_date'))}")
    print(
        "Last error message: "
        + safe_text(
            info.get("last_error_message"),
            settings.telegram_bot_token,
            settings.telegram_webhook_secret,
        )
    )
    allowed = info.get("allowed_updates") or []
    print(f"Allowed updates: {', '.join(map(str, allowed)) or 'Telegram default'}")


if __name__ == "__main__":
    main()
