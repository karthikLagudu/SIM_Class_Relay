from collections import defaultdict
from ipaddress import ip_address

from fastapi import Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.demo import (
    FULL_DEMO_ACCOUNTS,
    FULL_DEMO_SEED_KEY,
    FULL_DEMO_SEED_OPERATION,
    QUICK_DEMO_ACCOUNTS,
    QUICK_DEMO_ASSIGNMENT_KEY,
)
from app.models import ClassMembership, Classroom, IdempotencyKey, School, SchoolMembership, User
from app.web.helpers import WORKSPACES


def demo_account_groups(settings: Settings, db: Session, request: Request) -> dict:
    if (
        not settings.demo_mode
        or settings.environment != "development"
        or settings.llm_mode != "fake"
    ):
        return {}
    if settings.telegram_mode == "real" and not _local_request(request):
        return {}

    full = db.get(IdempotencyKey, FULL_DEMO_SEED_KEY)
    if full and full.operation == FULL_DEMO_SEED_OPERATION:
        definitions = FULL_DEMO_ACCOUNTS
    else:
        quick = db.get(IdempotencyKey, QUICK_DEMO_ASSIGNMENT_KEY)
        if not quick or quick.operation != "create_assignment":
            return {}
        definitions = QUICK_DEMO_ACCOUNTS.values()

    passwords = {account.email: account.password for account in definitions}
    users = db.execute(
        select(User.id, User.name, User.email)
        .where(User.email.in_(passwords))
        .order_by(User.name, User.email)
    ).all()
    user_ids = [user.id for user in users]
    access = defaultdict(lambda: defaultdict(dict))
    for user_id, role, school_id, school in db.execute(
        select(SchoolMembership.user_id, SchoolMembership.role, School.id, School.name)
        .join(School, School.id == SchoolMembership.school_id)
        .where(SchoolMembership.user_id.in_(user_ids))
        .order_by(School.name)
    ):
        access[user_id][role][(school_id, school)] = []
    for user_id, role, school_id, school, classroom in db.execute(
        select(
            ClassMembership.user_id, ClassMembership.role, School.id, School.name, Classroom.name
        )
        .join(Classroom, Classroom.id == ClassMembership.classroom_id)
        .join(School, School.id == Classroom.school_id)
        .where(
            ClassMembership.user_id.in_(user_ids),
            or_(ClassMembership.role != "teacher", Classroom.archived_at.is_(None)),
        )
        .order_by(School.name, Classroom.name)
    ):
        access[user_id][role].setdefault((school_id, school), []).append(classroom)

    groups = {role: [] for role in WORKSPACES}
    for user in users:
        roles = [role for role in WORKSPACES if role in access[user.id]]
        for role in roles:
            groups[role].append(
                {
                    "name": user.name,
                    "email": user.email,
                    "password": passwords[user.email],
                    "access": [
                        (school, classes) for (_, school), classes in access[user.id][role].items()
                    ],
                    "roles": [WORKSPACES[key] for key in roles],
                    "multi_school": len(access[user.id].get("coordinator", {})) > 1,
                }
            )
    return {role: accounts for role, accounts in groups.items() if accounts}


def _local_request(request: Request) -> bool:
    if not request.client or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    try:
        return ip_address(request.client.host).is_loopback
    except ValueError:
        return False
