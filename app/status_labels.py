from __future__ import annotations

STUDENT_STATUS = {
    "assigned": "To acknowledge",
    "acknowledged": "Acknowledged",
    "in_progress": "In progress",
    "blocked": "Help requested",
    "submitted": "Submitted",
    "needs_revision": "Revision requested",
    "completed": "Complete",
    "overdue": "Overdue",
    "cancelled": "Cancelled",
}

DELIVERY_STATUS = {
    "pending": "Queued",
    "sent": "Sent",
    "logged": "Simulated",
    "skipped": "Skipped",
    "failed": "Failed",
}


def student_status(value: str) -> str:
    return STUDENT_STATUS.get(value, value.replace("_", " ").capitalize())


def delivery_status(value: str) -> str:
    return DELIVERY_STATUS.get(value, value.replace("_", " ").capitalize())


def reminder_status(value: str, processed: bool, delivery: str | None = None) -> str:
    if value == "pending" and not processed:
        return "Scheduled"
    return delivery_status(value if value == "cancelled" else delivery or value)
