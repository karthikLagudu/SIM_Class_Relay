from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import validate_csrf
from app.database import get_db
from app.exceptions import AuthorizationError, ValidationError
from app.models import ClassMembership, Classroom, School, User
from app.services.authorization import (
    coordinator_class_scope,
    require_coordinator_class,
)
from app.services.classroom import ClassroomService
from app.services.staffing import apply_staffing, staffing_plan
from app.services.workload import class_workloads, school_teachers, school_users, teacher_directory
from app.web.helpers import coordinator_school, csrf, redirect_with_message, render, web_user
from app.web.school_activity import school_activity

router = APIRouter()


def _school_classes(db: Session, school_id: int) -> list[Classroom]:
    return list(
        db.scalars(
            select(Classroom)
            .where(Classroom.school_id == school_id)
            .order_by(Classroom.archived_at.is_not(None), Classroom.name, Classroom.id)
        )
    )


@router.get("/coordinator/schools/{school_id}/teachers/{teacher_id}", response_class=HTMLResponse)
def teacher_detail(
    request: Request,
    school_id: int,
    teacher_id: int,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    school = coordinator_school(request, db, user, school_id)
    teacher = next((item for item in school_teachers(db, school_id) if item.id == teacher_id), None)
    if not teacher:
        raise AuthorizationError("Resource is outside your authorized scope")
    classes = _school_classes(db, school_id)
    row = teacher_directory([teacher], class_workloads(db, classes))[0]
    return render(
        request,
        "coordinator/teacher.html",
        school=school,
        row=row,
        classes=[item for item in classes if not item.archived_at],
        selected_ids={item.classroom.id for item in row["classes"]},
        message=request.query_params.get("message"),
    )


def _staffing_token(request: Request) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        request.app.state.settings.session_secret, salt="coordinator-staffing-v1"
    )


def _staffing_destination(plan: dict) -> str:
    if plan["class_id"] is not None:
        return f"/coordinator/classes/{plan['class_id']}"
    return f"/coordinator/schools/{plan['school_id']}/teachers/{plan['teacher_id']}"


