from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.exceptions import AuthorizationError, ValidationError
from app.models import (
    ActivityEvent,
    Assignment,
    ClassMembership,
    Classroom,
    ConversationContext,
    Invite,
    NotificationDelivery,
    Reminder,
    School,
    SchoolMembership,
    User,
)
from app.reminders.policy import aware_utc
from app.services.authorization import (
    has_school_role,
    require_active_class,
    require_coordinator_class,
    require_coordinator_school,
    require_managed_class,
    workspace_roles,
)
from app.services.delivery import STAFF_ALERTS
from app.services.telegram_link import TelegramLinkService


@dataclass
class StudentSchoolRemoval:
    student: User
    membership: SchoolMembership | None
    enrollments: list[tuple[ClassMembership, Classroom]]
    last_removal: int | None

    @property
    def present(self) -> bool:
        return self.membership is not None or bool(self.enrollments)

    def snapshot(self) -> dict:
        return {
            "membership": self.membership.id if self.membership else None,
            "enrollments": [
                [member.id, classroom.id, aware_utc(member.created_at).isoformat()]
                for member, classroom in self.enrollments
            ],
            "last_removal": self.last_removal,
        }


class ClassroomService:
    @staticmethod
    def student_school_removal(
        db: Session, actor: User, school_id: int, student_id: int
    ) -> StudentSchoolRemoval:
        require_coordinator_school(db, actor, school_id)
        membership = db.scalar(
            select(SchoolMembership).where(
                SchoolMembership.school_id == school_id,
                SchoolMembership.user_id == student_id,
                SchoolMembership.role == "student",
            )
        )
        enrollments = [
            (member, classroom)
            for member, classroom in db.execute(
                select(ClassMembership, Classroom)
                .join(Classroom)
                .where(
                    Classroom.school_id == school_id,
                    ClassMembership.user_id == student_id,
                    ClassMembership.role == "student",
                )
                .order_by(Classroom.name, Classroom.id)
            )
        ]
        last_removal = db.scalar(
            select(ActivityEvent.id)
            .where(
                ActivityEvent.school_id == school_id,
                ActivityEvent.event_type == "student_removed_from_school",
                ActivityEvent.entity_type == "user",
                ActivityEvent.entity_id == student_id,
            )
            .order_by(ActivityEvent.id.desc())
            .limit(1)
        )
        student = db.get(User, student_id)
        if not student or not (membership or enrollments or last_removal):
            raise AuthorizationError("Resource is outside your authorized scope")
        return StudentSchoolRemoval(student, membership, enrollments, last_removal)

    def remove_student_from_school(
        self,
        db: Session,
        actor: User,
        school_id: int,
        student_id: int,
        expected: dict,
        links: TelegramLinkService,
    ) -> str:
        plan = self.student_school_removal(db, actor, school_id, student_id)
        if not plan.present:
            return f"{plan.student.name} was already removed from this school."
        if plan.snapshot() != expected:
            raise ValidationError("The student’s enrollments changed. Review the removal again.")
        now = datetime.now(UTC)
        if plan.membership:
            db.delete(plan.membership)
        for enrollment, _ in plan.enrollments:
            db.delete(enrollment)
        db.flush()
        remaining_access = bool(workspace_roles(db, plan.student))
        links.revoke_active_tokens(
            db, student_id, now, student_school_id=school_id if remaining_access else None
        )
        if not remaining_access:
            plan.student.telegram_user_id = None
            plan.student.telegram_chat_id = None

        assignments = select(Assignment.id).where(Assignment.school_id == school_id)
        classes = select(Classroom.id).where(Classroom.school_id == school_id)
        reason = "Student left the school"
        db.execute(
            update(Reminder)
            .where(
                Reminder.student_id == student_id,
                Reminder.assignment_id.in_(assignments),
                or_(
                    Reminder.status.in_(("pending", "deferred")),
                    (Reminder.status == "failed")
                    & Reminder.dedupe_key.in_(
                        select(Reminder.dedupe_key)
                        .join(
                            NotificationDelivery,
                            NotificationDelivery.idempotency_key
                            == ("reminder:" + Reminder.dedupe_key),
                        )
                        .where(NotificationDelivery.next_attempt_at.is_not(None))
                    ),
                ),
            )
            .values(status="cancelled", reason=reason, processed_at=now)
        )
        deliveries = update(NotificationDelivery).where(
            NotificationDelivery.user_id == student_id,
            NotificationDelivery.status.in_(("pending", "failed")),
            or_(
                NotificationDelivery.status == "pending",
                NotificationDelivery.next_attempt_at.is_not(None),
            ),
        )
        if remaining_access:
            scoped = or_(
                NotificationDelivery.school_id == school_id,
                NotificationDelivery.classroom_id.in_(classes),
                NotificationDelivery.assignment_id.in_(assignments),
            )
            # Old student summaries can contain several schools and have no single scope.
            summaries = (
                NotificationDelivery.assignment_id.is_(None)
                & NotificationDelivery.school_id.is_(None)
                & NotificationDelivery.classroom_id.is_(None)
                & NotificationDelivery.kind.in_(
                    ("status", "help", "telegram_connected", "student_summary")
                )
            )
            deliveries = deliveries.where(
                or_(
                    scoped
                    & NotificationDelivery.kind.not_in(STAFF_ALERTS | {"teacher_confirmation"}),
                    summaries,
                )
            )
        db.execute(deliveries.values(status="skipped", error=reason, next_attempt_at=None))
        if not remaining_access:
            db.execute(delete(ConversationContext).where(ConversationContext.user_id == student_id))
        db.add(
            ActivityEvent(
                school_id=school_id,
                actor_user_id=actor.id,
                event_type="student_removed_from_school",
                entity_type="user",
                entity_id=student_id,
                created_at=now,
                metadata_json={
                    "school_membership_id": plan.membership.id if plan.membership else None,
                    "classroom_ids": [classroom.id for _, classroom in plan.enrollments],
                    "class_names": [classroom.name for _, classroom in plan.enrollments],
                    "removed_membership_ids": [member.id for member, _ in plan.enrollments],
                },
            )
        )
        return (
            f"{plan.student.name} was removed from this school. Their assignment history was kept."
        )

    def edit_school(
        self, db: Session, actor: User, school_id: int, name: str, timezone: str
    ) -> School:
        school = require_coordinator_school(db, actor, school_id)
        name, timezone = name.strip(), timezone.strip()
        if not name or len(name) > 160:
            raise ValidationError("Enter a school name of up to 160 characters.")
        if not timezone or len(timezone) > 80:
            raise ValidationError("Enter an IANA time zone such as Asia/Kolkata.")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError("Enter an IANA time zone such as Asia/Kolkata.") from exc
        if (school.name, school.timezone) != (name, timezone):
            db.add(
                ActivityEvent(
                    school_id=school_id,
                    actor_user_id=actor.id,
                    event_type="school_updated",
                    entity_type="school",
                    entity_id=school_id,
                    metadata_json={
                        "before": {"name": school.name, "timezone": school.timezone},
                        "after": {"name": name, "timezone": timezone},
                    },
                )
            )
            school.name, school.timezone = name, timezone
        return school

    def add_teacher(
        self,
        db: Session,
        actor: User,
        school_id: int,
        classroom_ids: list[int],
        name: str,
        email: str,
        temporary_password: str,
    ) -> User:
        if not has_school_role(db, actor.id, school_id, {"coordinator"}):
            raise AuthorizationError("Resource is outside your authorized scope")

        clean_name = name.strip()
        clean_email = email.strip().lower()
        selected_ids = sorted(set(classroom_ids))
        if not clean_name or not clean_email or "@" not in clean_email:
            raise ValidationError("A valid teacher name and email are required")
        if len(temporary_password) < 10:
            raise ValidationError("Temporary password must be at least 10 characters")
        if db.scalar(select(User).where(User.email == clean_email)):
            raise ValidationError("An account with that email already exists")

        classrooms = list(db.scalars(select(Classroom).where(Classroom.id.in_(selected_ids))))
        if len(classrooms) != len(selected_ids) or any(
            classroom.school_id != school_id for classroom in classrooms
        ):
            raise AuthorizationError("Resource is outside your authorized scope")
        for classroom in classrooms:
            require_active_class(classroom)

        teacher = User(
            name=clean_name,
            email=clean_email,
            password_hash=hash_password(temporary_password),
        )
        db.add(teacher)
        db.flush()
        school_membership = SchoolMembership(
            school_id=school_id, user_id=teacher.id, role="teacher"
        )
        memberships = [
            ClassMembership(classroom_id=classroom.id, user_id=teacher.id, role="teacher")
            for classroom in classrooms
        ]
        db.add_all([school_membership, *memberships])
        db.flush()
        db.add(
            ActivityEvent(
                school_id=school_id,
                classroom_id=None,
                actor_user_id=actor.id,
                event_type="teacher_created",
                entity_type="user",
                entity_id=teacher.id,
                metadata_json={
                    "classroom_ids": selected_ids,
                    "school_membership_id": school_membership.id,
                    "class_membership_ids": [item.id for item in memberships],
                },
            )
        )
        return teacher

    def create_classroom(
        self, db: Session, actor: User, school_id: int, name: str, grade: str
    ) -> Classroom:
        allowed = has_school_role(db, actor.id, school_id, {"coordinator"})
        if not allowed:
            raise AuthorizationError("Resource is outside your authorized scope")
        clean_name, clean_grade = name.strip(), grade.strip()
        if not clean_name or not clean_grade or len(clean_name) > 160 or len(clean_grade) > 80:
            raise ValidationError("Enter a class name (up to 160 characters) and grade (up to 80).")
        existing = db.scalar(
            select(Classroom).where(Classroom.school_id == school_id, Classroom.name == clean_name)
        )
        if existing:
            raise ValidationError("A class with that name already exists")
        classroom = Classroom(school_id=school_id, name=clean_name, grade=clean_grade)
        db.add(classroom)
        db.flush()
        db.add(
            ActivityEvent(
                school_id=school_id,
                classroom_id=classroom.id,
                actor_user_id=actor.id,
                event_type="class_created",
                entity_type="classroom",
                entity_id=classroom.id,
                metadata_json={"name": classroom.name, "grade": classroom.grade},
            )
        )
        return classroom

    def edit_classroom(
        self, db: Session, actor: User, class_id: int, name: str, grade: str
    ) -> Classroom:
        classroom = require_active_class(require_coordinator_class(db, actor, class_id))
        name, grade = name.strip(), grade.strip()
        if not name or not grade or len(name) > 160 or len(grade) > 80:
            raise ValidationError("Enter a class name (up to 160 characters) and grade (up to 80).")
        if (
            db.scalar(
                select(Classroom.id).where(
                    Classroom.school_id == classroom.school_id,
                    Classroom.name == name,
                    Classroom.id != class_id,
                )
            )
            is not None
        ):
            raise ValidationError("A class with that name already exists")
        if (name, grade) != (classroom.name, classroom.grade):
            db.add(
                ActivityEvent(
                    school_id=classroom.school_id,
                    classroom_id=class_id,
                    actor_user_id=actor.id,
                    event_type="class_updated",
                    entity_type="classroom",
                    entity_id=class_id,
                    metadata_json={
                        "before": {"name": classroom.name, "grade": classroom.grade},
                        "after": {"name": name, "grade": grade},
                    },
                )
            )
            classroom.name, classroom.grade = name, grade
        return classroom

    def archive_classroom(self, db: Session, actor: User, class_id: int) -> Classroom:
        classroom = require_coordinator_class(db, actor, class_id)
        if classroom.archived_at is None:
            classroom.archived_at = datetime.now(UTC)
            self._cancel_reminders(db, class_id, "Class archived")
            db.add(
                ActivityEvent(
                    school_id=classroom.school_id,
                    classroom_id=class_id,
                    actor_user_id=actor.id,
                    event_type="class_archived",
                    entity_type="classroom",
                    entity_id=class_id,
                )
            )
        return classroom

    @staticmethod
    def _cancel_reminders(
        db: Session, class_id: int, reason: str, student_id: int | None = None
    ) -> None:
        query = update(Reminder).where(
            Reminder.assignment_id.in_(
                select(Assignment.id).where(Assignment.classroom_id == class_id)
            ),
            Reminder.status.in_(("pending", "deferred")),
        )
        if student_id is not None:
            query = query.where(Reminder.student_id == student_id)
        db.execute(query.values(status="cancelled", reason=reason, processed_at=datetime.now(UTC)))

    def student_class_change(
        self, db: Session, actor: User, class_id: int, student_id: int, destination_id: int | None
    ) -> tuple[Classroom, ClassMembership, Classroom | None]:
        source = require_active_class(require_coordinator_class(db, actor, class_id))
        member = db.scalar(
            select(ClassMembership).where(
                ClassMembership.classroom_id == class_id,
                ClassMembership.user_id == student_id,
                ClassMembership.role == "student",
            )
        )
        if not member:
            raise AuthorizationError("Resource is outside your authorized scope")
        destination = None
        if destination_id is not None:
            destination = require_coordinator_class(db, actor, destination_id)
            if destination.school_id != source.school_id:
                raise AuthorizationError("Resource is outside your authorized scope")
            require_active_class(destination)
            if destination_id == class_id:
                raise ValidationError("Choose a different destination class.")
        return source, member, destination

    def change_student_class(
        self,
        db: Session,
        actor: User,
        class_id: int,
        student_id: int,
        destination_id: int | None,
        *,
        expected_membership_id: int,
        expected_joined_at: str,
    ) -> None:
        source, member, destination = self.student_class_change(
            db, actor, class_id, student_id, destination_id
        )
        if (
            member.id != expected_membership_id
            or aware_utc(member.created_at).isoformat() != expected_joined_at
        ):
            raise ValidationError("The student’s enrollment changed. Review the change again.")
        added = None
        if destination is not None and (
            db.scalar(
                select(ClassMembership.id).where(
                    ClassMembership.classroom_id == destination_id,
                    ClassMembership.user_id == student_id,
                    ClassMembership.role == "student",
                )
            )
            is None
        ):
            added = ClassMembership(classroom_id=destination_id, user_id=student_id, role="student")
            db.add(added)
            db.flush()
        db.delete(member)
        self._cancel_reminders(db, class_id, "Student left the class", student_id)
        db.add(
            ActivityEvent(
                school_id=source.school_id,
                classroom_id=class_id,
                actor_user_id=actor.id,
                event_type="student_transferred"
                if destination_id is not None
                else "student_removed",
                entity_type="user",
                entity_id=student_id,
                metadata_json={
                    "from_class_id": class_id,
                    "to_class_id": destination_id,
                    "removed_membership_id": member.id,
                    "added_membership_id": added.id if added else None,
                },
            )
        )

    def create_school_student(
        self,
        db: Session,
        actor: User,
        school_id: int,
        name: str,
        email: str,
        temporary_password: str,
    ) -> User:
        require_coordinator_school(db, actor, school_id)
        email = email.strip().lower()
        if db.scalar(select(User.id).where(User.email == email)) is not None:
            raise ValidationError("An account with that email already exists.")
        student = self._create_student(db, name, email, temporary_password)
        membership = SchoolMembership(school_id=school_id, user_id=student.id, role="student")
        db.add(membership)
        db.flush()
        db.add(
            ActivityEvent(
                school_id=school_id,
                actor_user_id=actor.id,
                event_type="student_created",
                entity_type="user",
                entity_id=student.id,
                metadata_json={"school_membership_id": membership.id},
            )
        )
        return student

    @staticmethod
    def _create_student(db: Session, name: str, email: str, temporary_password: str) -> User:
        name = name.strip()
        if not name or len(name) > 160 or "@" not in email or len(email) > 254:
            raise ValidationError("Enter a valid student name and email address.")
        if len(temporary_password) < 10:
            raise ValidationError("Temporary password must be at least 10 characters")
        student = User(name=name, email=email, password_hash=hash_password(temporary_password))
        db.add(student)
        db.flush()
        return student

    def add_student(
        self,
        db: Session,
        actor: User,
        classroom_id: int,
        name: str,
        email: str,
        temporary_password: str,
    ) -> User:
        classroom = require_managed_class(db, actor, classroom_id)
        clean_email = email.strip().lower()
        if not clean_email or "@" not in clean_email or not name.strip():
            raise ValidationError("A valid name and email are required")
        student = db.scalar(select(User).where(User.email == clean_email))
        if student:
            schools = set(
                db.scalars(
                    select(SchoolMembership.school_id).where(
                        SchoolMembership.user_id == student.id,
                        SchoolMembership.role == "student",
                    )
                )
            )
            if schools and classroom.school_id not in schools:
                raise ValidationError("That student belongs to another school")
            if (
                not has_school_role(
                    db, student.id, classroom.school_id, {"student", "teacher", "coordinator"}
                )
                and db.scalar(
                    select(ClassMembership.id)
                    .join(Classroom)
                    .where(
                        ClassMembership.user_id == student.id,
                        Classroom.school_id == classroom.school_id,
                    )
                    .limit(1)
                )
                is None
            ):
                raise AuthorizationError("Resource is outside your authorized scope")
        else:
            student = self._create_student(db, name, clean_email, temporary_password)
        school_membership = db.scalar(
            select(SchoolMembership).where(
                SchoolMembership.school_id == classroom.school_id,
                SchoolMembership.user_id == student.id,
                SchoolMembership.role == "student",
            )
        )
        if not school_membership:
            db.add(
                SchoolMembership(school_id=classroom.school_id, user_id=student.id, role="student")
            )
        membership = db.scalar(
            select(ClassMembership).where(
                ClassMembership.classroom_id == classroom.id,
                ClassMembership.user_id == student.id,
                ClassMembership.role == "student",
            )
        )
        if not membership:
            membership = ClassMembership(
                classroom_id=classroom.id, user_id=student.id, role="student"
            )
            db.add(membership)
            db.flush()
            db.add(
                ActivityEvent(
                    school_id=classroom.school_id,
                    classroom_id=classroom.id,
                    actor_user_id=actor.id,
                    event_type="student_enrolled",
                    entity_type="user",
                    entity_id=student.id,
                    metadata_json={"membership_id": membership.id},
                )
            )
        return student

    def create_invite(
        self,
        db: Session,
        actor: User,
        classroom_id: int,
        expires_in_days: int = 7,
        max_uses: int = 20,
    ) -> Invite:
        classroom = require_managed_class(db, actor, classroom_id)
        if not 1 <= expires_in_days <= 365 or max_uses < 1:
            raise ValidationError("Set an expiry of 1 to 365 days and at least one use.")
        invite = Invite(
            school_id=classroom.school_id,
            classroom_id=classroom.id,
            code=secrets.token_hex(4).upper(),
            expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
            max_uses=max_uses,
            use_count=0,
            active=True,
        )
        db.add(invite)
        return invite

    def disable_invite(self, db: Session, actor: User, invite_id: int) -> Invite:
        invite = db.get(Invite, invite_id)
        if not invite:
            from app.exceptions import NotFoundError

            raise NotFoundError("Invite not found")
        require_managed_class(db, actor, invite.classroom_id)
        invite.active = False
        return invite
