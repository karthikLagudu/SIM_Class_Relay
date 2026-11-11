from __future__ import annotations

import asyncio
import logging
import time

from app.database import begin_sqlite_write
from app.services.delivery import DeliveryProcessor

logger = logging.getLogger(__name__)


def run_cycle(app, *, schedule: bool) -> tuple[int, int]:
    jobs = 0
    if schedule:
        try:
            with app.state.session_factory.begin() as db:
                begin_sqlite_write(db)
                jobs = len(app.state.reminder_service.run(db))
        except Exception:
            logger.exception("reminder_worker_cycle_failed")
    delivered = DeliveryProcessor(app.state.telegram_client, app.state.session_factory).run()
    return jobs, len(delivered)


async def reminder_loop(app) -> None:
    """Poll durable reminder jobs and dispatch committed Telegram delivery rows."""
    interval = app.state.settings.reminder_interval_seconds
    dispatch = app.state.settings.telegram_mode == "real"
    if interval <= 0 and not dispatch:
        logger.info("reminder_worker_disabled")
        return
    delay = min(2, interval) if dispatch and interval > 0 else 2 if dispatch else interval
    next_schedule = time.monotonic()
    logger.info("reminder_worker_started interval_seconds=%s live_delivery=%s", interval, dispatch)
    while True:
        schedule = interval > 0 and time.monotonic() >= next_schedule
        if schedule:
            next_schedule = time.monotonic() + interval
        try:
            # Finish any in-flight attempt before lifespan closes the HTTP client.
            cycle = asyncio.create_task(asyncio.to_thread(run_cycle, app, schedule=schedule))
            try:
                jobs, delivered = await asyncio.shield(cycle)
            except asyncio.CancelledError:
                await asyncio.gather(cycle, return_exceptions=True)
                raise
            if jobs or delivered:
                logger.info(
                    "reminder_worker_cycle processed=%s delivery_attempts=%s", jobs, delivered
                )
        except Exception:
            logger.exception("delivery_worker_cycle_failed")
        await asyncio.sleep(delay)
