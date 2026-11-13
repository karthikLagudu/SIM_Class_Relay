from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.auth.security import hash_password
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.demo import QUICK_DEMO_ACCOUNTS, QUICK_DEMO_ASSIGNMENT_KEY, QUICK_DEMO_CHAT_IDS
from app.models import (
    ClassMembership,
    Classroom,
    Invite,
    School,
    SchoolMembership,
    StudentAssignmentState,
    User,
)
from app.services.assignment import AssignmentService


def add_user(db, name: str, email: str, password: str, telegram_id: str | None = None) -> User:
    user = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        telegram_user_id=telegram_id,
        telegram_chat_id=telegram_id,
    )
    db.add(user)
    db.flush()
    return user


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Class Relay demo data.")
    parser.add_argument(
        "--full-demo", action="store_true", help="Seed the isolated reviewer database."
    )
    parser.add_argument(
        "--reset", action="store_true", help="Replace all records in the full-demo database."
    )
    args = parser.parse_args()
    if args.reset and not args.full_demo:
        parser.error("--reset requires --full-demo")

    settings = get_settings()
    if settings.environment == "production":
        raise SystemExit("Demo accounts require a development database.")
    if args.full_demo:
        from scripts.seed_full_demo import FULL_DEMO_DIR, seed_full_demo

        try:
            changed = seed_full_demo(FULL_DEMO_DIR, reset=args.reset)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            "Seeded the full Class Relay demo."
            if changed
            else "Full demo already seeded. No records changed."
        )
        print(f"Database: {FULL_DEMO_DIR / 'class_relay.db'}")
        print("See DEMO.md for the accounts and local run commands.")
        return

    init_db()
    with SessionLocal.begin() as db:
        existing = db.scalar(select(School).where(School.name == "SIM Demo School"))
        if existing:
            print("Demo data already exists; nothing changed.")
            return
        school = School(name="SIM Demo School", timezone="Asia/Kolkata")
        other_school = School(name="Northstar Academy", timezone="Asia/Kolkata")
        db.add_all([school, other_school])
        db.flush()
        log_mode = settings.telegram_mode == "log"
        users = {
            key: add_user(
                db,
                account.name,
                account.email,
                account.password,
                QUICK_DEMO_CHAT_IDS.get(key) if log_mode else None,
            )
            for key, account in QUICK_DEMO_ACCOUNTS.items()
        }
        teacher, student1, student2 = users["teacher"], users["student1"], users["student2"]
        coordinator, outsider, lead = users["coordinator"], users["outsider"], users["lead"]
        class_a = Classroom(school_id=school.id, name="Grade 8 - A", grade="Grade 8")
        class_b = Classroom(school_id=other_school.id, name="Grade 9 Mathematics", grade="Grade 9")
        db.add_all([class_a, class_b])
        db.flush()
        elective = Classroom(school_id=school.id, name="Grade 8 - B", grade="Grade 8")
        lead_class = Classroom(school_id=school.id, name="Grade 8 - C", grade="Grade 8")
        db.add_all([elective, lead_class])
        db.flush()
        db.add_all(
            [
                SchoolMembership(school_id=school.id, user_id=teacher.id, role="teacher"),
                SchoolMembership(school_id=school.id, user_id=student1.id, role="student"),
                SchoolMembership(school_id=school.id, user_id=student2.id, role="student"),
                SchoolMembership(school_id=school.id, user_id=coordinator.id, role="coordinator"),
                SchoolMembership(school_id=other_school.id, user_id=outsider.id, role="teacher"),
                SchoolMembership(school_id=school.id, user_id=lead.id, role="coordinator"),
                SchoolMembership(school_id=school.id, user_id=lead.id, role="teacher"),
                ClassMembership(classroom_id=class_a.id, user_id=teacher.id, role="teacher"),
                ClassMembership(classroom_id=class_a.id, user_id=student1.id, role="student"),
                ClassMembership(classroom_id=class_a.id, user_id=student2.id, role="student"),
                ClassMembership(classroom_id=class_b.id, user_id=outsider.id, role="teacher"),
                ClassMembership(classroom_id=elective.id, user_id=teacher.id, role="teacher"),
                ClassMembership(classroom_id=elective.id, user_id=student1.id, role="student"),
                ClassMembership(classroom_id=lead_class.id, user_id=lead.id, role="teacher"),
                Invite(
                    school_id=school.id,
                    classroom_id=class_a.id,
                    code="CLASS8A",
                    max_uses=20,
                    use_count=0,
                    active=True,
                ),
                Invite(
                    school_id=school.id,
                    classroom_id=elective.id,
                    code="OLDCLASS8B",
                    expires_at=datetime.now(UTC) - timedelta(days=1),
                    active=True,
                    use_count=0,
                ),
            ]
        )
        db.flush()
        assignment = AssignmentService().create(
            db,
            teacher,
            class_a.id,
            "Climate observation journal",
            "Record the weather for two days and upload a photo of your completed journal.",
            datetime.now(UTC) + timedelta(hours=36),
            school.timezone,
            QUICK_DEMO_ASSIGNMENT_KEY,
        )
        state = db.scalar(
            select(StudentAssignmentState).where(
                StudentAssignmentState.assignment_id == assignment.id,
                StudentAssignmentState.student_id == student1.id,
            )
        )
        state.status = "in_progress"
        state.last_activity_at = datetime.now(UTC)
        AssignmentService().create(
            db,
            teacher,
            elective.id,
            "Water conservation checklist",
            "List three ways to save water at home and explain one of them.",
            datetime.now(UTC) + timedelta(days=3),
            school.timezone,
            "seed:assignment:elective",
        )
    print("Seeded Class Relay demo data.")
    print("Teacher: teacher@demo.local / Teacher123!")
    print("Students: student1@demo.local and student2@demo.local / Student123!")
    print("Coordinator: coordinator@demo.local / Coordinator123!")
    print("Teaching coordinator: lead@demo.local / Coordinator123!")
    print("Invite: CLASS8A (send /join CLASS8A after connecting your student account)")


if __name__ == "__main__":
    main()
