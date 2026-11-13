from openai import OpenAI, OpenAIError


class TranscriptionError(Exception):
    pass


class OpenAISpeechProvider:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        try:
            with OpenAI(api_key=self.api_key, timeout=25, max_retries=0) as client:
                result = client.audio.transcriptions.create(
                    model=self.model,
                    file=(filename, audio, content_type),
                    response_format="json",
                    prompt=(
                        "Transcribe this teacher's assignment as spoken. Preserve names, class "
                        "sections, question and page numbers, and deadline wording."
                    ),
                )
        except OpenAIError as exc:
            raise TranscriptionError(
                "Transcription failed. Try recording again or type the work."
            ) from exc
        return result.text.strip()
