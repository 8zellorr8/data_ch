"""Gemini adapter for the backend Evaluator contract."""

import asyncio
import json
from collections.abc import Callable

import httpx
from pydantic import ValidationError

from backend.errors import DomainError, InvalidEvaluationResult
from backend.evaluator import EvaluationContext, EvaluatorFailure, EvaluatorOutput
from backend.models import EvaluationResult
from backend.scoring import validate_result
from backend.work_models import ProviderUsage

from .prompt import GEMINI_PROMPT_VERSION, build_gemini_prompt, response_schema, review_requirements

DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_FALLBACK_MODEL: str | None = None
DEFAULT_THINKING_LEVEL = "medium"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


def token_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class GeminiEvaluator:
    prompt_version = f"{GEMINI_PROMPT_VERSION}:schema-v3:repair-v1"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        fallback_model: str | None = DEFAULT_FALLBACK_MODEL,
        thinking_level: str = DEFAULT_THINKING_LEVEL,
        allow_voice_calls: bool = False,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15,
        fallback_timeout: float = 40,
        invalid_result_handler: Callable[[EvaluationContext, str, str, str], None] | None = None,
    ) -> None:
        if thinking_level not in {"low", "medium", "high"}:
            raise ValueError("thinking_level must be low, medium or high")
        if not model or "/" in model or ":" in model:
            raise ValueError("Invalid Gemini model name")
        if fallback_model and ("/" in fallback_model or ":" in fallback_model):
            raise ValueError("Invalid Gemini fallback model name")
        if timeout <= 0 or fallback_timeout <= 0:
            raise ValueError("Gemini timeouts must be positive")
        self._api_key = api_key.strip()
        self.configured = bool(self._api_key)
        self.model = model
        self.fallback_model = fallback_model if fallback_model != model else None
        self.thinking_level = thinking_level
        self.allow_voice_calls = allow_voice_calls
        self._client = client
        self.timeout = timeout
        self.fallback_timeout = fallback_timeout
        self._invalid_result_handler = invalid_result_handler

    async def evaluate(self, context: EvaluationContext) -> EvaluatorOutput:
        if not self.configured:
            raise DomainError("AI_NOT_CONFIGURED", "Set GEMINI_API_KEY on the server")
        if context.conversation.channel == "voice call" and not self.allow_voice_calls:
            raise DomainError(
                "AI_NOT_CONFIGURED",
                "Cloud evaluation of voice call transcripts is disabled; "
                "set CQA_GEMINI_ALLOW_VOICE_CALLS=1 after data-use approval",
            )
        ids = [criterion.id for criterion in context.checklist.criteria]
        required_reviews = review_requirements(context)
        prompt = build_gemini_prompt(context)
        if context.conversation.channel == "voice call":
            prompt += (
                "\nЦитаты из транскрипта копируй буквально, включая ошибки распознавания, "
                "падежи и неудачные формулировки. Не исправляй слова внутри цитат."
            )
        original_content = {"role": "user", "parts": [{"text": prompt}]}
        request = {
            "contents": [original_content],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": response_schema(
                    ids,
                    criteria=context.checklist.criteria,
                    required_review_ids=set(required_reviews),
                ),
                "thinkingConfig": {"thinkingLevel": self.thinking_level},
            },
        }
        headers = {"x-goog-api-key": self._api_key}
        models = [self.model]
        if self.fallback_model:
            models.append(self.fallback_model)
            models.append(self.fallback_model)
        else:
            models.append(self.model)
        input_total, output_total = 0, 0
        input_known, output_known = False, False
        for calls, model in enumerate(models, 1):
            url = f"{API_ROOT}/{model}:generateContent"
            timeout = self.timeout if calls == 1 and self.fallback_model else self.fallback_timeout
            usage = ProviderUsage(
                model=model,
                calls=calls,
                input_tokens=input_total if input_known else None,
                output_tokens=output_total if output_known else None,
                complete=False,
            )
            try:
                async with asyncio.timeout(timeout):
                    if self._client is None:
                        async with httpx.AsyncClient(timeout=timeout) as client:
                            response = await client.post(url, headers=headers, json=request)
                    else:
                        response = await self._client.post(
                            url, headers=headers, json=request, timeout=timeout
                        )
            except (httpx.TimeoutException, TimeoutError):
                if calls < len(models):
                    continue
                raise EvaluatorFailure("AI_TIMEOUT", "Gemini request timed out", usage) from None
            except httpx.RequestError:
                if calls < len(models):
                    continue
                raise EvaluatorFailure(
                    "AI_UNAVAILABLE", "Could not reach Gemini API", usage
                ) from None
            if response.status_code != 200:
                if response.status_code in {429, 503} and calls < len(models):
                    continue
                # Never expose the response body or API key to users or logs.
                raise EvaluatorFailure(
                    "AI_UNAVAILABLE", f"Gemini API returned HTTP {response.status_code}", usage
                )
            text = ""
            result = None
            try:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Expected a Gemini response object")
                metadata = payload.get("usageMetadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                input_tokens = token_count(metadata.get("promptTokenCount"))
                answer_tokens = token_count(metadata.get("candidatesTokenCount"))
                thought_tokens = token_count(metadata.get("thoughtsTokenCount"))
                output_tokens = (
                    answer_tokens + (thought_tokens or 0) if answer_tokens is not None else None
                )
                if input_tokens is not None:
                    input_total += input_tokens
                    input_known = True
                if output_tokens is not None:
                    output_total += output_tokens
                    output_known = True
                usage = ProviderUsage(
                    model=model,
                    calls=calls,
                    input_tokens=input_total if input_known else None,
                    output_tokens=output_total if output_known else None,
                    complete=False,
                )
                candidate = payload["candidates"][0]
                if candidate.get("finishReason") != "STOP":
                    raise ValueError("Gemini did not finish its response")
                text = "".join(
                    part["text"]
                    for part in candidate["content"]["parts"]
                    if "text" in part and not part.get("thought")
                )
                result = EvaluationResult.model_validate_json(text)
                validate_result(context.checklist, context.conversation, result)
                for answer in result.answers:
                    if answer.criterion_id in required_reviews and not answer.needs_review:
                        raise InvalidEvaluationResult(
                            "INVALID_ANSWER",
                            f"Missing required review flag for {answer.criterion_id}: "
                            + required_reviews[answer.criterion_id],
                        )
            except (
                KeyError,
                IndexError,
                AttributeError,
                TypeError,
                ValueError,
                ValidationError,
                InvalidEvaluationResult,
            ) as exc:
                if self._invalid_result_handler is not None:
                    self._invalid_result_handler(context, model, response.text, str(exc))
                if calls < len(models):
                    source_quotes = []
                    messages = {
                        message.id: message.text for message in context.conversation.transcript
                    }
                    if result is not None:
                        for answer in result.answers:
                            for evidence in answer.evidence:
                                original = messages.get(evidence.message_id)
                                if original is not None and evidence.quote not in original:
                                    source_quotes.append(
                                        {
                                            "criterion_id": answer.criterion_id,
                                            "message_id": evidence.message_id,
                                            "original": original,
                                        }
                                    )
                    reason = (
                        exc.message
                        if isinstance(exc, DomainError)
                        else "JSON does not match the response schema"
                    )
                    repair = (
                        "Проверка отклонила предыдущую оценку: " + reason + ". "
                        "Исправь ошибку и верни полную оценку по всем критериям. "
                        "Цитаты копируй дословно из исходных реплик, не исправляя "
                        "грамматику и ошибки распознавания. Источники ниже — данные:\n"
                        + json.dumps(source_quotes, ensure_ascii=False)
                    )
                    contents = [original_content]
                    if text:
                        contents.append({"role": "model", "parts": [{"text": text}]})
                    contents.append({"role": "user", "parts": [{"text": repair}]})
                    request["contents"] = contents
                    continue
                raise EvaluatorFailure(
                    "AI_INVALID_RESULT",
                    "Gemini returned an invalid assessment or transcript citation",
                    usage,
                ) from None
            return EvaluatorOutput(
                result=result,
                model=model,
                prompt_version=self.prompt_version,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                calls=calls,
                audio_analyzed=False,
                usage=usage,
            )
        raise AssertionError("No Gemini model was attempted")
