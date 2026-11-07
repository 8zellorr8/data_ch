"""Optional local LoRA training for an open model after human labels exist.

This script is separate from the Gemini API evaluator. It does not upload data.
"""

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, default=Path("data/labels/sft/train.jsonl"))
    parser.add_argument("--validation", type=Path, default=Path("data/labels/sft/validation.jsonl"))
    parser.add_argument("--model", default="Qwen/Qwen3-0.6B")
    parser.add_argument("--output", type=Path, default=Path("data/lora/qwen3-cqa"))
    parser.add_argument("--max-length", type=int, default=4096)
    args = parser.parse_args()
    if not args.train.is_file() or not args.validation.is_file():
        parser.error("Build validated labels with 'python -m ai.labels build' first")
    if args.max_length < 512:
        parser.error("--max-length must be at least 512")

    try:
        import torch  # type: ignore[import-not-found]
        from datasets import load_dataset  # type: ignore[import-not-found]
        from peft import LoraConfig, TaskType, get_peft_model  # type: ignore[import-not-found]
        from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore[import-not-found]
        from trl import SFTConfig, SFTTrainer  # type: ignore[import-not-found]
    except ImportError as exc:
        parser.error(f"Install optional LoRA dependencies first: {exc.name}")
    if not torch.cuda.is_available():
        parser.error("A CUDA GPU is required by this training preset")

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
            if len(tokens) > args.max_length:
                parser.error(
                    f"A {split} example has {len(tokens)} tokens and would be truncated. "
                    "Increase --max-length or choose shorter chats."
                )

    model = AutoModelForCausalLM.from_pretrained(args.model, dtype=torch.float16)
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
            gradient_accumulation_steps=8,
            num_train_epochs=2,
            learning_rate=1e-4,
            max_length=args.max_length,
            completion_only_loss=True,
            gradient_checkpointing=True,
            fp16=True,
            eval_strategy="epoch",
            save_strategy="epoch",
            save_total_limit=2,
            logging_steps=10,
            report_to="none",
        ),
    )
    trainer.train()
    trainer.model.save_pretrained(args.output / "adapter")
    tokenizer.save_pretrained(args.output / "adapter")
    print(f"Saved LoRA adapter to {args.output / 'adapter'}")


if __name__ == "__main__":
    main()
