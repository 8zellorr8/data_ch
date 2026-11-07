"""Gemini adapter for the backend Evaluator contract."""

import asyncio
import math
import re
from time import time

import httpx
from pydantic import ValidationError

from backend.errors import DomainError, InvalidEvaluationResult
from backend.evaluator import (
    EvaluationContext,
    EvaluatorFailure,
    EvaluatorOutput,
    ProviderDeferred,
    RequestGate,
)
from backend.models import EvaluationResult
from backend.rate_limits import RequestLimiter
from backend.scoring import validate_result
from backend.work_models import ProviderUsage

from .prompt import PROMPT_VERSION, build_prompt, response_schema
from .rate_errors import retry_hint

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_MODEL = "gemini-3.6-flash"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


def token_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class GeminiEvaluator:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        fallback_model: str | None = DEFAULT_FALLBACK_MODEL,
        thinking_level: str = "low",
        allow_voice_calls: bool = False,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15,
        fallback_timeout: float = 40,
        request_gate: RequestGate | None = None,
    ) -> None:
        if thinking_level not in {"low", "medium", "high"}:
            raise ValueError("thinking_level must be low, medium or high")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", model):
            raise ValueError("Invalid Gemini model name")
        if fallback_model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", fallback_model):
            raise ValueError("Invalid Gemini fallback model name")
        if any(not math.isfinite(value) or value <= 0 for value in (timeout, fallback_timeout)):
            raise ValueError("Gemini timeouts must be finite and positive")
        self._api_key = api_key.strip()
        self.configured = bool(self._api_key)
        self.model = model
        self.fallback_model = fallback_model if fallback_model != model else None
        self.thinking_level = thinking_level
        self.allow_voice_calls = allow_voice_calls
        self._client = client
        self.timeout = timeout
        self.fallback_timeout = fallback_timeout
        self._request_gate = request_gate if request_gate is not None else RequestLimiter()

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
        request = {
            "contents": [{"role": "user", "parts": [{"text": build_prompt(context)}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": response_schema(ids),
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
            # ProviderUsage has one model/tariff. Combining tokens from different
            # models would price earlier attempts using the final model's rate.
            mixed_models = model != self.model
            url = f"{API_ROOT}/{model}:generateContent"
            timeout = self.timeout if calls == 1 and self.fallback_model else self.fallback_timeout
            usage = ProviderUsage(
                model=model,
                calls=calls,
                input_tokens=input_total if input_known and not mixed_models else None,
                output_tokens=output_total if output_known and not mixed_models else None,
                complete=False,
            )
            try:
                await self._request_gate.wait(model)
            except ProviderDeferred as exc:
                # A local cooldown has made no new network call. Earlier attempts
                # may be ambiguous, so do not enable automatic batch replay then.
                exc.provider_not_called = calls == 1
                exc.safe_to_retry = calls == 1
                exc.usage = usage.model_copy(update={"calls": calls - 1}) if calls > 1 else None
                raise
            try:
                async with asyncio.timeout(timeout):
                    if self._client is None:
                        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
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
            if response.status_code == 429:
                retry_at, exhausted = retry_hint(response, time())
                await self._request_gate.defer(model, retry_at, exhausted)
                # Never burn the remaining attempts immediately, or silently
                # switch model when a known quota refusal should pause the queue.
                raise ProviderDeferred(
                    retry_at,
                    quota_exhausted=exhausted,
                    usage=usage,
                    safe_to_retry=calls == 1,
                )
            if response.status_code != 200:
                if response.status_code == 503 and calls < len(models):
                    continue
                # Never expose the response body or API key to users or logs.
                raise EvaluatorFailure(
                    "AI_UNAVAILABLE", f"Gemini API returned HTTP {response.status_code}", usage
                )
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
                    input_tokens=input_total if input_known and not mixed_models else None,
                    output_tokens=output_total if output_known and not mixed_models else None,
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
            except (
                KeyError,
                IndexError,
                AttributeError,
                TypeError,
                ValueError,
                ValidationError,
                InvalidEvaluationResult,
            ):
                if calls < len(models):
                    continue
                raise EvaluatorFailure(
                    "AI_INVALID_RESULT",
                    "Gemini returned an invalid assessment or transcript citation",
                    usage,
                ) from None
            return EvaluatorOutput(
                result=result,
                model=model,
                prompt_version=PROMPT_VERSION,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                calls=calls,
                audio_analyzed=False,
                usage=usage,
            )
        raise AssertionError("No Gemini model was attempted")
