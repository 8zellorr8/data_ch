"""Optional local LoRA training for an open model from validated annotation files.

This script is separate from the Gemini API evaluator. It does not upload data.
"""

import argparse
import json
import math
from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, default=Path("data/labels/sft/train.jsonl"))
    parser.add_argument("--validation", type=Path, default=Path("data/labels/sft/validation.jsonl"))
    parser.add_argument("--model", default="Qwen/Qwen3-0.6B")
    parser.add_argument("--output", type=Path, default=Path("data/lora/qwen3-cqa"))
    parser.add_argument("--max-length", type=int, default=4096)
    parser.add_argument("--epochs", type=float, default=3)
    parser.add_argument("--grad-accum", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=-1)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--gpu-memory-fraction", type=float, default=0.85)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--load-in-4bit", action="store_true", help="QLoRA NF4 on CUDA")
    parser.add_argument("--target-modules", choices=("q-v", "all-linear"), default="q-v")
    parser.add_argument(
        "--optimizer", choices=("adamw_torch", "adamw_bnb_8bit"), default="adamw_torch"
    )
    parser.add_argument("--warmup-steps", type=int, default=0)
    parser.add_argument("--scheduler", choices=("linear", "cosine"), default="linear")
    args = parser.parse_args()
    if not args.train.is_file() or not args.validation.is_file():
        parser.error("Build validated labels with 'python -m ai.labels build' first")
    if args.max_length < 512:
        parser.error("--max-length must be at least 512")
    if args.epochs <= 0 or args.grad_accum < 1 or args.cpu_threads < 1:
        parser.error("--epochs, --grad-accum and --cpu-threads must be positive")
    if not 0 < args.gpu_memory_fraction <= 1 or args.max_steps == 0 or args.max_steps < -1:
        parser.error("Invalid memory fraction or maximum step count")
    if args.warmup_steps < 0:
        parser.error("--warmup-steps cannot be negative")

    try:
        import torch  # type: ignore[import-not-found]
        from datasets import Dataset, DatasetDict  # type: ignore[import-not-found]
        from peft import LoraConfig, TaskType, get_peft_model, prepare_model_for_kbit_training  # type: ignore[import-not-found]
        from transformers import (  # type: ignore[import-not-found]
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            TrainerCallback,
            set_seed,
        )
        from trl import SFTConfig, SFTTrainer  # type: ignore[import-not-found]
    except ImportError as exc:
        parser.error(f"Install optional LoRA dependencies first: {exc.name}")
    use_cuda = args.device == "cuda" or (args.device == "auto" and torch.cuda.is_available())
    if use_cuda and not torch.cuda.is_available():
        parser.error("CUDA was requested but this PyTorch build cannot use it")
    device = "cuda" if use_cuda else "cpu"
    torch.set_num_threads(args.cpu_threads)
    if use_cuda:
        from .torch_attention import enable_efficient_attention

        enable_efficient_attention()
        torch.cuda.set_per_process_memory_fraction(args.gpu_memory_fraction)
        torch.set_float32_matmul_precision("high")
    set_seed(42)
    use_bf16 = use_cuda and torch.cuda.is_bf16_supported()
    dtype = torch.bfloat16 if use_bf16 else torch.float16 if use_cuda else torch.float32
    print(f"Training on {device}, dtype={dtype}", flush=True)

    # These small local files fit in RAM; the JSON builder's lock path can exceed
    # Windows MAX_PATH inside a worktree. Do not upload or silently truncate them.
    data = DatasetDict(
        {
            split: Dataset.from_list(
                [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            )
            for split, path in (("train", args.train), ("validation", args.validation))
        }
    )
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    labels_manifest = args.train.parent / "manifest.json"
    label_info = (
        json.loads(labels_manifest.read_text(encoding="utf-8")) if labels_manifest.is_file() else {}
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    for split in ("train", "validation"):
        for row in data[split]:
            tokens = tokenizer.apply_chat_template(
                row["prompt"] + row["completion"],
                tokenize=True,
                add_generation_prompt=False,
                enable_thinking=False,
            )
            try:
                token_ids = tokens["input_ids"]
            except (TypeError, KeyError):
                token_ids = tokens
            if len(token_ids) > args.max_length:
                parser.error(
                    f"A {split} example has {len(token_ids)} tokens and would be truncated. "
                    "Increase --max-length or choose shorter chats."
                )

    model_options = {}
    if args.load_in_4bit:
        if not use_cuda:
            parser.error("NF4 training requires CUDA")
        model_options = {
            "quantization_config": BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=dtype,
            ),
            "device_map": {"": 0},
        }
    model = AutoModelForCausalLM.from_pretrained(
        args.model, dtype=dtype, attn_implementation="sdpa", **model_options
    )
    if args.load_in_4bit:
        model = prepare_model_for_kbit_training(
            model, gradient_checkpointing_kwargs={"use_reentrant": False}
        )
        # Keep frozen embeddings/norm weights in the requested compute dtype;
        # prepare_model_for_kbit_training otherwise promotes them to float32.
        for parameter in model.parameters():
            if not parameter.requires_grad and parameter.dtype == torch.float32:
                parameter.data = parameter.data.to(dtype)
        chunked_loss: Any = import_module("trl.kernels.chunked_logprob")
        loss_utils: Any = import_module("trl.trainer.utils")
        chunked_loss.TOKEN_CHUNK_SIZE = 256
        loss_utils._CHUNKED_LOGPROB_CHUNK_SIZE = 8192
    model.config.use_cache = False
    model = get_peft_model(
        model,
        LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules="all-linear"
            if args.target_modules == "all-linear"
            else ["q_proj", "v_proj"],
        ),
    )
    model.print_trainable_parameters()
    if label_info.get("example_format") == "compact_dialogue":
        model.save_pretrained(args.output / "baseline-adapter")
        tokenizer.save_pretrained(args.output / "baseline-adapter")

    class FiniteMetrics(TrainerCallback):  # type: ignore[misc]
        def on_log(
            self, arguments: Any, state: Any, control: Any, logs: Any = None, **kwargs: Any
        ) -> None:
            for name in ("loss", "grad_norm", "eval_loss"):
                if name in (logs or {}) and not math.isfinite(float(logs[name])):
                    raise RuntimeError(f"Non-finite {name}; local training stopped")

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=data["train"],
        eval_dataset=data["validation"],
        callbacks=[FiniteMetrics()],
        args=SFTConfig(
            output_dir=str(args.output),
            per_device_train_batch_size=1,
            per_device_eval_batch_size=1,
            gradient_accumulation_steps=args.grad_accum,
            num_train_epochs=args.epochs,
            max_steps=args.max_steps,
            learning_rate=1e-4,
            warmup_steps=args.warmup_steps,
            lr_scheduler_type=args.scheduler,
            max_length=args.max_length,
            completion_only_loss=True,
            gradient_checkpointing=True,
            gradient_checkpointing_kwargs={"use_reentrant": False},
            fp16=use_cuda and not use_bf16,
            bf16=use_bf16,
            tf32=use_cuda,
            use_cpu=not use_cuda,
            eval_strategy="epoch",
            save_strategy="epoch",
            save_total_limit=2,
            logging_steps=1,
            logging_first_step=True,
            prediction_loss_only=True,
            dataloader_pin_memory=False,
            optim=args.optimizer,
            report_to="none",
            load_best_model_at_end=True,
            metric_for_best_model="eval_loss",
        ),
    )
    if args.load_in_4bit:
        for parameter in trainer.model.parameters():
            if not parameter.requires_grad and parameter.dtype == torch.float32:
                parameter.data = parameter.data.to(dtype)
    baseline = trainer.evaluate(metric_key_prefix="baseline")
    training = trainer.train()
    final = trainer.evaluate(metric_key_prefix="final")
    trainer.save_state()
    trainer.model.save_pretrained(args.output / "adapter")
    tokenizer.save_pretrained(args.output / "adapter")
    manifest = {
        "model": args.model,
        "created_at": datetime.now(UTC).isoformat(),
        "train_examples": len(data["train"]),
        "validation_examples": len(data["validation"]),
        "train_sha256": sha256(args.train.read_bytes()).hexdigest(),
        "validation_sha256": sha256(args.validation.read_bytes()).hexdigest(),
        "max_length": args.max_length,
        "epochs": args.epochs,
        "gradient_accumulation_steps": args.grad_accum,
        "device": device,
        "gpu": torch.cuda.get_device_name(0) if use_cuda else None,
        "gpu_memory_fraction": args.gpu_memory_fraction if use_cuda else None,
        "dtype": str(dtype),
        "max_steps": args.max_steps,
        "quantization": "nf4" if args.load_in_4bit else "none",
        "loss_token_tile": 256 if args.load_in_4bit else None,
        "loss_vocab_tile": 8192 if args.load_in_4bit else None,
        "peak_gpu_bytes": torch.cuda.max_memory_allocated() if use_cuda else None,
        "best_checkpoint": trainer.state.best_model_checkpoint,
        "best_validation_loss": trainer.state.best_metric,
        "optimizer_steps": trainer.state.global_step,
        "optimizer": args.optimizer,
        "warmup_steps": args.warmup_steps,
        "scheduler": args.scheduler,
        "lora": {"r": 8, "alpha": 16, "target_modules": args.target_modules},
        "metrics": {"baseline": baseline, "train": training.metrics, "final": final},
    }
    labels_manifest = args.train.parent / "manifest.json"
    if labels_manifest.is_file():
        manifest["label_manifest"] = json.loads(labels_manifest.read_text(encoding="utf-8"))
    (args.output / "training-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Saved LoRA adapter to {args.output / 'adapter'}")


if __name__ == "__main__":
    main()
