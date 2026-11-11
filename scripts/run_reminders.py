from app.config import get_settings
from app.database import SessionLocal, begin_sqlite_write, init_db
from app.dependencies import build_llm_provider
from app.services.delivery import DeliveryProcessor
from app.services.reminder import ReminderService
from app.telegram.client import TelegramClient


def main() -> None:
    settings = get_settings()
    init_db()
    telegram = TelegramClient(settings)
    try:
        service = ReminderService(build_llm_provider(settings), telegram, settings)
        with SessionLocal.begin() as db:
            begin_sqlite_write(db)
            reminders = service.run(db)
        attempts = DeliveryProcessor(telegram, SessionLocal).run()
        for item in reminders:
            print(
                f"{item.reminder_type}: assignment={item.assignment_id} student={item.student_id} status={item.status}"
            )
        print(f"Processed {len(reminders)} reminder(s) and {len(attempts)} delivery attempts.")
    finally:
        telegram.close()


if __name__ == "__main__":
    main()
