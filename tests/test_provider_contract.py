from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx2 as httpx
from openai import OpenAI
from sqlalchemy import select

from app.exceptions import LLMInterpretationError
from app.llm.fake import DemoLLM
from app.llm.openai_client import OpenAIProvider
from app.llm.service import LLMService
from app.models import LLMInteraction


def test_official_openai_sdk_parses_real_responses_contract():
    def transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert payload["store"] is False
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["text"]["format"]["strict"] is True
        assert "2026-08-04T11:30:00+05:30" in payload["input"][1]["content"]
        intent = {
            "intent": "create_assignment",
            "title": "Energy worksheet",
            "instructions": "Complete questions 1–10",
            "due_at": "2026-08-05T18:00:00+05:30",
            "confidence": 0.95,
            "clarification_question": None,
        }
        return httpx.Response(
            200,
            json={
                "id": "resp_mock",
                "object": "response",
                "status": "completed",
                "created_at": 0,
                "model": "gpt-5-mini",
                "output": [
                    {
                        "id": "msg_mock",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": json.dumps(intent), "annotations": []}
                        ],
                    }
                ],
            },
        )

    provider = OpenAIProvider("offline-test-key", "gpt-5-mini")
    provider.client.close()
    with OpenAI(
        api_key="offline-test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    ) as sdk:
        provider.client = sdk
        result = provider.interpret_assignment(
            "Complete the energy worksheet by tomorrow evening",
            "Asia/Kolkata",
            datetime(2026, 8, 4, 6, tzinfo=UTC),
        )
        assert result.title == "Energy worksheet"
        assert result.due_at.astimezone(UTC) == datetime(2026, 8, 5, 12, 30, tzinfo=UTC)


def test_progress_provider_failure_is_logged_without_mutation(db, data):
    import pytest

    class BrokenProvider(DemoLLM):
        def interpret_progress(self, text):
            raise TimeoutError("internal provider detail")

    actor = data["student1"]
    with pytest.raises(LLMInterpretationError, match="clarify"):
        LLMService(BrokenProvider()).progress(db, actor.id, "I am halfway done")
    interaction = db.scalar(select(LLMInteraction))
    assert (
        interaction.operation,
        interaction.actor_user_id,
        interaction.success,
        interaction.error,
    ) == (
        "progress",
        actor.id,
        False,
        "TimeoutError",
    )