@router.post("/coordinator/staffing/preview", response_class=HTMLResponse)
def preview_staffing(
    request: Request,
    school_id: int = Form(),
    class_id: int | None = Form(None),
    teacher_id: int | None = Form(None),
    selected_ids: list[int] = Form(default=[]),
    revoke: bool = Form(False),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    school = coordinator_school(request, db, user, school_id)
    plan = staffing_plan(
        db, user, school_id, selected_ids, class_id=class_id, teacher_id=teacher_id, revoke=revoke
    )
    return render(
        request,
        "coordinator/staffing.html",
        plan=plan,
        token=_staffing_token(request).dumps(plan),
        school=school,
        classroom=db.get(Classroom, class_id) if class_id else None,
        classes={item.id: item for item in _school_classes(db, school_id)},
        teachers={item.id: item for item in school_teachers(db, school_id)},
        back_url=_staffing_destination(plan),
    )


@router.post("/coordinator/staffing/confirm")
def confirm_staffing(
    request: Request,
    plan_token: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    try:
        plan = _staffing_token(request).loads(plan_token, max_age=15 * 60)
    except BadSignature as exc:
        raise ValidationError(
            "This staffing preview expired or is invalid. Open the class or teacher and review the change again."
        ) from exc
    if plan["actor_id"] != user.id:
        raise AuthorizationError("Resource is outside your authorized scope")
    coordinator_school(request, db, user, plan["school_id"])
    destination = _staffing_destination(plan)
    try:
        apply_staffing(db, user, plan)
    except ValidationError as exc:
        db.rollback()
        return render(
            request,
            "error.html",
            status_code=409,
            code=409,
            detail=str(exc),
            recovery_url=destination,
            recovery_label="Return to staffing",
            school=db.get(School, plan["school_id"]),
            classroom=db.get(Classroom, plan["class_id"]) if plan["class_id"] else None,
        )
    db.commit()
    if plan["revoke"]:
        return redirect_with_message(
            f"/coordinator?school_id={plan['school_id']}",
            "Teaching access removed. Other roles and classroom history were preserved.",
            anchor="teacher-roster",
        )
    return redirect_with_message(
        destination, "Teaching assignments saved. Access has been updated."
    )


@router.get("/coordinator", response_class=HTMLResponse)
def coordinator_dashboard(
    request: Request,
    school_id: int | None = None,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return _render_school(request, user, db, school_id)


def _render_school(request, user, db, school_id=None, *, errors=None, form=None):
    schools = getattr(request.state, "managed_schools", ())
    if not schools:
        raise HTTPException(403, "Use a coordinator account to open the school overview.")
    school = coordinator_school(
        request, db, user, schools[0].id if school_id is None else school_id
    )
    classes = _school_classes(db, school.id)
    teachers = school_teachers(db, school.id)
    workloads = class_workloads(db, classes)
    directory = teacher_directory(teachers, workloads)
    query = request.query_params.get("q", "").strip()
    filter_by = request.query_params.get("class_filter", "")
    if filter_by and filter_by not in {
        "unassigned",
        *(str(classroom.id) for classroom in classes if not classroom.archived_at),
    }:
        raise ValidationError("Choose a class from this school or select Unassigned.")
    directory = [
        row
        for row in directory
        if query.casefold() in row["teacher"].name.casefold()
        and (
            not filter_by
            or (filter_by == "unassigned" and not row["classes"])
            or any(str(item.classroom.id) == filter_by for item in row["classes"])
        )
    ]
    return render(
        request,
        "coordinator/dashboard.html",
        user=user,
        school=school,
        classes=classes,
        teachers=teachers,
        student_count=len(school_users(db, school.id, "student")),
        pending_reviews=sum(len(item.reviews) for item in workloads.values()),
        workloads=workloads,
        recent_assignments=sorted(
            (assignment for item in workloads.values() for assignment in item.assignments),
            key=lambda item: (item.updated_at, item.id),
            reverse=True,
        )[:8],
        directory=directory,
        query=query,
        filter_by=filter_by,
        active_classes=[item for item in classes if not item.archived_at],
        can_link=request.app.state.telegram_link_service.can_generate_links,
        activity=school_activity(db, school.id),
        csrf_token=csrf(request),
        message=request.query_params.get("message"),
        errors=errors or {},
        form=form or {},
        status_code=422 if errors else 200,
    )


@router.post("/coordinator/schools/{school_id}/edit")
def edit_school(
    request: Request,
    school_id: int,
    name: str = Form(),
    timezone: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    coordinator_school(request, db, user, school_id)
    try:
        ClassroomService().edit_school(db, user, school_id, name, timezone)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_school(
            request,
            user,
            db,
            school_id,
            errors={"school": str(exc)},
            form={"school_name": name, "timezone": timezone},
        )
    return redirect_with_message(
        f"/coordinator?school_id={school_id}", "School details saved.", anchor="school-settings"
    )


@router.get("/coordinator/schools/{school_id}/students", response_class=HTMLResponse)
def student_directory(
    request: Request,
    school_id: int,
    q: str = "",
    class_filter: str = "",
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    school = coordinator_school(request, db, user, school_id)
    return _render_students(request, db, school, q=q, class_filter=class_filter)


def _render_students(request, db, school, *, q="", class_filter="", error=None, form=None):
    classes = {item.id: item for item in _school_classes(db, school.id) if not item.archived_at}
    if class_filter and class_filter not in {"unassigned", *(str(item) for item in classes)}:
        raise ValidationError("Choose a class from this school or select Unassigned.")
    enrollments = defaultdict(list)
    for member in db.scalars(
        select(ClassMembership)
        .where(
            ClassMembership.classroom_id.in_(classes),
            ClassMembership.role == "student",
        )
        .order_by(ClassMembership.classroom_id)
    ):
        enrollments[member.user_id].append(classes[member.classroom_id])
    query = q.strip().casefold()
    students = [
        student
        for student in school_users(db, school.id, "student")
        if (not query or query in student.name.casefold() or query in student.email.casefold())
        and (
            not class_filter
            or (class_filter == "unassigned" and not enrollments[student.id])
            or any(str(item.id) == class_filter for item in enrollments[student.id])
        )
    ]
    return render(
        request,
        "coordinator/students.html",
        school=school,
        students=students,
        enrollments=enrollments,
        classes=list(classes.values()),
        query=q.strip(),
        filter_by=class_filter,
        error=error,
        form=form or {},
        status_code=422 if error else 200,
        message=request.query_params.get("message"),
    )


@router.post("/coordinator/schools/{school_id}/students")
def create_student(
    request: Request,
    school_id: int,
    name: str = Form(),
    email: str = Form(),
    temporary_password: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    school = coordinator_school(request, db, user, school_id)
    try:
        student = ClassroomService().create_school_student(
            db, user, school_id, name, email, temporary_password
        )
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_students(
            request, db, school, error=str(exc), form={"name": name, "email": email}
        )
    return redirect_with_message(
        f"/coordinator/schools/{school_id}/students",
        f"{student.name} added. Choose a class to enroll them.",
        anchor=f"student-{student.id}",
    )


@router.post("/coordinator/schools/{school_id}/students/{student_id}/enroll")
def enroll_student(
    request: Request,
    school_id: int,
    student_id: int,
    class_id: int = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    coordinator_school(request, db, user, school_id)
    classroom = require_coordinator_class(db, user, class_id)
    if classroom.school_id != school_id:
        raise AuthorizationError("Resource is outside your authorized scope")
    student = next(
        (item for item in school_users(db, school_id, "student") if item.id == student_id), None
    )
    if not student:
        raise AuthorizationError("Resource is outside your authorized scope")
    ClassroomService().add_student(db, user, class_id, student.name, student.email, "")
    db.commit()
    return redirect_with_message(
        f"/coordinator/schools/{school_id}/students",
        f"{student.name} is enrolled in {classroom.name}.",
        anchor=f"student-{student.id}",
    )


def _student_removal_token(request: Request) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        request.app.state.settings.session_secret, salt="coordinator-student-school-removal-v1"
    )


@router.get(
    "/coordinator/schools/{school_id}/students/{student_id}/remove", response_class=HTMLResponse
)
def preview_student_school_removal(
    request: Request,
    school_id: int,
    student_id: int,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    school = coordinator_school(request, db, user, school_id)
    plan = ClassroomService().student_school_removal(db, user, school_id, student_id)
    directory_url = f"/coordinator/schools/{school_id}/students"
    if not plan.present:
        return redirect_with_message(directory_url, "This student was already removed from this school.")
    token = _student_removal_token(request).dumps(
        {"actor": user.id, "school": school_id, "student": student_id, "snapshot": plan.snapshot()}
    )
    return render(
        request,
        "coordinator/remove_student.html",
        school=school,
        student=plan.student,
        classes=[classroom for _, classroom in plan.enrollments],
        token=token,
        directory_url=directory_url,
    )


@router.post("/coordinator/schools/{school_id}/students/{student_id}/remove")
def confirm_student_school_removal(
    request: Request,
    school_id: int,
    student_id: int,
    csrf_token: str = Form(),
    removal_token: str = Form(),
    confirmed: bool = Form(default=False),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    coordinator_school(request, db, user, school_id)
    if not confirmed:
        raise ValidationError("Confirm the removal by checking the box on the review page.")
    try:
        plan = _student_removal_token(request).loads(removal_token, max_age=15 * 60)
    except BadSignature as exc:
        raise ValidationError("This confirmation expired or is invalid. Open the student directory and review the removal again.") from exc
    if (plan["actor"], plan["school"], plan["student"]) != (user.id, school_id, student_id):
        raise AuthorizationError("Resource is outside your authorized scope")
    message = ClassroomService().remove_student_from_school(
        db, user, school_id, student_id, plan["snapshot"], request.app.state.telegram_link_service
    )
    db.commit()
    return redirect_with_message(f"/coordinator/schools/{school_id}/students", message)


@router.post("/coordinator/classes")
def add_class(
    request: Request,
    school_id: int = Form(),
    name: str = Form(),
    grade: str = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    coordinator_school(request, db, user, school_id)
    try:
        classroom = ClassroomService().create_classroom(db, user, school_id, name, grade)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_school(
            request,
            user,
            db,
            school_id,
            errors={"class": str(exc)},
            form={"name": name, "grade": grade},
        )
    return redirect_with_message(
        f"/coordinator?school_id={school_id}",
        f"{classroom.name} created. Add students or set up a teacher next.",
        anchor=f"class-{classroom.id}",
    )


@router.post("/coordinator/teachers")
def add_teacher(
    request: Request,
    school_id: int = Form(),
    name: str = Form(),
    email: str = Form(),
    temporary_password: str = Form(),
    csrf_token: str = Form(),
    classroom_ids: list[int] = Form(default=[]),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    coordinator_school(request, db, user, school_id)
    try:
        teacher = ClassroomService().add_teacher(
            db,
            user,
            school_id,
            classroom_ids,
            name,
            email,
            temporary_password,
        )
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_school(
            request,
            user,
            db,
            school_id,
            errors={"teacher": str(exc)},
            form={"name": name, "email": email, "classroom_ids": classroom_ids},
        )
    return redirect_with_message(
        f"/coordinator?school_id={school_id}",
        f"{teacher.name} can now sign in. Share the temporary password privately.",
        anchor="teacher-roster",
    )


@router.post("/coordinator/reminders/run")
def run_school_reminders(
    request: Request,
    school_id: int = Form(),
    csrf_token: str = Form(),
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf_token)
    coordinator_school(request, db, user, school_id)
    class_ids = set(
        db.scalars(coordinator_class_scope(user.id).where(Classroom.school_id == school_id))
    )
    reminders = (
        request.app.state.reminder_service.run(db, classroom_ids=class_ids) if class_ids else []
    )
    db.commit()
    message = (
        f"Processed {len(reminders)} reminder jobs."
        if reminders
        else "No reminder jobs are due for this school."
    )
    return redirect_with_message(
        f"/coordinator?school_id={school_id}", message, anchor="school-reminders"
    )
