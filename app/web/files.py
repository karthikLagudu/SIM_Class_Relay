from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.exceptions import AuthorizationError, NotFoundError
from app.models import Submission, User
from app.services.authorization import (
    require_staff_submission,
    require_student_submission,
    require_teacher_submission,
)
from app.web.helpers import web_user

router = APIRouter()
INLINE_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


@router.get("/submissions/{submission_id}/file")
def submission_file(
    request: Request,
    submission_id: int,
    download: bool = False,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    try:
        submission = require_teacher_submission(db, user, submission_id)
    except (AuthorizationError, NotFoundError):
        submission = require_student_submission(db, user, submission_id)
    return _send_file(request, submission, download)


@router.get("/teacher/submissions/{submission_id}/file")
def teacher_file(
    request: Request,
    submission_id: int,
    download: bool = False,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return _send_file(request, require_teacher_submission(db, user, submission_id), download)


@router.get("/coordinator/submissions/{submission_id}/file")
def coordinator_file(
    request: Request,
    submission_id: int,
    download: bool = False,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return _send_file(
        request, require_staff_submission(db, user, submission_id, coordinator=True), download
    )


@router.get("/student/submissions/{submission_id}/file")
def student_file(
    request: Request,
    submission_id: int,
    download: bool = False,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    return _send_file(request, require_student_submission(db, user, submission_id), download)


def _send_file(request: Request, submission: Submission, download: bool):
    return send_private_file(
        request, submission.stored_file_path, submission.original_filename,
        submission.mime_type, download,
    )


def send_private_file(
    request: Request, stored_path: str | None, filename: str | None,
    mime_type: str | None, download: bool,
):
    if not stored_path:
        raise HTTPException(404, "Submission file is unavailable")
    root = request.app.state.settings.upload_dir.resolve()
    path = Path(stored_path).resolve()
    if root != path and root not in path.parents:
        raise HTTPException(404, "Submission file is unavailable")
    if not path.is_file():
        raise HTTPException(404, "Submission file is unavailable")
    inline = mime_type in INLINE_IMAGE_TYPES
    return FileResponse(
        path,
        media_type=mime_type if inline else "application/octet-stream",
        filename=filename or path.name,
        content_disposition_type="inline" if inline and not download else "attachment",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
