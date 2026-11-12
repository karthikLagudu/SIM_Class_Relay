from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import current_user, validate_csrf, verify_password
from app.database import get_db
from app.models import SchoolMembership, User
from app.web.helpers import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    try:
        user = current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    coordinator = db.scalar(
        select(SchoolMembership).where(
            SchoolMembership.user_id == user.id,
            SchoolMembership.role == "coordinator",
        )
    )
    if coordinator:
        return RedirectResponse("/coordinator", status_code=303)
    teacher = db.scalar(
        select(SchoolMembership).where(
            SchoolMembership.user_id == user.id,
            SchoolMembership.role == "teacher",
        )
    )
    return RedirectResponse("/teacher" if teacher else "/student", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "login.html", demo=demo_accounts_available(request, db))


def demo_accounts_available(request: Request, db: Session) -> bool:
    settings = request.app.state.settings
    if settings.environment != "development" or settings.telegram_mode != "log":
        return False
    return db.scalar(select(User.id).where(User.email == "teacher@demo.local")) is not None


@router.post("/login")
def login(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not verify_password(password, user.password_hash):
        return render(
            request, "login.html", status_code=401, error="Check your email and password and try again.",
            email=email, demo=demo_accounts_available(request, db),
        )
    token, _ = request.app.state.sessions.create(user.id)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        request.app.state.sessions.cookie_name,
        token,
        httponly=True,
        secure=request.app.state.sessions.secure,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
    )
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form()):
    validate_csrf(request, csrf_token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(request.app.state.sessions.cookie_name)
    return response
