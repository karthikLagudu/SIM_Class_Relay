from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.security import validate_csrf
from app.database import get_db
from app.exceptions import AuthorizationError, ValidationError
from app.models import ClassMembership, Classroom, School, SchoolMembership, User
from app.services.authorization import has_school_role
from app.services.classroom import ClassroomService
from app.web.helpers import csrf, redirect_with_message, render, web_user

router = APIRouter()


@router.get("/coordinator", response_class=HTMLResponse)
def coordinator_dashboard(
    request: Request,
    school_id: int | None = None,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return _render_school(request, user, db, school_id)


def _render_school(request, user, db, school_id=None, *, errors=None, form=None):
    schools = list(
        db.scalars(
            select(School)
            .join(SchoolMembership, SchoolMembership.school_id == School.id)
            .where(SchoolMembership.user_id == user.id, SchoolMembership.role == "coordinator")
            .order_by(School.name)
        )
    )
    if not schools:
        raise HTTPException(403, "Use a coordinator account to open the school overview.")
    school = (
        schools[0]
        if school_id is None
        else next((item for item in schools if item.id == school_id), None)
    )
    if not school:
        raise AuthorizationError("Resource is outside your authorized scope")
    classes = list(
        db.scalars(
            select(Classroom).where(Classroom.school_id == school.id).order_by(Classroom.name)
        )
    )
    teachers = list(
        db.scalars(
            select(User)
            .join(SchoolMembership, SchoolMembership.user_id == User.id)
            .where(SchoolMembership.school_id == school.id, SchoolMembership.role == "teacher")
            .order_by(User.name)
        )
    )
    class_ids = [item.id for item in classes]
    counts = dict(
        db.execute(
            select(ClassMembership.classroom_id, func.count())
            .where(ClassMembership.classroom_id.in_(class_ids), ClassMembership.role == "student")
            .group_by(ClassMembership.classroom_id)
        ).all()
    )
    teacher_classes = {}
    for user_id, classroom in db.execute(
        select(ClassMembership.user_id, Classroom)
        .join(Classroom, Classroom.id == ClassMembership.classroom_id)
        .where(Classroom.school_id == school.id, ClassMembership.role == "teacher")
        .order_by(Classroom.name)
    ):
        teacher_classes.setdefault(user_id, []).append(classroom)
    return render(
        request,
        "coordinator/dashboard.html",
        user=user,
        school=school,
        schools=schools,
        classes=classes,
        teachers=teachers,
        counts=counts,
        teacher_classes=teacher_classes,
        csrf_token=csrf(request),
        message=request.query_params.get("message"),
        errors=errors or {},
        form=form or {},
        status_code=422 if errors else 200,
    )


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
    if not has_school_role(db, user.id, school_id, {"coordinator"}):
        raise AuthorizationError("Resource is outside your authorized scope")
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
