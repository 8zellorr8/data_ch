"""Prepare a downloaded private adapter and its pinned public base, without paid APIs."""

import argparse
import json
from hashlib import file_digest
from pathlib import Path


def prepare(adapter: Path, base: Path, *, download: bool = True) -> None:
    manifest_path = adapter.parent / "training-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest["adapter_sha256"]
    with (adapter / "adapter_model.safetensors").open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("Adapter checksum does not match the training manifest")
    if download:
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

        snapshot_download(
            repo_id=manifest["base_repo"],
            revision=manifest["model_revision"],
            local_dir=base,
            token=False,
            allow_patterns=["*.json", "*.txt", "*.safetensors", "*.model", "LICENSE", "README.md"],
        )
    for name, checksum in manifest["base_files_sha256"].items():
        with (base / name).open("rb") as stream:
            if file_digest(stream, "sha256").hexdigest() != checksum:
                raise ValueError(f"Base model checksum mismatch: {name}")
    configuration_path = adapter / "adapter_config.json"
    configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
    configuration["base_model_name_or_path"] = str(base.resolve())
    configuration_path.write_text(json.dumps(configuration, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, default=Path("data/lora/qwen3-4b-cqa/adapter"))
    parser.add_argument("--base", type=Path, default=Path("data/models/qwen3-4b-instruct-2507-nf4"))
    parser.add_argument(
        "--existing-base", action="store_true", help="Verify existing files offline"
    )
    args = parser.parse_args()
    prepare(args.adapter, args.base, download=not args.existing_base)
    print("Local model prepared. No paid inference API was called.")


if __name__ == "__main__":
    main()
