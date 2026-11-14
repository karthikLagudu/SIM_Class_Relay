from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import LLMInterpretationError
from app.llm.assignment_text import (
    AMBIGUOUS_NUMBER,
    GRADE_REFERENCE,
    MISSING_RANGE_END,
    QUESTION_REFERENCE,
    RECIPIENT_PREFIX,
    normalize_spoken_references,
    parse_known_deadline,
    recipient_references,
    reference_words,
    split_deadline,
)
from app.llm.schemas import AssignmentIntent
from app.models import ClassMembership, Classroom, School, User


def class_students(db: Session, classroom_id: int) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .join(ClassMembership, ClassMembership.user_id == User.id)
            .where(ClassMembership.classroom_id == classroom_id, ClassMembership.role == "student")
            .order_by(User.name)
        )
    )


def _take_source_phrase(text: str, phrase: str) -> str | None:
    words = r"\s+".join(re.escape(word) for word in phrase.split())
    if not words:
        return None
    match = re.search(rf"(?<!\w){words}(?!\w)", text, re.IGNORECASE)
    return text[: match.start()] + " " + text[match.end() :] if match else None


def class_reference_matches(classroom: Classroom, reference: str) -> bool:
    name = reference_words(classroom.name)
    short_name = re.match(r"grade \d+ [a-z]\b", name)
    allowed = {name}
    if short_name:
        allowed.update((short_name[0], short_name[0].removeprefix("grade ")))
    return reference_words(reference) in allowed


def resolve_natural_draft(
    parsed: AssignmentIntent,
    source: str,
    classroom: Classroom,
    school: School,
    students: list[User],
    selected_ids: list[int] | None,
    now: datetime,
) -> tuple[datetime, list[int] | None]:
    work = source
    expression = parsed.due_expression
    if not expression or (work := _take_source_phrase(work, expression)) is None:
        raise LLMInterpretationError("What day and time should the assignment be due?")
    try:
        _, source_deadline = split_deadline(source)
        known = parse_known_deadline(source_deadline or expression, school.timezone, now)
        local = parsed.due_at.astimezone(ZoneInfo(school.timezone))
        deadline = local.astimezone(UTC)
        if local.utcoffset() != local.replace(fold=1 - local.fold).utcoffset():
            raise ValueError
        if local.utcoffset() != parsed.due_at.utcoffset() or (known and deadline != known):
            raise ValueError
    except ValueError as exc:
        if str(exc).endswith("?"):
            raise LLMInterpretationError(str(exc)) from exc
        raise LLMInterpretationError(
            "What exact date and time should the assignment be due in the school’s time zone?"
        ) from exc
    if deadline <= now:
        raise LLMInterpretationError(
            "The deadline is in the past. What future date and time should I use?"
        )

    # The class selector sets scope. A conflicting name in the text must never silently reroute it.
    if parsed.class_reference:
        remaining = _take_source_phrase(work, parsed.class_reference)
        if not class_reference_matches(classroom, parsed.class_reference) or remaining is None:
            raise LLMInterpretationError(
                f"Did you mean {classroom.name}? Your wording names “{parsed.class_reference}”. "
                "Change the wording or start the draft in the intended class."
            )
        work = remaining
    if GRADE_REFERENCE.search(work):
        raise LLMInterpretationError(
            f"Is {classroom.name} the intended class? Use its full name or leave the class to the selector."
        )

    if addressed := RECIPIENT_PREFIX.match(work.strip()):
        extracted = {target.strip().casefold() for target in parsed.student_targets}
        explicit = {name.casefold() for name in recipient_references(addressed)}
        if not explicit or extracted != explicit:
            raise LLMInterpretationError(
                "Who should receive the assignment? Keep each intended student’s full name or email in the wording."
            )
    named_ids = set()
    for target in parsed.student_targets:
        name_or_email = target.strip().casefold()
        matches = [
            student.id
            for student in students
            if name_or_email in {student.name.casefold(), student.email.casefold()}
        ]
        work = _take_source_phrase(work, target)
        if len(matches) != 1 or work is None:
            raise LLMInterpretationError(
                f"Which student did you mean by “{target}”? Use their full name or email in {classroom.name}."
            )
        named_ids.add(matches[0])
    if named_ids and selected_ids is not None and set(selected_ids) != named_ids:
        named = ", ".join(student.name for student in students if student.id in named_ids)
        selected = ", ".join(student.name for student in students if student.id in selected_ids)
        raise LLMInterpretationError(
            f"Who should receive the assignment? Your wording names {named}. "
            f"Selected recipients: {selected}. Update your wording or the checkboxes to match."
        )
    if missing := MISSING_RANGE_END.search(normalize_spoken_references(split_deadline(source)[0])):
        raise LLMInterpretationError(
            f"What is the last number in the range starting at {missing[1]}?"
        )
    work = normalize_spoken_references(work)
    if (
        AMBIGUOUS_NUMBER.search(work)
        or re.findall(r"\d+", work)
        != re.findall(r"\d+", normalize_spoken_references(parsed.instructions))
        or QUESTION_REFERENCE.findall(work) != QUESTION_REFERENCE.findall(parsed.instructions)
    ):
        raise LLMInterpretationError(
            "What exact question, page, or chapter numbers should students use? "
            "Check the numbers in your description."
        )
    words = re.findall(r"\b[^\W\d_][\w’'-]*\b", work)
    cleaned = {word.casefold() for word in re.findall(r"\b[^\W\d_][\w’'-]*\b", parsed.instructions)}
    for word in words[1:]:
        if word[0].isupper() and word.casefold() not in cleaned:
            raise LLMInterpretationError(
                f"Should “{word}” stay exactly as written in the instructions?"
            )
    return deadline, sorted(named_ids) if named_ids else selected_ids
