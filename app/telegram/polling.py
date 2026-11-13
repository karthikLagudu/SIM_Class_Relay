import asyncio
import logging

import httpx
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


async def poll_telegram(app) -> None:
    """Pull updates directly from Telegram without a public inbound URL."""
    api = app.state.telegram_client.api_base
    offset = None
    async with httpx.AsyncClient(timeout=20) as http:
        while True:
            try:
                response = await http.post(
                    f"{api}/getUpdates",
                    json={"offset": offset, "timeout": 12, "allowed_updates": ["message", "callback_query"]},
                )
                response.raise_for_status()
                updates = response.json()["result"]
                if not updates:
                    offset = None
                for update in updates:
                    processing = asyncio.create_task(asyncio.to_thread(process_update, app, update))
                    try:
                        result = await asyncio.shield(processing)
                    except asyncio.CancelledError:
                        await asyncio.gather(processing, return_exceptions=True)
                        raise
                    offset = update["update_id"] + 1
                    callback = callback_to_answer(update, result)
                    if callback:
                        try:
                            await http.post(f"{api}/answerCallbackQuery", json={
                                "callback_query_id": callback,
                            })
                        except httpx.HTTPError:
                            logger.info("telegram_poll_callback_ack_failed")
            except (httpx.HTTPError, SQLAlchemyError, KeyError, ValueError, TypeError) as exc:
                logger.warning("telegram_poll_failed error_type=%s", type(exc).__name__)
                await asyncio.sleep(3)


def process_update(app, update: dict) -> dict:
    with app.state.session_factory.begin() as db:
        return app.state.telegram_service.process(db, update)


def callback_to_answer(update: dict, result: dict) -> str | None:
    callback = update.get("callback_query")
    callback_id = callback.get("id") if isinstance(callback, dict) else None
    if (
        isinstance(callback_id, str) and 0 < len(callback_id) <= 256
        and not str(result.get("result", "")).startswith("ignored")
    ):
        return callback_id
    return None
