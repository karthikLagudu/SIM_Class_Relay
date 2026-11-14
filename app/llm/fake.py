from __future__ import annotations

import re
from datetime import datetime, timedelta

from app.llm.assignment_text import (
    AMBIGUOUS_NUMBER,
    CLASS_QUALIFIER,
    GRADE_REFERENCE,
    MISSING_RANGE_END,
    QUESTION_REFERENCE,
    RECIPIENT_PREFIX,
    normalize_spoken_references,
    parse_known_deadline,
    recipient_references,
    split_deadline,
)
from app.llm.base import LLMProvider
from app.llm.schemas import AssignmentIntent, ProgressIntent, StudentIntent, TeacherIntent

WORK_TYPOS = {
    "qstns": "questions",
    "qns": "questions",
    "qstn": "question",
    "questons": "questions",
    "frm": "from",
    "chpter": "chapter",
    "chptr": "chapter",
    "complet": "complete",
    "compleet": "complete",
    "submt": "submit",
    "pgs": "pages",
    "labled": "labeled",
    "photosyntehsis": "photosynthesis",
    "uplod": "upload",
    "workseet": "worksheet",
}


class DemoLLM(LLMProvider):
    """Deterministic local/demo provider. Production language understanding uses OpenAIProvider."""

    def interpret_assignment(
        self, text: str, timezone_name: str, now: datetime
    ) -> AssignmentIntent:
        clean = text.strip()

        def clarify(question: str) -> AssignmentIntent:
            return AssignmentIntent(
                intent="clarify",
                title="Clarification needed",
                instructions=clean[:5000] if len(clean) >= 3 else "Missing details",
                due_at=now + timedelta(days=1),
                confidence=0.2,
                clarification_question=question,
            )

        if len(clean) < 8 or "???" in clean:
            return clarify("What should students do, and when is it due?")
        work, expression = split_deadline(clean)
        work = normalize_spoken_references(work)
        if missing := MISSING_RANGE_END.search(work):
            return clarify(f"What is the last number in the range starting at {missing[1]}?")
        try:
            due = parse_known_deadline(expression or "", timezone_name, now)
        except ValueError as exc:
            return clarify(
                str(exc) if str(exc).endswith("?") else "What exact date and time should this be due?"
            )
        if due is None:
            return clarify("When is this due? Include a day or date and a time, such as tomorrow at 6 PM.")
        if AMBIGUOUS_NUMBER.search(work):
            return clarify("What exact question, page, or chapter numbers should students use?")
        class_matches = list(GRADE_REFERENCE.finditer(work))
        if len(class_matches) > 1:
            return clarify("Which single class should receive this assignment?")
        class_reference = None
        if class_matches:
            match = class_matches[0]
            suffix = CLASS_QUALIFIER.match(work, match.end())
            end = suffix.end(1) if suffix else match.end()
            class_reference = work[match.start() : end]
            before = re.sub(r"\b(?:for|to)\s*$", "", work[: match.start()], flags=re.IGNORECASE)
            work = before + " " + work[end:].lstrip(" :,-")
        student_targets = []
        addressed = RECIPIENT_PREFIX.match(work)
        if addressed:
            student_targets = recipient_references(addressed)
            work = work[addressed.end() :]
        work = re.sub(r"^(?:give|gve|assign)\b\s*", "", work, flags=re.IGNORECASE).strip(" :,-")
        work = re.sub(r"[^\S\n]+", " ", work)
        work = re.sub(r"(?<!\n)\n(?![ \t]*(?:\n|[-*] |\d+[.)] ))", " ", work)
        work = re.sub(r" {2,}", " ", work)
        work = re.sub(
            r"\b\w+\b",
            lambda match: WORK_TYPOS.get(
                match[0].lower() if match.start() == 0 else match[0], match[0]
            ),
            work,
        )
        work = re.sub(r"\bch\.?(?=\s+\d)", "Chapter", work, flags=re.IGNORECASE)
        work = re.sub(
            r"(\b(?:questions|pages|exercises)\s+\d+)\s*(?:\bto\b|[-–])\s*(?=\d)",
            r"\1–",
            work,
            flags=re.IGNORECASE,
        )
        work = re.sub(r"\bchapter(?=\s+\d)", "Chapter", work, flags=re.IGNORECASE)
        if re.match(r"^(?:questions?|pages?|the worksheet)\b", work, re.IGNORECASE):
            work = "Complete " + work
        if len(work) < 3:
            return clarify("What should students complete or turn in?")
        instructions = work[0].upper() + work[1:]
        if instructions[-1] not in ".!?":
            instructions += "."
        first_sentence = re.split(r"[.!?]\s+|\n", instructions, maxsplit=1)[0]
        title = re.sub(r"^(?:Complete|Do|Finish)\s+", "", first_sentence)
        title = title.rstrip(".!?")
        if QUESTION_REFERENCE.match(title):
            title = "Selected questions"
        if len(title) > 72:
            title = title[:72].rsplit(" ", 1)[0] or title[:72]
        return AssignmentIntent(
            intent="create_assignment",
            title=title[:1].upper() + title[1:] if len(title) >= 3 else "Class assignment",
            instructions=instructions,
            due_at=due,
            confidence=0.92,
            class_reference=class_reference,
            student_targets=student_targets,
            due_expression=expression,
        )

    def interpret_progress(self, text: str) -> ProgressIntent:
        lower = text.lower()
        percent_match = re.search(r"(\d{1,3})\s*%", lower)
        percent = min(100, int(percent_match.group(1))) if percent_match else None
        if any(word in lower for word in ("blocked", "stuck", "help")):
            status = "blocked"
        elif any(word in lower for word in ("started", "progress", "done", "%")):
            status = "in_progress"
        else:
            status = "acknowledged"
        return ProgressIntent(status=status, message=text, progress_percent=percent, confidence=0.9)

    def interpret_teacher(
        self, text: str, timezone_name: str, now: datetime
    ) -> TeacherIntent:
        clean = " ".join(text.split())
        lower = clean.lower()
        if "who needs attention" in lower or "at risk" in lower:
            return TeacherIntent(intent="risk_summary", confidence=0.96)
        if lower.startswith("how is ") or "class status" in lower:
            return TeacherIntent(intent="class_status", class_reference=re.sub(r"\s+doing[?.!]*$", "", clean[7:], flags=re.IGNORECASE), confidence=0.9)
        if lower.startswith("cancel "):
            reference = re.sub(r"^cancel\s+(?:today(?:'s)?\s+)?", "", clean, flags=re.IGNORECASE)
            reference = re.sub(r"\s+(?:homework|assignment)$", "", reference, flags=re.IGNORECASE)
            return TeacherIntent(
                intent="cancel_assignment", assignment_reference=reference, confidence=0.92
            )
        if lower.startswith("clarify ") or "instructions" in lower:
            before, _, after = clean.partition(":")
            reference = re.sub(r"^clarify\s+", "", before, flags=re.IGNORECASE)
            return TeacherIntent(
                intent="update_assignment_instructions",
                assignment_reference=reference,
                instructions=after.strip() or clean,
                confidence=0.91,
            )
        if lower.startswith(("move ", "actually ")) or "until " in lower:
            due_intent = self.interpret_assignment(f"Temporary task by {clean}", timezone_name, now)
            match = re.search(r"move\s+(?:the\s+)?(.+?)\s+to\s+", clean, re.IGNORECASE)
            reference = match.group(1) if match else ""
            return TeacherIntent(
                intent="update_assignment_deadline",
                assignment_reference=reference or None,
                due_expression=clean,
                due_at=due_intent.due_at,
                confidence=0.9,
            )
        assignment = self.interpret_assignment(clean, timezone_name, now)
        return TeacherIntent(
            intent="create_assignment",
            class_reference=assignment.class_reference,
            student_targets=assignment.student_targets,
            title=assignment.title,
            instructions=assignment.instructions,
            due_expression=clean,
            due_at=assignment.due_at,
            confidence=0.92,
        )

    def interpret_student(self, text: str) -> StudentIntent:
        clean = " ".join(text.split())
        lower = clean.lower()
        percent_match = re.search(r"(\d{1,3})\s*%", lower)
        percent = min(100, int(percent_match.group(1))) if percent_match else None
        if "halfway" in lower or "half way" in lower:
            percent = 50
        if lower in {"status", "what is due", "what's due"}:
            return StudentIntent(intent="status", message=clean, confidence=0.95)
        if any(term in lower for term in ("stuck", "blocked", "can't", "cannot")):
            return StudentIntent(
                intent="blocked",
                message=clean,
                block_reason=clean,
                confidence=0.95,
            )
        if "help" in lower:
            return StudentIntent(intent="ask_for_help", message=clean, confidence=0.9)
        if any(term in lower for term in ("here's my homework", "here is my homework", "submit")):
            return StudentIntent(intent="submit_attachment", message=clean, confidence=0.9)
        if lower in {"got it", "got it.", "okay", "ok", "i understand"}:
            return StudentIntent(intent="acknowledge", message=clean, confidence=0.96)
        if percent is not None or any(term in lower for term in ("started", "working", "done")):
            return StudentIntent(
                intent="progress_update",
                message=clean,
                progress_percent=percent,
                confidence=0.92,
            )
        return StudentIntent(
            intent="unknown",
            message=clean,
            confidence=0.3,
            requires_clarification=True,
            clarification_question="Are you sharing progress, asking for help, or submitting work?",
        )

    def summarize_risks(self, facts: list[str]) -> str:
        return "No active risks." if not facts else " ".join(facts)

    def write_reminder(self, reminder_type: str, facts: dict[str, str]) -> str:
        title = facts["title"]
        due = facts["due"]
        if reminder_type == "blocked_support":
            return f"You marked '{title}' as blocked. Your teacher has been alerted. Reply with what would help; it is due {due}."
        if reminder_type == "silent_due_soon":
            return f"Quick check-in: we have not heard an update on '{title}', due {due}. Reply with your progress or /blocked if you need help."
        if reminder_type in {"gentle_due_soon", "due_within_2h"}:
            return (
                f"Reminder: '{title}' is due {due}. Submit with /submit or share a progress update."
            )
        return f"'{title}' is overdue. Please submit or tell your teacher what is blocking you."
