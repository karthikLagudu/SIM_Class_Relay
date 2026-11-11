from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.exceptions import AuthorizationError, NotFoundError
from app.models import User
from app.services.authorization import require_student_submission, require_teacher_submission
from app.web.helpers import web_user

router = APIRouter()


@router.get("/submissions/{submission_id}/file")
def submission_file(
    request: Request,
    submission_id: int,
    user: User = Depends(web_user),
    db: Session = Depends(get_db),
):
    try:
        submission = require_teacher_submission(db, user, submission_id)
    except (AuthorizationError, NotFoundError):
        submission = require_student_submission(db, user, submission_id)
    if not submission.stored_file_path:
        raise HTTPException(404, "Submission file is unavailable")
    root = request.app.state.settings.upload_dir.resolve()
    path = Path(submission.stored_file_path).resolve()
    if root != path and root not in path.parents:
        raise HTTPException(404, "Submission file is unavailable")
    if not path.is_file():
        raise HTTPException(404, "Submission file is unavailable")
    previews = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    inline = submission.mime_type in previews
    return FileResponse(
        path,
        media_type=submission.mime_type if inline else "application/octet-stream",
        filename=submission.original_filename or path.name,
        content_disposition_type="inline" if inline else "attachment",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
