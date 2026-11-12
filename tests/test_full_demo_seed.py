import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.database import create_database_engine, get_db
from app.demo import (
    FULL_DEMO_ACCOUNTS,
    FULL_DEMO_PASSWORD,
    QUICK_DEMO_ACCOUNTS,
    QUICK_DEMO_ASSIGNMENT_KEY,
)
from app.models import IdempotencyKey, User
from scripts.seed_full_demo import seed_full_demo


def test_full_demo_seed_is_repeatable_and_reset_is_explicit(tmp_path):
    assert seed_full_demo(tmp_path)
    database = tmp_path / "class_relay.db"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM schools").fetchone() == (3,)
        assert (
            db.execute("SELECT count(*) FROM classrooms GROUP BY school_id").fetchall()
            == [(3,)] * 3
        )
        assert (
            db.execute(
                "SELECT count(*) FROM school_memberships WHERE role = 'student' GROUP BY school_id"
            ).fetchall()
            == [(25,)] * 3
        )
        assert (
            db.execute(
                "SELECT count(*) FROM school_memberships WHERE role = 'teacher' GROUP BY school_id"
            ).fetchall()
            == [(4,)] * 3
        )
        assert db.execute(
            "SELECT count(*) FROM school_memberships WHERE role = 'coordinator' GROUP BY user_id ORDER BY count(*)"
        ).fetchall() == [(1,), (2,)]
        assert db.execute("""SELECT count(*) FROM classrooms c WHERE NOT EXISTS (
                          SELECT 1 FROM class_memberships cm WHERE cm.classroom_id = c.id AND cm.role = 'teacher'
                          )""").fetchone() == (1,)
        assert {
            status
            for (status,) in db.execute("SELECT DISTINCT status FROM student_assignment_states")
        } == {
            "assigned",
            "acknowledged",
            "in_progress",
            "blocked",
            "submitted",
            "needs_revision",
            "completed",
            "overdue",
            "cancelled",
        }
        assert {
            status
            for (status,) in db.execute("SELECT DISTINCT status FROM notification_deliveries")
        } == {"logged", "skipped"}
        deadlines = {
            title: datetime.fromisoformat(due).replace(tzinfo=UTC)
            for title, due in db.execute("SELECT title, due_at FROM assignments")
        }
        now = datetime.now(UTC)
        assert now < deadlines["Watershed vocabulary"] < now + timedelta(hours=24)
        assert deadlines["Cloud cover notes"] < now
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.execute(
            "UPDATE assignments SET title = 'Reviewer edit' WHERE title = 'Soil texture lab'"
        )
        db.commit()
        before = list(db.iterdump())

    image = tmp_path / "uploads" / "full-demo-rain-gauge.png"
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert not seed_full_demo(tmp_path)
    with sqlite3.connect(database) as db:
        assert list(db.iterdump()) == before

    assert seed_full_demo(tmp_path, reset=True)
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT status FROM assignments WHERE title = 'Soil texture lab'"
        ).fetchone() == ("draft",)
        assert db.execute("SELECT count(*) FROM assignments").fetchone() == (17,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_full_demo_reset_preserves_an_unrecognized_database(tmp_path):
    database = tmp_path / "class_relay.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE reviewer_notes (note TEXT)")
        db.execute("INSERT INTO reviewer_notes VALUES ('keep this')")

    with pytest.raises(ValueError, match="unrecognized database"):
        seed_full_demo(tmp_path, reset=True)

    with sqlite3.connect(database) as db:
        assert db.execute("SELECT note FROM reviewer_notes").fetchall() == [("keep this",)]
        assert db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == [
            ("reviewer_notes",)
        ]


