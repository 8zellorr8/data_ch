"""Exercise the local annotation pipeline using synthetic conversations only."""

import json
from pathlib import Path

from ai.labels import build, prepare


def test_prepared_labels_validate_and_split(tmp_path: Path, dataset: dict, answers: dict) -> None:
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
    assert build(drafts, output) == (4, 1)
    row = json.loads((output / "train.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["prompt"][0]["role"] == "user"
    assert "Здравствуйте, я Анна" in row["prompt"][0]["content"]
    assert json.loads(row["completion"][0]["content"])["answers"][0]["criterion_id"] == "contact"
