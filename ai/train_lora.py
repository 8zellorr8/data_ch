"""Optional local LoRA training for an open model after human labels exist.

This script is separate from the Gemini API evaluator. It does not upload data.
"""

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, default=Path("data/labels/sft/train.jsonl"))
    parser.add_argument("--validation", type=Path, default=Path("data/labels/sft/validation.jsonl"))
    parser.add_argument("--model", default="Qwen/Qwen3-0.6B")
    parser.add_argument("--output", type=Path, default=Path("data/lora/qwen3-cqa"))
    parser.add_argument("--max-length", type=int, default=4096)
    parser.add_argument("--epochs", type=float, default=3)
    parser.add_argument("--grad-accum", type=int, default=2)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    args = parser.parse_args()
    if not args.train.is_file() or not args.validation.is_file():
        parser.error("Build validated labels with 'python -m ai.labels build' first")
    if args.max_length < 512:
        parser.error("--max-length must be at least 512")
    if args.epochs <= 0 or args.grad_accum < 1:
        parser.error("--epochs and --grad-accum must be positive")

    try:
        import torch  # type: ignore[import-not-found]
        from datasets import load_dataset  # type: ignore[import-not-found]
        from peft import LoraConfig, TaskType, get_peft_model  # type: ignore[import-not-found]
        from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore[import-not-found]
        from trl import SFTConfig, SFTTrainer  # type: ignore[import-not-found]
    except ImportError as exc:
        parser.error(f"Install optional LoRA dependencies first: {exc.name}")
    use_cuda = args.device == "cuda" or (args.device == "auto" and torch.cuda.is_available())
    if use_cuda and not torch.cuda.is_available():
        parser.error("CUDA was requested but this PyTorch build cannot use it")
    device = "cuda" if use_cuda else "cpu"
    print(f"Training on {device}")

    data = load_dataset(
        "json",
        data_files={"train": str(args.train), "validation": str(args.validation)},
    )
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    for split in ("train", "validation"):
        for row in data[split]:
            tokens = tokenizer.apply_chat_template(
                row["prompt"] + row["completion"],
                tokenize=True,
                add_generation_prompt=False,
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

    model = AutoModelForCausalLM.from_pretrained(
        args.model, dtype=torch.float16 if use_cuda else torch.float32
    )
    model.config.use_cache = False
    model = get_peft_model(
        model,
        LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules=["q_proj", "v_proj"],
        ),
    )
    model.print_trainable_parameters()
    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=data["train"],
        eval_dataset=data["validation"],
        args=SFTConfig(
            output_dir=str(args.output),
            per_device_train_batch_size=1,
            per_device_eval_batch_size=1,
            gradient_accumulation_steps=args.grad_accum,
            num_train_epochs=args.epochs,
            learning_rate=1e-4,
            max_length=args.max_length,
            completion_only_loss=True,
            gradient_checkpointing=True,
            fp16=use_cuda,
            use_cpu=not use_cuda,
            eval_strategy="epoch",
            save_strategy="epoch",
            save_total_limit=2,
            logging_steps=10,
            optim="adamw_torch",
            report_to="none",
        ),
    )
    trainer.train()
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
