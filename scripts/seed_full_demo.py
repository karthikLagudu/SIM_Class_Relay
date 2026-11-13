from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, inspect, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.config import get_settings
from app.database import Base, create_database_engine
from app.demo import (
    DEMO_CHAT_PLACEHOLDERS,
    FULL_DEMO_ACCOUNTS,
    FULL_DEMO_SEED_KEY,
    FULL_DEMO_SEED_OPERATION,
)
from app.llm.fake import DemoLLM
from app.models import (
    ClassMembership,
    Classroom,
    IdempotencyKey,
    Invite,
    Reminder,
    School,
    SchoolMembership,
    StudentAssignmentState,
    User,
)
from app.services.assignment import AssignmentService
from app.services.classroom import ClassroomService
from app.services.notification import NotificationService
from app.services.overdue import OverdueService
from app.services.progress import ProgressService
from app.services.reminder import ReminderService
from app.services.submission import FeedbackService, SubmissionService
from app.telegram.client import TelegramClient

FULL_DEMO_DIR = Path(__file__).resolve().parents[1] / ".demo-data" / "multi-school-demo"


def seed_full_demo(directory: Path, *, reset: bool = False) -> bool:
    settings = get_settings()
    if settings.environment == "production":
        raise ValueError("Demo accounts require a development database.")

    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    database_url = str(URL.create("sqlite", database=(directory / "class_relay.db").as_posix()))
    settings = settings.model_copy(
        update={
            "database_url": database_url,
            "upload_dir": directory / "uploads",
            "telegram_mode": "log",
            "llm_mode": "fake",
            "quiet_hour_start": 0,
            "quiet_hour_end": 0,
        }
    )
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            tables = inspect(connection).get_table_names()
            with Session(connection, autoflush=False) as db:
                if tables:
                    operation = None
                    if IdempotencyKey.__tablename__ in tables:
                        operation = db.scalar(
                            select(IdempotencyKey.operation).where(
                                IdempotencyKey.key == FULL_DEMO_SEED_KEY
                            )
                        )
                    if operation != FULL_DEMO_SEED_OPERATION:
                        raise ValueError(
                            f"Refusing to modify an unrecognized database at {directory / 'class_relay.db'}."
                        )
                    if not reset:
                        return False
                    for table in reversed(Base.metadata.sorted_tables):
                        db.execute(table.delete())
                else:
                    Base.metadata.create_all(connection)

                users, classes = _seed_people_and_classes(db)
                telegram = TelegramClient(settings)
                try:
                    _seed_classwork(db, users, classes, telegram)
                finally:
                    telegram.close()
                db.add(IdempotencyKey(key=FULL_DEMO_SEED_KEY, operation=FULL_DEMO_SEED_OPERATION))
                db.flush()
    finally:
        engine.dispose()
    return True


