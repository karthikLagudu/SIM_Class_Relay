from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.auth.security import hash_password
from app.config import get_settings
from app.database import SessionLocal, init_db
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
    settings = get_settings()
    if settings.environment == "production":
        raise SystemExit("Demo accounts require a development database.")
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
        teacher = add_user(db, "Maya Teacher", "teacher@demo.local", "Teacher123!", "900001" if log_mode else None)
        student1 = add_user(db, "Aarav Student", "student1@demo.local", "Student123!", "900101" if log_mode else None)
        student2 = add_user(db, "Diya Student", "student2@demo.local", "Student123!")
        coordinator = add_user(db, "Ravi Coordinator", "coordinator@demo.local", "Coordinator123!")
        outsider = add_user(db, "Nora Teacher", "teacher@northstar.demo.local", "Teacher123!")
        class_a = Classroom(school_id=school.id, name="Grade 8 - A", grade="Grade 8")
        class_b = Classroom(school_id=other_school.id, name="Grade 9 Mathematics", grade="Grade 9")
        db.add_all([class_a, class_b])
        db.flush()
        db.add_all(
            [
                SchoolMembership(school_id=school.id, user_id=teacher.id, role="teacher"),
                SchoolMembership(school_id=school.id, user_id=student1.id, role="student"),
                SchoolMembership(school_id=school.id, user_id=student2.id, role="student"),
                SchoolMembership(school_id=school.id, user_id=coordinator.id, role="coordinator"),
                SchoolMembership(school_id=other_school.id, user_id=outsider.id, role="teacher"),
                ClassMembership(classroom_id=class_a.id, user_id=teacher.id, role="teacher"),
                ClassMembership(classroom_id=class_a.id, user_id=student1.id, role="student"),
                ClassMembership(classroom_id=class_a.id, user_id=student2.id, role="student"),
                ClassMembership(classroom_id=class_b.id, user_id=outsider.id, role="teacher"),
                Invite(
                    school_id=school.id,
                    classroom_id=class_a.id,
                    code="CLASS8A",
                    max_uses=20,
                    use_count=0,
                    active=True,
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
            "seed:assignment:1",
        )
        state = db.scalar(
            select(StudentAssignmentState).where(
                StudentAssignmentState.assignment_id == assignment.id,
                StudentAssignmentState.student_id == student1.id,
            )
        )
        state.status = "in_progress"
        state.last_activity_at = datetime.now(UTC)
    print("Seeded Class Relay demo data.")
    print("Teacher: teacher@demo.local / Teacher123!")
    print("Students: student1@demo.local and student2@demo.local / Student123!")
    print("Coordinator: coordinator@demo.local / Coordinator123!")
    print("Invite: CLASS8A (send /join CLASS8A after connecting your student account)")


if __name__ == "__main__":
    main()
