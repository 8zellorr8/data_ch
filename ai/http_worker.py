"""Remote local-GPU evaluator; this transport has no paid-provider fallback."""

from ipaddress import ip_address
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from backend.errors import DomainError, InvalidEvaluationResult
from backend.evaluator import EvaluationContext, EvaluatorOutput
from backend.scoring import validate_result

from .worker_protocol import WorkerOutput


class HTTPWorkerEvaluator:
    local_only = True

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 210,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url, self.api_key = base_url.rstrip("/"), api_key
        self.timeout, self.client = timeout, client
        self.configured = bool(base_url and api_key)
        self.model, self.prompt_version = "local-gpu-worker", "local-worker-v1"
        self.thinking_level = "disabled"
        if timeout <= 0:
            raise ValueError("Worker timeout must be positive")
        if base_url:
            parts = urlparse(base_url)
            try:
                loopback = (
                    parts.hostname == "localhost" or ip_address(parts.hostname or "").is_loopback
                )
            except ValueError:
                loopback = False
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
                or (parts.scheme != "https" and not loopback)
            ):
                raise ValueError("GPU worker URL must use HTTPS, or HTTP on loopback")

    async def _request(
        self, client: httpx.AsyncClient, context: EvaluationContext
    ) -> EvaluatorOutput:
        try:
            response = await client.post(
                self.base_url + "/v1/evaluate",
                headers={"Authorization": "Bearer " + self.api_key},
                json={
                    "conversation": context.conversation.model_dump(mode="json"),
                    "checklist": context.checklist.model_dump(mode="json"),
                },
                timeout=httpx.Timeout(self.timeout, connect=10, write=30, pool=10),
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise DomainError("AI_TIMEOUT", "GPU worker timed out") from None
        except httpx.HTTPError:
            raise DomainError("AI_UNAVAILABLE", "GPU worker could not be reached") from None
        if response.status_code in {401, 403}:
            raise DomainError("AI_NOT_CONFIGURED", "Check the GPU worker access key")
        if response.status_code == 429:
            raise DomainError("AI_BUSY", "GPU worker is processing another dialogue")
        if response.status_code == 504:
            raise DomainError("AI_TIMEOUT", "GPU worker generation timed out")
        if response.status_code == 502:
            try:
                code = response.json().get("detail", {}).get("code")
            except (ValueError, AttributeError):
                code = None
            if code in {
                "AI_INVALID_RESULT",
                "INVALID_RESULT",
                "INVALID_ANSWER",
                "INVALID_EVIDENCE",
            }:
                raise DomainError(
                    "AI_INVALID_RESULT", "GPU worker rejected an invalid model answer"
                )
        if response.status_code != 200:
            raise DomainError("AI_UNAVAILABLE", "GPU worker rejected the request")
        try:
            output = WorkerOutput.model_validate_json(response.content)
            validate_result(context.checklist, context.conversation, output.result)
        except (ValidationError, ValueError, InvalidEvaluationResult):
            raise DomainError(
                "AI_INVALID_RESULT", "GPU worker returned an invalid result"
            ) from None
        metadata = output.metadata
        self.model, self.prompt_version = metadata.model, metadata.prompt_version
        return EvaluatorOutput(
            result=output.result,
            model=metadata.model,
            prompt_version=metadata.prompt_version,
            input_tokens=metadata.input_tokens,
            output_tokens=metadata.output_tokens,
            calls=metadata.calls,
            audio_analyzed=metadata.audio_analyzed,
        )

    async def evaluate(self, context: EvaluationContext) -> EvaluatorOutput:
        if not self.configured:
            raise DomainError("AI_NOT_CONFIGURED", "Set the GPU worker URL and access key")
        if self.client is not None:
            return await self._request(self.client, context)
        async with httpx.AsyncClient() as client:
            return await self._request(client, context)