def _seed_people_and_classes(db: Session) -> tuple[dict[str, User], dict[str, Classroom]]:
    willowbend = School(name="Willowbend Academy", timezone="Asia/Kolkata")
    harbor = School(name="Cedar Harbor School", timezone="Asia/Singapore")
    grove = School(name="Meridian Grove School", timezone="Asia/Manila")
    schools = {"willowbend": willowbend, "cedarharbor": harbor, "meridiangrove": grove}
    db.add_all(schools.values())
    db.flush()
    password_hashes = {
        password: hash_password(password)
        for password in {account.password for account in FULL_DEMO_ACCOUNTS}
    }
    users: dict[str, User] = {}
    for account in FULL_DEMO_ACCOUNTS:
        telegram_id = DEMO_CHAT_PLACEHOLDERS.get(account.email)
        users[account.handle] = User(
            name=account.name,
            email=account.email,
            password_hash=password_hashes[account.password],
            telegram_user_id=telegram_id,
            telegram_chat_id=telegram_id,
        )
    db.add_all(users.values())
    db.flush()
    db.add_all(
        SchoolMembership(
            school_id=schools[school].id,
            user_id=users[account.handle].id,
            role=role,
        )
        for account in FULL_DEMO_ACCOUNTS
        for school, role in account.school_roles
    )
    db.flush()
    mira, nell = users["mira"], users["nell"]
    service = ClassroomService()
    classes = {
        "8A": service.create_classroom(db, mira, willowbend.id, "Grade 8A Science", "Grade 8"),
        "8B": service.create_classroom(db, mira, willowbend.id, "Grade 8B Science", "Grade 8"),
        "9H": service.create_classroom(db, mira, harbor.id, "Grade 9 Field Studies", "Grade 9"),
        "8C": service.create_classroom(db, mira, willowbend.id, "Grade 8C Lab", "Grade 8"),
        "9M": service.create_classroom(db, mira, harbor.id, "Grade 9 Mapping", "Grade 9"),
        "9D": service.create_classroom(db, mira, harbor.id, "Grade 9 Coastal Design", "Grade 9"),
        "7A": service.create_classroom(db, nell, grove.id, "Grade 7A Ecology", "Grade 7"),
        "7B": service.create_classroom(db, nell, grove.id, "Grade 7B Data", "Grade 7"),
        "7C": service.create_classroom(db, nell, grove.id, "Grade 7C Studio", "Grade 7"),
    }
    teaching = {
        "8A": ("mira", "arun"),
        "8B": ("arun", "tessa"),
        "9H": ("jonah", "nell"),
        "9M": ("jonah",),
        "9D": ("luca",),
        "7A": ("sam", "amina"),
        "7B": ("elivale",),
        "7C": ("sam", "rose"),
    }
    db.add_all(
        ClassMembership(classroom_id=classes[key].id, user_id=users[handle].id, role="teacher")
        for key, team in teaching.items()
        for handle in team
    )
    db.add_all(
        ClassMembership(
            classroom_id=classes[key].id,
            user_id=users[account.handle].id,
            role="student",
        )
        for account in FULL_DEMO_ACCOUNTS
        for key in account.student_classes
    )

    now = datetime.now(UTC)
    for key, code, expires_in_days in (
        ("8A", "WILLOW8A", 14),
        ("8B", "WILLOW8B", 14),
        ("8B", "OLDWILLOW8B", -2),
        ("9H", "CEDAR9", 14),
        ("8C", "WILLOW8C", 14),
        ("9M", "OLDMAP9", -2),
        ("9M", "MAP9", 14),
        ("9D", "CEDAR9D", 14),
        ("7A", "GROVE7A", 14),
        ("7B", "OLDGROVE7B", -2),
        ("7B", "GROVE7B", 14),
        ("7C", "GROVE7C", 14),
    ):
        classroom = classes[key]
        db.add(
            Invite(
                school_id=classroom.school_id,
                classroom_id=classroom.id,
                code=code,
                expires_at=now + timedelta(days=expires_in_days),
                max_uses=20,
                active=True,
            )
        )
    db.flush()
    return users, classes


