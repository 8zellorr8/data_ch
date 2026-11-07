"""Budgeted evaluator for a third-party OpenAI-compatible Responses endpoint."""

import asyncio
import json
from collections.abc import Callable
from hashlib import sha256

import httpx

from backend.errors import DomainError, ErrorCode, InvalidEvaluationResult
from backend.evaluator import EvaluationContext, EvaluatorFailure, EvaluatorOutput
from backend.models import EvaluationResult
from backend.scoring import validate_result
from backend.work_models import ProviderUsage

from .budget import TokenBudget
from .gemini import token_count
from .prompt import GEMINI_PROMPT_VERSION, build_gemini_prompt, response_schema, review_requirements


class ResponsesEvaluator:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        budget: TokenBudget,
        model_aliases: tuple[str, ...] = (),
        thinking_level: str = "medium",
        response_format: str = "json_schema",
        allow_voice_calls: bool = False,
        max_output_tokens: int = 4096,
        max_input_bytes: int = 250_000,
        max_attempts: int = 2,
        timeout: float = 75,
        client: httpx.AsyncClient | None = None,
        invalid_result_handler: Callable[[EvaluationContext, str, str, str], None] | None = None,
    ) -> None:
        url = httpx.URL(base_url)
        if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("LLM base URL must be HTTPS without credentials, query or fragment")
        if not model or len(model) > 200 or any(c.isspace() for c in model):
            raise ValueError("Invalid LLM model")
        if thinking_level not in {"low", "medium", "high"}:
            raise ValueError("Invalid LLM thinking level")
        if response_format not in {"json_schema", "json_object", "text"}:
            raise ValueError("Invalid LLM response format")
        if not 1 <= max_attempts <= 2 or not 128 <= max_output_tokens <= 8192:
            raise ValueError("Invalid LLM attempt or output limit")
        if timeout <= 0 or max_input_bytes <= 0:
            raise ValueError("Invalid LLM deadline or input limit")
        self._api_key = api_key.strip()
        self.configured = bool(self._api_key)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.accepted_models = {model, *model_aliases}
        self.thinking_level = thinking_level
        self.response_format = response_format
        self.budget = budget
        self.allow_voice_calls = allow_voice_calls
        self.max_output_tokens = max_output_tokens
        self.max_input_bytes = max_input_bytes
        self.max_attempts = max_attempts
        self.timeout = timeout
        self._client = client
        self._invalid_result_handler = invalid_result_handler
        endpoint_id = sha256(self.base_url.encode()).hexdigest()[:12]
        self.prompt_version = f"cqa-responses-v2:{GEMINI_PROMPT_VERSION}:{response_format}:{thinking_level}:{endpoint_id}"

    async def evaluate(self, context: EvaluationContext) -> EvaluatorOutput:
        if not self.configured:
            raise DomainError("AI_NOT_CONFIGURED", "Set CQA_LLM_API_KEY on the server")
        if context.conversation.channel == "voice call" and not self.allow_voice_calls:
            raise DomainError(
                "AI_NOT_CONFIGURED", "Cloud evaluation of voice transcripts is disabled"
            )
        required = review_requirements(context)
        schema = response_schema(
            [c.id for c in context.checklist.criteria],
            criteria=context.checklist.criteria,
            required_review_ids=set(required),
        )
        prompt = build_gemini_prompt(context)
        # Compatible gateways may accept text.format without forwarding the schema.
        prompt += (
            "\n\nВерни только JSON-объект с верхним ключом answers, без Markdown и блоков кода. "
            "Каждый элемент answers обязан содержать criterion_id, value, explanation, "
            "evidence_kind, evidence (массив объектов message_id/quote), na_reason, "
            "needs_review, limitation. Для N/A тоже нужна контекстная цитата. "
            "Обязательная схема формы ответа (шкалы и условия N/A бери из чек-листа):\n"
            + json.dumps(
                response_schema([c.id for c in context.checklist.criteria]),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        if context.conversation.channel == "voice call":
            prompt += "\nЦитаты из транскрипта копируй буквально, включая ошибки ASR и падежи."
        original = {"role": "user", "content": prompt}
        request: dict[str, object] = {
            "model": self.model,
            "input": [original],
            "max_output_tokens": self.max_output_tokens,
            "reasoning": {"effort": self.thinking_level},
            "store": False,
        }
        if self.response_format == "json_schema":
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "cqa_assessment",
                    "strict": True,
                    "schema": schema,
                }
            }
        else:
            original["content"] += "\nОбязательная JSON-схема ответа:\n" + json.dumps(
                schema, ensure_ascii=False
            )
            if self.response_format == "json_object":
                request["text"] = {"format": {"type": "json_object"}}
        input_total, output_total = 0, 0
        input_known, output_known = False, False
        for calls in range(1, self.max_attempts + 1):
            body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(body) > self.max_input_bytes:
                raise DomainError(
                    "AI_INVALID_RESULT", "LLM input exceeds the configured byte limit"
                )
            # Text-only requests: UTF-8 bytes plus framing is a conservative input-token bound.
            # The gateway reports reasoning tokens beyond the visible-output cap.
            held = len(body) + 1024 + max(self.max_output_tokens, 65_536)
            reservation = await asyncio.to_thread(self.budget.reserve, self.model, held)
            settled = False
            usage = ProviderUsage(
                model=self.model,
                calls=calls,
                input_tokens=input_total if input_known else None,
                output_tokens=output_total if output_known else None,
                complete=False,
            )
            try:
                try:
                    async with asyncio.timeout(self.timeout):
                        headers = {
                            "Authorization": "Bearer " + self._api_key,
                            "Content-Type": "application/json",
                        }
                        if self._client is None:
                            async with httpx.AsyncClient(
                                timeout=self.timeout, follow_redirects=False
                            ) as client:
                                response = await client.post(
                                    self.base_url + "/responses", headers=headers, content=body
                                )
                        else:
                            response = await self._client.post(
                                self.base_url + "/responses",
                                headers=headers,
                                content=body,
                                timeout=self.timeout,
                                follow_redirects=False,
                            )
                except (httpx.TimeoutException, TimeoutError, httpx.RequestError) as exc:
                    if calls < self.max_attempts:
                        await asyncio.sleep(2)
                        continue
                    code: ErrorCode = (
                        "AI_TIMEOUT"
                        if isinstance(exc, (httpx.TimeoutException, TimeoutError))
                        else "AI_UNAVAILABLE"
                    )
                    raise EvaluatorFailure(code, "LLM provider connection failed", usage) from None
                if response.status_code != 200:
                    if response.status_code in {502, 503, 504} and calls < self.max_attempts:
                        await asyncio.sleep(2)
                        continue
                    raise EvaluatorFailure(
                        "AI_UNAVAILABLE",
                        f"LLM provider returned HTTP {response.status_code}",
                        usage,
                    )
                try:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("Expected a Responses object")
                    metadata = payload.get("usage") or {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                    inputs = token_count(metadata.get("input_tokens"))
                    outputs = token_count(metadata.get("output_tokens"))
                    total = token_count(metadata.get("total_tokens"))
                    await asyncio.to_thread(
                        self.budget.settle,
                        reservation,
                        input_tokens=inputs,
                        output_tokens=outputs,
                        total_tokens=total,
                    )
                    settled = True
                    if inputs is not None:
                        input_total += inputs
                        input_known = True
                    if outputs is not None:
                        output_total += outputs
                        output_known = True
                    usage = ProviderUsage(
                        model=self.model,
                        calls=calls,
                        input_tokens=input_total if input_known else None,
                        output_tokens=output_total if output_known else None,
                        complete=False,
                    )
                except (ValueError, TypeError):
                    raise EvaluatorFailure(
                        "AI_INVALID_RESULT",
                        "LLM provider returned an invalid response envelope",
                        usage,
                    ) from None
                if payload.get("error"):
                    if self._invalid_result_handler is not None:
                        self._invalid_result_handler(
                            context,
                            self.model,
                            response.text.replace(self._api_key, "[REDACTED]"),
                            "Provider returned an API error",
                        )
                    raise EvaluatorFailure(
                        "AI_UNAVAILABLE", "LLM provider returned an API error", usage
                    )
                actual_model = payload.get("model")
                if not isinstance(actual_model, str) or actual_model not in self.accepted_models:
                    raise EvaluatorFailure(
                        "AI_INVALID_RESULT", "LLM provider returned an unexpected model", usage
                    )
                usage = usage.model_copy(update={"model": actual_model})
                text = ""
                result = None
                try:
                    if payload.get("status") != "completed":
                        raise ValueError("Incomplete response")
                    text = "".join(
                        part["text"]
                        for item in payload["output"]
                        if item.get("type") == "message" and item.get("role") == "assistant"
                        for part in item["content"]
                        if part.get("type") == "output_text"
                    )
                    normalized = text.strip()
                    if normalized.startswith(("```json\n", "```\n")) and normalized.endswith("```"):
                        normalized = normalized.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                    result = EvaluationResult.model_validate_json(normalized)
                    validate_result(context.checklist, context.conversation, result)
                    for answer in result.answers:
                        if answer.criterion_id in required and not answer.needs_review:
                            raise InvalidEvaluationResult(
                                "INVALID_ANSWER",
                                "Missing required review flag for " + answer.criterion_id,
                            )
                except (
                    KeyError,
                    TypeError,
                    AttributeError,
                    ValueError,
                    InvalidEvaluationResult,
                ) as exc:
                    if self._invalid_result_handler is not None:
                        self._invalid_result_handler(
                            context,
                            self.model,
                            response.text.replace(self._api_key, "[REDACTED]"),
                            str(exc),
                        )
                    if calls < self.max_attempts:
                        source = []
                        messages = {m.id: m.text for m in context.conversation.transcript}
                        if result is not None:
                            source = [
                                {"message_id": e.message_id, "original": messages[e.message_id]}
                                for a in result.answers
                                for e in a.evidence
                                if e.message_id in messages
                                and e.quote not in messages[e.message_id]
                            ]
                        reason = (
                            exc.message
                            if isinstance(exc, DomainError)
                            else "JSON does not match the response schema"
                        )
                        feedback = (
                            "Исправь отклонённый ответ: "
                            + reason
                            + ". Верни все критерии. Копируй цитаты буквально. Исходные реплики:\n"
                            + json.dumps(source, ensure_ascii=False)
                        )
                        request["input"] = [
                            original,
                            {"role": "assistant", "content": text},
                            {"role": "user", "content": feedback},
                        ]
                        continue
                    raise EvaluatorFailure(
                        "AI_INVALID_RESULT",
                        "LLM assessment or transcript citation failed validation",
                        usage,
                    ) from None
                return EvaluatorOutput(
                    result=result,
                    model=actual_model,
                    prompt_version=self.prompt_version,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    calls=calls,
                    audio_analyzed=False,
                    usage=usage,
                )
            finally:
                if not settled:
                    await asyncio.to_thread(
                        self.budget.settle, reservation, input_tokens=None, output_tokens=None
                    )
        raise AssertionError("No LLM attempt was made")
