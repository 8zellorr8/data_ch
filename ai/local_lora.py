"""Local LoRA evaluator for compact dialogue and legacy per-criterion adapters."""

import asyncio
import json
import threading
import time
from importlib import import_module
from pathlib import Path
from typing import Any

from backend.errors import DomainError, InvalidEvaluationResult
from backend.evaluator import EvaluationContext, EvaluatorOutput
from backend.models import EvaluationResult
from backend.scoring import validate_result

from .compact import compact_schema, expand_compact
from .local_policy import PROFILES, mark_input_review, messages_for, version_for
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
    local_only = True

    def __init__(
        self,
        adapter: Path,
        *,
        device: str = "auto",
        batch_size: int = 2,
        max_new_tokens: int = 2048,
        timeout: float = 150,
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
        self._enforcer_data: Any = None
        self._format = "criterion"
        self._prompt_profile = "legacy"
        self._force_json_order = False
        self._max_explanation_chars = 1000
        self._last_output_tokens: int | None = None
        self._prefill_chunk_size = 0
        self._prefill_threshold = 4096
        self.thinking_level = "disabled"
        self.prompt_version = f"{PROMPT_VERSION}:criterion"

    def _load(self) -> None:
        if self._model is not None:
            return
        manifest_path = self.adapter.parent / "training-manifest.json"
        if not manifest_path.is_file():
            raise DomainError("AI_NOT_CONFIGURED", "Local adapter training manifest is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        inference = manifest.get("inference", {})
        self._prompt_profile = inference.get("prompt_profile", "legacy")
        if self._prompt_profile not in PROFILES:
            raise DomainError("AI_NOT_CONFIGURED", "Unknown adapter prompt profile")
        self._force_json_order = bool(inference.get("force_json_field_order", False))
        self._max_explanation_chars = int(inference.get("max_explanation_chars", 1000))
        self._prefill_chunk_size = int(inference.get("prefill_chunk_size", 0))
        self._prefill_threshold = int(inference.get("prefill_threshold", 4096))
        if not 20 <= self._max_explanation_chars <= 1000:
            raise DomainError("AI_NOT_CONFIGURED", "Invalid adapter explanation limit")
        self._format = manifest.get("label_manifest", {}).get("example_format", "")
        if self._format not in {"criterion", "compact_dialogue"}:
            raise DomainError("AI_NOT_CONFIGURED", "Unsupported local adapter example format")
        try:
            import torch  # type: ignore[import-not-found]
            from peft import PeftConfig, PeftModel  # type: ignore[import-not-found]
            from transformers import (  # type: ignore[import-not-found]
                AutoModelForCausalLM,
                AutoTokenizer,
                BitsAndBytesConfig,
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
            from .torch_attention import enable_efficient_attention

            enable_efficient_attention()
            torch.cuda.set_per_process_memory_fraction(
                float(manifest.get("gpu_memory_fraction", 0.85))
            )
            torch.set_float32_matmul_precision("high")
        dtype = (
            torch.bfloat16
            if use_cuda and torch.cuda.is_bf16_supported()
            else torch.float16
            if use_cuda
            else torch.float32
        )
        config = PeftConfig.from_pretrained(self.adapter)
        load_options = {}
        if manifest.get("quantization") == "nf4":
            if not use_cuda:
                raise DomainError(
                    "AI_NOT_CONFIGURED", "This NF4 adapter requires a CUDA inference host"
                )
            load_options = {
                "quantization_config": BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_use_double_quant=True,
                    bnb_4bit_compute_dtype=dtype,
                ),
                "device_map": {"": 0},
            }
        base = AutoModelForCausalLM.from_pretrained(
            config.base_model_name_or_path,
            dtype=dtype,
            attn_implementation="sdpa",
            local_files_only=True,
            **load_options,
        )
        self._model = PeftModel.from_pretrained(base, self.adapter)
        if not load_options:
            self._model = self._model.to(self.device)
        self._model.eval()
        self._model.config.use_cache = True
        self._tokenizer = AutoTokenizer.from_pretrained(self.adapter, local_files_only=True)
        self._tokenizer.padding_side = "left"
        if self._tokenizer.pad_token is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token
        self._torch = torch
        self._max_length = int(inference.get("max_input_tokens", manifest["max_length"]))
        if self._format == "compact_dialogue":
            self.model = str(manifest.get("evaluator_model", "local-qwen3-1.7b-cqa-qlora"))
            self.prompt_version = version_for(self._prompt_profile)

    def warmup(self) -> None:
        with self._lock:
            self._load()

    def _generate_batch(
        self, batch: list[EvaluationContext], remaining: float
    ) -> tuple[list[str], int]:
        prompts = [
            self._tokenizer.apply_chat_template(
                messages_for(selection, self._prompt_profile)
                if self._format == "compact_dialogue"
                else [{"role": "user", "content": build_prompt(selection)}],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            for selection in batch
        ]
        encoded = self._tokenizer(prompts, padding=True, return_tensors="pt").to(self.device)
        if encoded["input_ids"].shape[-1] > self._max_length:
            raise DomainError("AI_INVALID_RESULT", "Chat exceeds the local adapter input limit")
        constraints = {}
        if self._format == "compact_dialogue":
            lmfe = import_module("lmformatenforcer")
            # LMFE 0.11.3 imports this typing alias from the pre-v5 location.
            legacy_tokenizers: Any = import_module("transformers.tokenization_utils")
            if not hasattr(legacy_tokenizers, "PreTrainedTokenizerBase"):
                legacy_tokenizers.PreTrainedTokenizerBase = import_module(
                    "transformers.tokenization_utils_base"
                ).PreTrainedTokenizerBase
            lmfe_transformers = import_module("lmformatenforcer.integrations.transformers")
            parser_config = import_module("lmformatenforcer.characterlevelparser")
            if self._enforcer_data is None:
                self._enforcer_data = lmfe_transformers.build_token_enforcer_tokenizer_data(
                    self._tokenizer
                )
            # A full multilingual tokenizer alphabet makes trie filtering expensive.
            # Keep all source characters, ASCII, RU/KZ Cyrillic and common punctuation.
            alphabet = (
                parser_config.CharacterLevelParserConfig().alphabet
                + prompts[0]
                + "".join(chr(code) for code in range(0x0400, 0x0500))
                + "—–…«»“”₸€№"
            )
            config = parser_config.CharacterLevelParserConfig(
                alphabet="".join(sorted(set(alphabet))),
                force_json_field_order=self._force_json_order,
            )
            schema: Any = compact_schema(batch[0])
            for properties in schema["properties"].values():
                properties["properties"]["explanation"]["maxLength"] = self._max_explanation_chars

            constraints["prefix_allowed_tokens_fn"] = (
                lmfe_transformers.build_transformers_prefix_allowed_tokens_fn(
                    self._enforcer_data,
                    lmfe.JsonSchemaParser(schema, config=config),
                )
            )
        with self._torch.inference_mode():
            if self.device == "cuda":
                self._torch.cuda.empty_cache()
            prefill = (
                {"prefill_chunk_size": self._prefill_chunk_size}
                if self._prefill_chunk_size
                and encoded["input_ids"].shape[-1] > self._prefill_threshold
                else {}
            )
            generated = self._model.generate(
                **encoded,
                max_new_tokens=self.max_new_tokens,
                max_time=remaining,
                do_sample=False,
                pad_token_id=self._tokenizer.pad_token_id,
                logits_to_keep=1,
                **prefill,
                **constraints,
            )
        texts: list[str] = self._tokenizer.batch_decode(
            generated[:, encoded["input_ids"].shape[-1] :], skip_special_tokens=True
        )
        self._last_output_tokens = int(
            (generated.shape[-1] - encoded["input_ids"].shape[-1]) * len(batch)
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
            if self._format == "compact_dialogue":
                texts, count = self._generate_batch([context], self.timeout)
                if time.perf_counter() - started >= self.timeout:
                    raise DomainError("AI_TIMEOUT", "Local QLoRA generation timed out")
                try:
                    result = expand_compact(context, json.loads(extract_json(texts[0])))
                    if self._prompt_profile == "guarded":
                        mark_input_review(context, result)
                except (ValueError, InvalidEvaluationResult):
                    raise LocalLoRAFailure(texts) from None
                return EvaluatorOutput(
                    result=result,
                    model=self.model,
                    prompt_version=self.prompt_version,
                    input_tokens=count,
                    output_tokens=self._last_output_tokens,
                    calls=1,
                    audio_analyzed=False,
                ), texts
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
