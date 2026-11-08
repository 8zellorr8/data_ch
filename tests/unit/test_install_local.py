"""Installing model artifacts verifies both weights before changing the base path."""

import hashlib
import json

import pytest

from ai.install_local import prepare


def test_offline_install_checks_both_artifacts(tmp_path):
    adapter = tmp_path / "model" / "adapter"
    base = tmp_path / "base"
    adapter.mkdir(parents=True)
    base.mkdir()
    (adapter / "adapter_model.safetensors").write_bytes(b"adapter")
    (base / "model.safetensors").write_bytes(b"base")
    config = adapter / "adapter_config.json"
    config.write_text('{"base_model_name_or_path":"unconfigured"}')
    manifest = {
        "adapter_sha256": hashlib.sha256(b"adapter").hexdigest(),
        "base_files_sha256": {"model.safetensors": hashlib.sha256(b"base").hexdigest()},
    }
    (adapter.parent / "training-manifest.json").write_text(json.dumps(manifest))
    (base / "model.safetensors").write_bytes(b"wrong")
    with pytest.raises(ValueError, match="Base model checksum"):
        prepare(adapter, base, download=False)
    assert json.loads(config.read_text())["base_model_name_or_path"] == "unconfigured"
    (base / "model.safetensors").write_bytes(b"base")
    prepare(adapter, base, download=False)
    assert json.loads(config.read_text())["base_model_name_or_path"] == str(base.resolve())
    (adapter / "adapter_model.safetensors").write_bytes(b"wrong")
    with pytest.raises(ValueError, match="Adapter checksum"):
        prepare(adapter, base, download=False)
