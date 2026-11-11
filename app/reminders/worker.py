from __future__ import annotations

import asyncio
import logging

from app.database import begin_sqlite_write

logger = logging.getLogger(__name__)


def run_cycle(app) -> tuple[int, int]:
    with app.state.session_factory.begin() as db:
        begin_sqlite_write(db)
        created = app.state.reminder_service.run(db)
        retried = app.state.telegram_client.retry_due(db)
        return len(created), len(retried)


async def reminder_loop(app) -> None:
    """Run due reminder policy periodically; database dedupe makes restarts safe."""
    interval = app.state.settings.reminder_interval_seconds
    if interval <= 0:
        logger.info("reminder_worker_disabled")
        return
    logger.info("reminder_worker_started interval_seconds=%s", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            # Let an in-flight transaction finish before lifespan closes the HTTP client.
            cycle = asyncio.create_task(asyncio.to_thread(run_cycle, app))
            try:
                created, retried = await asyncio.shield(cycle)
            except asyncio.CancelledError:
                await asyncio.gather(cycle, return_exceptions=True)
                raise
            logger.info(
                "reminder_worker_cycle processed=%s delivery_retries=%s",
                created,
                retried,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reminder_worker_cycle_failed")
