"""Exercise the local annotation pipeline using synthetic conversations only."""

import json
from pathlib import Path

import pytest

from ai.labels import build, prepare


@pytest.mark.parametrize("per_criterion", [False, True])
def test_prepared_labels_validate_and_split(
    tmp_path: Path, dataset: dict, answers: dict, per_criterion: bool
) -> None:
    records = dataset["records"]
    dataset["records"] = [{**records[0], "conversation_id": f"synthetic-{i}"} for i in range(5)]
    source = tmp_path / "dataset.json"
    source.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    drafts = tmp_path / "drafts"
    assert prepare(source, drafts, per_language=5, seed=42) == 5

    for path in drafts.glob("*.json"):
        draft = json.loads(path.read_text(encoding="utf-8"))
        draft["result"] = answers
        path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    output = tmp_path / "sft"
    with pytest.raises(ValueError, match="Incomplete or invalid annotation"):
        build(drafts, output)
    for path in drafts.glob("*.json"):
        draft = json.loads(path.read_text(encoding="utf-8"))
        draft["annotation_status"] = "reviewed"
        path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    assert build(drafts, output, per_criterion=per_criterion) == (
        (32, 8) if per_criterion else (4, 1)
    )
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["annotation_status_counts"] == {"reviewed": 5}
    assert len(manifest["validation_conversation_ids"]) == 1
    assert set(manifest["train_example_conversation_ids"]).isdisjoint(
        manifest["validation_example_conversation_ids"]
    )
    row = json.loads((output / "train.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["prompt"][0]["role"] == "user"
    assert "Здравствуйте, я Анна" in row["prompt"][0]["content"]
    assert json.loads(row["completion"][0]["content"])["answers"][0]["criterion_id"] == "contact"
    if per_criterion:
        rows = [
            json.loads(line)
            for line in (output / "validation.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 8
        assert all(len(json.loads(row["completion"][0]["content"])["answers"]) == 1 for row in rows)
        assert set(manifest["validation_criterion_ids"]) == {
            answer["criterion_id"] for answer in answers["answers"]
        }
