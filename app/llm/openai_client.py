from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

from app.llm.base import LLMProvider
from app.llm.schemas import (
    AssignmentIntent,
    ProgressIntent,
    RiskSummary,
    StudentIntent,
    TeacherIntent,
)

DEADLINE_RULES = (
    "Use 09:00 for morning, 15:00 for afternoon, and 18:00 for evening or a bare day. "
    "A bare weekday is the next occurrence that is in the future. "
    "'Next Friday' means Friday of the following calendar week (Monday through Sunday). "
    "Ask for clarification if the time lacks AM/PM and is ambiguous or no deadline is given. "
    "Return an ISO datetime with the offset for the supplied school timezone on that date."
)


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        self.client = OpenAI(api_key=api_key, timeout=12, max_retries=0)
        self.model = model

    def _structured(self, system: str, user: str, schema: type):
        response = self.client.responses.parse(
            model=self.model,
            input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            text_format=schema,
            store=False,
        )
        if not response.output_parsed:
            raise ValueError("Model returned no validated output")
        return response.output_parsed

    def interpret_assignment(
        self, text: str, timezone_name: str, now: datetime
    ) -> AssignmentIntent:
        system = (
            "Prepare a teacher-reviewed assignment draft. Write a short title and student-facing instructions. "
            "Fix obvious spelling, grammar, punctuation and formatting. Preserve the original work, names, "
            "question and page numbers, ranges, chapter references, materials, and deliverables. Never guess "
            "a name, number, or missing range endpoint. Preserve the written form of numbers except for range punctuation. "
            "Keep routing and deadlines separate from instructions. Copy any explicit class name into class_reference "
            "and each intended student's name or email into student_targets, verbatim and without guessing. "
            "Leave references empty when the teacher did not name a class or students. Ask for clarification for "
            "ambiguous recipient descriptions, conflicting or uncertain classes, numbers, work, or deadlines. "
            "Copy the exact deadline words from the message into due_expression and resolve them using the supplied "
            "current time and school timezone. Do not invent database identifiers. "
            + DEADLINE_RULES
        )
        return self._structured(
            system,
            f"Now: {now.astimezone(ZoneInfo(timezone_name)).isoformat()}\nTimezone: {timezone_name}\nMessage: {text}",
            AssignmentIntent,
        )

    def interpret_progress(self, text: str) -> ProgressIntent:
        return self._structured(
            "Interpret a student's assignment update as acknowledged, in_progress, or blocked. Preserve their meaning.",
            text,
            ProgressIntent,
        )

    def interpret_teacher(
        self, text: str, timezone_name: str, now: datetime
    ) -> TeacherIntent:
        return self._structured(
            "Classify the teacher's message into the supplied schema. Extract human references only, never IDs unless explicitly written. For named recipients copy their names or emails verbatim into student_targets. Leave it empty only for class-wide work. Resolve relative deadlines from the provided aware current time and IANA timezone. Set requires_clarification when the action, assignment, class, or deadline is unsafe to infer. "
            + DEADLINE_RULES,
            f"Now: {now.astimezone(ZoneInfo(timezone_name)).isoformat()}\nTimezone: {timezone_name}\nMessage: {text}",
            TeacherIntent,
        )

    def interpret_student(self, text: str) -> StudentIntent:
        return self._structured(
            "Classify a student's informal classroom message. Preserve blocker meaning and extract progress percentage. Use submit_text only when the actual work is supplied and copy the submitted work verbatim into message. Use submit_attachment for announcements such as Here is my homework when no work is included. For I am done without work, report progress at 100 percent. Never choose a database ID.",
            text,
            StudentIntent,
        )

    def summarize_risks(self, facts: list[str]) -> str:
        result = self._structured(
            "Write a concise factual classroom risk summary. Do not infer facts not supplied.",
            "\n".join(facts) or "No risks supplied.",
            RiskSummary,
        )
        return result.summary

    def write_reminder(self, reminder_type: str, facts: dict[str, str]) -> str:
        response = self.client.responses.create(
            model=self.model,
            instructions="Write one short supportive classroom reminder using only supplied facts. Never shame the student.",
            store=False,
            input=json.dumps({"type": reminder_type, **facts}),
        )
        return response.output_text.strip()
