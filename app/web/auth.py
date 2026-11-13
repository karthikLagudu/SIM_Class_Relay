from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import current_user, validate_csrf, verify_password
from app.database import get_db
from app.models import User
from app.services.authorization import workspace_roles
from app.web.demo_accounts import demo_account_groups
from app.web.helpers import WORKSPACES, csrf, render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    try:
        user = current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    roles = workspace_roles(db, user)
    for role in WORKSPACES:
        if role in roles:
            return RedirectResponse(f"/{role}", status_code=303)
    return render(
        request,
        "error.html",
        status_code=403,
        code=403,
        user=user,
        csrf_token=csrf(request),
        detail="You don’t have a workspace yet. Ask your school coordinator to add you.",
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return render_login(request, db)


def render_login(request: Request, db: Session, **context):
    response = render(
        request,
        "login.html",
        demo_groups=demo_account_groups(request.app.state.settings, db, request),
        **context,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/login")
def login(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not verify_password(password, user.password_hash):
        return render_login(
            request,
            db,
            status_code=401,
            error="Check your email and password and try again.",
            email=email,
        )
    if not workspace_roles(db, user):
        return render_login(
            request,
            db,
            status_code=403,
            error="This account has no workspace access. Ask your school coordinator for help.",
            email=email,
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
