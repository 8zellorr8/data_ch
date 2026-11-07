"""Experimental local LoRA evaluator using one criterion per generation."""

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

from backend.errors import DomainError, InvalidEvaluationResult
from backend.evaluator import EvaluationContext, EvaluatorOutput
from backend.models import EvaluationResult
from backend.scoring import validate_result

from .prompt import PROMPT_VERSION, build_prompt


def extract_json(text: str) -> str:
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("No JSON object in generation")
    return text[start : end + 1]


class LocalLoRAFailure(DomainError):
    def __init__(self, generations: list[str]) -> None:
        super().__init__("AI_INVALID_RESULT", "Local LoRA returned an invalid answer or citation")
        self.generations = generations


class LocalLoRAEvaluator:
    def __init__(
        self,
        adapter: Path,
        *,
        device: str = "auto",
        batch_size: int = 2,
        max_new_tokens: int = 512,
        timeout: float = 120,
    ) -> None:
        if (
            device not in {"auto", "cuda", "cpu"}
            or not 1 <= batch_size <= 4
            or max_new_tokens < 1
            or timeout <= 0
        ):
            raise ValueError("Invalid local device or batch size")
        self.adapter = adapter
        self.configured = (adapter / "adapter_config.json").is_file()
        self.device = device
        self.batch_size = batch_size
        self.max_new_tokens = max_new_tokens
        self.timeout = timeout
        self.model = "local-qwen3-cqa-lora"
        self._lock = threading.Lock()
        self._torch: Any = None
        self._model: Any = None
        self._tokenizer: Any = None
        self._max_length = 2560

    def _load(self) -> None:
        if self._model is not None:
            return
        manifest_path = self.adapter.parent / "training-manifest.json"
        if not manifest_path.is_file():
            raise DomainError("AI_NOT_CONFIGURED", "Local adapter training manifest is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("label_manifest", {}).get("example_format") != "criterion":
            raise DomainError(
                "AI_NOT_CONFIGURED", "This evaluator requires a per-criterion adapter"
            )
        try:
            import torch  # type: ignore[import-not-found]
            from peft import PeftConfig, PeftModel  # type: ignore[import-not-found]
            from transformers import (  # type: ignore[import-not-found]
                AutoModelForCausalLM,
                AutoTokenizer,
            )
        except ImportError:
            raise DomainError("AI_NOT_CONFIGURED", "Install the local LoRA dependencies") from None
        use_cuda = self.device == "cuda" or (self.device == "auto" and torch.cuda.is_available())
        if use_cuda and not torch.cuda.is_available():
            raise DomainError(
                "AI_NOT_CONFIGURED", "CUDA is unavailable in this PyTorch environment"
            )
        self.device = "cuda" if use_cuda else "cpu"
        torch.set_num_threads(4)
        if use_cuda:
            torch.cuda.set_per_process_memory_fraction(0.85)
            torch.set_float32_matmul_precision("high")
        dtype = (
            torch.bfloat16
            if use_cuda and torch.cuda.is_bf16_supported()
            else torch.float16
            if use_cuda
            else torch.float32
        )
        config = PeftConfig.from_pretrained(self.adapter)
        base = AutoModelForCausalLM.from_pretrained(
            config.base_model_name_or_path,
            dtype=dtype,
            attn_implementation="sdpa",
            local_files_only=True,
        )
        self._model = PeftModel.from_pretrained(base, self.adapter).to(self.device).eval()
        self._model.config.use_cache = True
        self._tokenizer = AutoTokenizer.from_pretrained(self.adapter, local_files_only=True)
        self._tokenizer.padding_side = "left"
        if self._tokenizer.pad_token is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token
        self._torch = torch
        self._max_length = int(manifest["max_length"])

    def _generate_batch(
        self, batch: list[EvaluationContext], remaining: float
    ) -> tuple[list[str], int]:
        prompts = [
            self._tokenizer.apply_chat_template(
                [{"role": "user", "content": build_prompt(selection)}],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            for selection in batch
        ]
        encoded = self._tokenizer(prompts, padding=True, return_tensors="pt").to(self.device)
        if encoded["input_ids"].shape[-1] > self._max_length:
            raise DomainError("AI_INVALID_RESULT", "Chat exceeds the local adapter input limit")
        with self._torch.inference_mode():
            generated = self._model.generate(
                **encoded,
                max_new_tokens=self.max_new_tokens,
                max_time=remaining,
                do_sample=False,
                pad_token_id=self._tokenizer.pad_token_id,
            )
        texts: list[str] = self._tokenizer.batch_decode(
            generated[:, encoded["input_ids"].shape[-1] :], skip_special_tokens=True
        )
        return texts, int(encoded["attention_mask"].sum().item())

    def evaluate_with_generations(
        self, context: EvaluationContext
    ) -> tuple[EvaluatorOutput, list[str]]:
        if not self.configured:
            raise DomainError(
                "AI_NOT_CONFIGURED", "Set CQA_LORA_ADAPTER to a trained local adapter"
            )
        with self._lock:
            self._load()
            started = time.perf_counter()
            selections = [
                EvaluationContext(
                    context.conversation,
                    context.checklist.model_copy(update={"criteria": [criterion]}),
                )
                for criterion in context.checklist.criteria
            ]
            answers = []
            generations = []
            input_tokens = 0
            calls = 0
            for offset in range(0, len(selections), self.batch_size):
                batch = selections[offset : offset + self.batch_size]
                remaining = self.timeout - (time.perf_counter() - started)
                if remaining <= 0:
                    raise DomainError("AI_TIMEOUT", "Local LoRA generation timed out")
                texts, count = self._generate_batch(batch, remaining)
                if time.perf_counter() - started >= self.timeout:
                    raise DomainError("AI_TIMEOUT", "Local LoRA generation timed out")
                input_tokens += count
                calls += 1
                generations.extend(texts)
                for selection, text in zip(batch, texts, strict=True):
                    try:
                        result = EvaluationResult.model_validate_json(extract_json(text))
                        validate_result(selection.checklist, context.conversation, result)
                    except (ValueError, InvalidEvaluationResult):
                        raise LocalLoRAFailure(generations) from None
                    answers.extend(result.answers)
            result = EvaluationResult(answers=answers)
            validate_result(context.checklist, context.conversation, result)
            return EvaluatorOutput(
                result=result,
                model=self.model,
                prompt_version=f"{PROMPT_VERSION}:criterion",
                input_tokens=input_tokens,
                calls=calls,
                audio_analyzed=False,
            ), generations

    async def evaluate(self, context: EvaluationContext) -> EvaluatorOutput:
        output, _ = await asyncio.to_thread(self.evaluate_with_generations, context)
        return output