def test_login_groups_all_seeded_accounts_and_gates_the_picker(client, app, monkeypatch, tmp_path):
    assert seed_full_demo(tmp_path)
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'class_relay.db').as_posix()}")

    def demo_db():
        with Session(engine) as db:
            yield db

    with Session(engine) as db:
        db.add(User(name="Reviewer", email="reviewer@willowbend.example", password_hash="unused"))
        db.commit()
    monkeypatch.setitem(app.dependency_overrides, get_db, demo_db)
    monkeypatch.setattr(app.state.settings, "environment", "development")
    monkeypatch.setattr(app.state.settings, "telegram_mode", "log")
    monkeypatch.setattr(app.state.settings, "llm_mode", "fake")
    monkeypatch.setattr(app.state.settings, "demo_mode", True)

    try:
        page = client.get("/login")
        assert page.headers["Cache-Control"] == "no-store"
        assert 'aria-expanded="false" aria-controls="demo-accounts"' in page.text
        assert 'id="demo-accounts" aria-labelledby="demo-heading" hidden' in page.text
        assert (
            "Show demo accounts" in page.text and "Telegram deliveries are simulated." in page.text
        )
        assert "reviewer@willowbend.example" not in page.text
        assert "teacher@demo.local" not in page.text
        groups = {
            role: {account["email"]: account for account in accounts}
            for role, accounts in page.context["demo_groups"].items()
        }
        assert {role: len(accounts) for role, accounts in groups.items()} == {
            "coordinator": 2,
            "teacher": 12,
            "student": 75,
        }
        for account in FULL_DEMO_ACCOUNTS:
            roles = {role for _, role in account.school_roles}
            assert {role for role in groups if account.email in groups[role]} == roles
            assert all(groups[role][account.email]["name"] == account.name for role in roles)
        mira = groups["coordinator"]["mira@willowbend.example"]
        assert mira["multi_school"] and mira["roles"] == ["Coordinator", "Teacher"]
        assert {school for school, _ in mira["access"]} == {
            "Willowbend Academy",
            "Cedar Harbor School",
        }
        nell = "nell@cedarharbor.example"
        assert groups["coordinator"][nell]["access"] == [("Meridian Grove School", [])]
        assert groups["teacher"][nell]["access"] == [
            ("Cedar Harbor School", ["Grade 9 Field Studies"])
        ]
        assert groups["teacher"]["evan@willowbend.example"]["access"] == [
            ("Willowbend Academy", [])
        ]
        assert groups["student"]["isha@willowbend.example"]["access"] == [
            ("Willowbend Academy", ["Grade 8A Science", "Grade 8B Science"])
        ]

        for setting, value in (
            ("demo_mode", False),
            ("telegram_mode", "real"),
            ("llm_mode", "real"),
            ("environment", "production"),
        ):
            with monkeypatch.context() as disabled:
                disabled.setattr(app.state.settings, setting, value)
                failed = client.post(
                    "/login", data={"email": "bad@example.test", "password": "bad"}
                )
                assert failed.status_code == 401
                for response in (client.get("/login"), failed):
                    assert "demo-accounts" not in response.text
                    assert FULL_DEMO_PASSWORD not in response.text
                    assert all(account.email not in response.text for account in FULL_DEMO_ACCOUNTS)
    finally:
        engine.dispose()


def test_login_supports_quick_demo_definitions_and_requires_a_seed(
    client, app, db, data, monkeypatch
):
    monkeypatch.setattr(app.state.settings, "environment", "development")
    monkeypatch.setattr(app.state.settings, "llm_mode", "fake")
    monkeypatch.setattr(app.state.settings, "demo_mode", True)
    for user, key in (("teacher1", "teacher"), ("teacher2", "outsider"), ("student2", "student2")):
        data[user].email = QUICK_DEMO_ACCOUNTS[key].email
    db.commit()
    assert "Show demo accounts" not in client.get("/login").text

    db.add(IdempotencyKey(key=QUICK_DEMO_ASSIGNMENT_KEY, operation="create_assignment"))
    db.commit()
    page = client.get("/login")
    for key in ("teacher", "outsider", "student2"):
        assert f'data-demo-email="{QUICK_DEMO_ACCOUNTS[key].email}"' in page.text
    assert QUICK_DEMO_ACCOUNTS["student1"].email not in page.text