def _seed_classwork(
    db: Session, users: dict[str, User], classes: dict[str, Classroom], telegram: TelegramClient
) -> None:
    now = datetime.now(UTC)
    notifications = NotificationService(telegram)
    assignments = AssignmentService(notifications)
    progress = ProgressService()
    submissions = SubmissionService()
    feedback = FeedbackService(notifications)
    arun, jonah = users["arun"], users["jonah"]
    class_a, class_b, harbor = classes["8A"], classes["8B"], classes["9H"]

    vocabulary = assignments.create(
        db,
        arun,
        class_a.id,
        "Watershed vocabulary",
        "Label the watershed diagram with tributary, source, and mouth. Explain one label in your own words.",
        now + timedelta(hours=8),
        "Asia/Kolkata",
        "full-demo:watershed",
        [users[key].id for key in ("isha", "dev", "nila", "kavi")],
    )
    progress.update(
        db,
        users["isha"],
        vocabulary.id,
        "blocked",
        "I can label the source, but I’m unsure where the tributary joins. Can you point me to one example?",
    )
    progress.update(
        db,
        users["nila"],
        vocabulary.id,
        "acknowledged",
        "I have the diagram and will start after class.",
    )
    progress.update(
        db, users["kavi"], vocabulary.id, "in_progress", "I labeled the source and mouth.", 60
    )

    rain = assignments.create(
        db,
        arun,
        class_a.id,
        "Rain gauge observations",
        "Record rainfall for three days. Submit a chart or describe the pattern in a few sentences.",
        now + timedelta(days=3),
        "Asia/Kolkata",
        "full-demo:rain",
        [users[key].id for key in ("leena", "rafi", "meera")],
    )
    image, content = _seed_image(telegram.settings.upload_dir)
    submissions.submit(
        db,
        users["leena"],
        rain.id,
        "full-demo:rain:leena",
        stored_file_path=str(image),
        original_filename="rain-gauge-chart.png",
        mime_type="image/png",
        content=content,
    )
    revision = submissions.submit(
        db,
        users["rafi"],
        rain.id,
        "full-demo:rain:rafi",
        text_content="It rained most on Tuesday. Wednesday was lower.",
    )
    feedback.create(
        db,
        arun,
        revision.id,
        "Please add the rainfall in millimetres for each day and explain how you measured it.",
        idempotency_key="full-demo:revision:rafi",
    )
    complete = submissions.submit(
        db,
        users["meera"],
        rain.id,
        "full-demo:rain:meera",
        text_content="Monday 4 mm, Tuesday 9 mm, Wednesday 6 mm. Tuesday was the wettest day.",
    )
    feedback.create(
        db,
        arun,
        complete.id,
        "Clear measurements and a correct comparison.",
        complete=True,
        idempotency_key="full-demo:complete:meera",
    )

    overdue = AssignmentService().create(
        db,
        arun,
        class_a.id,
        "Cloud cover notes",
        "Describe the cloud cover at noon in two sentences.",
        now + timedelta(days=1),
        "Asia/Kolkata",
        "full-demo:clouds",
        [users["kavi"].id],
    )
    # Reconstruct a historical assignment before generating its notifications and schedule.
    overdue.created_at = now - timedelta(days=3)
    overdue.due_at = now - timedelta(days=1)
    db.flush()
    db.execute(delete(Reminder).where(Reminder.assignment_id == overdue.id))
    assignments.scheduler.schedule_assignment(db, overdue, overdue.created_at)
    state = db.scalar(
        select(StudentAssignmentState).where(StudentAssignmentState.assignment_id == overdue.id)
    )
    OverdueService().mark_overdue(db, overdue, state, now)
    notifications.notify_assignment_created(db, overdue)

    assignments.create(
        db,
        arun,
        class_b.id,
        "Field sketch practice",
        "Sketch a leaf or seed, label two features, and explain what one feature does. A written description is also accepted.",
        now + timedelta(days=3),
        "Asia/Kolkata",
        "full-demo:sketch",
    )
    assignments.create(
        db,
        arun,
        class_b.id,
        "Soil texture lab",
        "Compare two soil samples. Describe their texture and predict which drains faster.",
        now + timedelta(days=4),
        "Asia/Kolkata",
        "full-demo:soil",
        [users[key].id for key in ("isha", "tara", "sana")],
        draft=True,
    )
    cancelled = assignments.create(
        db,
        arun,
        class_b.id,
        "Pond habitat poster",
        "Sketch a pond habitat and label three organisms.",
        now + timedelta(days=4),
        "Asia/Kolkata",
        "full-demo:pond",
        [users["tara"].id, users["rin"].id],
    )
    assignments.cancel(db, arun, cancelled.id, "full-demo:pond:cancel")

    shoreline = assignments.create(
        db,
        jonah,
        harbor.id,
        "Shoreline survey",
        "Compare two stretches of shoreline and note a sign of erosion in each.",
        now + timedelta(hours=6),
        "Asia/Singapore",
        "full-demo:shoreline",
    )
    submissions.submit(
        db,
        users["kit"],
        shoreline.id,
        "full-demo:shoreline:kit",
        text_content="The exposed stretch had a steep cut in the sand. The sheltered stretch had smaller ripples.",
    )
    progress.update(
        db,
        users["eli"],
        shoreline.id,
        "blocked",
        "I can see the cut in the sand. Should I also count exposed roots as a sign of erosion?",
    )
    db.flush()
    for index, (key, teacher, learner, title) in enumerate(
        (
            ("8C", "mira", "willow09", "Lab safety checklist"),
            ("9M", "jonah", "cedar07", "Sketch a harbor map"),
            ("9D", "luca", "cedar15", "Design a coastal barrier"),
            ("7A", "sam", "grove01", "Map a food web"),
            ("7B", "elivale", "grove10", "Compare two data sets"),
            ("7C", "rose", "grove18", "Studio materials journal"),
        )
    ):
        classroom = classes[key]
        item = assignments.create(
            db,
            users[teacher],
            classroom.id,
            title,
            "Record two observations, explain one of them, and include a short example.",
            now + timedelta(days=3),
            db.get(School, classroom.school_id).timezone,
            f"full-demo:classwork:{key}",
            coordinator=key == "8C",
        )
        submitted = submissions.submit(
            db,
            users[learner],
            item.id,
            f"full-demo:example:{key}",
            text_content="I recorded two examples and compared the features they share.",
        )
        if index in {2, 4}:
            feedback.create(
                db,
                users[teacher],
                submitted.id,
                "Add one measurement or a labeled example to support your comparison.",
                complete=index == 4,
                idempotency_key=f"full-demo:feedback:{key}",
            )

    for key, teacher, draft_title, cancelled_title in (
        ("9D", "luca", "Coastal materials draft", "Tide chart practice"),
        ("7C", "rose", "Studio reflection draft", "Color wheel practice"),
    ):
        classroom = classes[key]
        timezone = db.get(School, classroom.school_id).timezone
        assignments.create(
            db,
            users[teacher],
            classroom.id,
            draft_title,
            "Describe two materials and how you used them.",
            now + timedelta(days=4),
            timezone,
            f"full-demo:draft:{key}",
            draft=True,
        )
        closed = assignments.create(
            db,
            users[teacher],
            classroom.id,
            cancelled_title,
            "Compare the examples from class.",
            now + timedelta(days=5),
            timezone,
            f"full-demo:closed:{key}",
        )
        assignments.cancel(db, users[teacher], closed.id, f"full-demo:cancel:{key}")

    db.flush()
    # Keep Willowbend's due jobs available for the reviewer to run manually.
    ReminderService(DemoLLM(), telegram, telegram.settings).run(db, classroom_ids={harbor.id})


def _seed_image(upload_dir: Path) -> tuple[Path, bytes]:
    """Render a tiny three-day rainfall chart using only the standard library."""
    width, height = 180, 120
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # PNG scanline without a filter.
        for x in range(width):
            color = (247, 248, 240)
            if (x == 24 and 13 <= y <= 99) or (y == 99 and 24 <= x <= 165):
                color = (23, 53, 43)
            for left, bar_height in ((40, 32), (82, 72), (124, 48)):
                if left <= x < left + 24 and 99 - bar_height <= y < 99:
                    color = (36, 101, 77)
                    break
            rows.extend(color)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    content = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    upload_dir.mkdir(parents=True, exist_ok=True)
    image = upload_dir / "full-demo-rain-gauge.png"
    try:
        with image.open("xb") as output:
            output.write(content)
    except FileExistsError:
        if image.read_bytes() != content:
            raise ValueError(f"Refusing to overwrite an existing upload at {image}.") from None
    return image, content
