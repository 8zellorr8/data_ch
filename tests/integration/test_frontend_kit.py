import json

from scripts.frontend_kit import ROOT, collection, export_kit, read_scenario


def test_frontend_handoff_runs_against_real_backend(client, tmp_path):
    assert export_kit(client, tmp_path) == 16
    bundle = json.loads((tmp_path / "responses.json").read_text(encoding="utf-8"))
    records = bundle["responses"]
    assert records["01-health"]["body"]["ai_configured"] is False
    assert records["08-manual-evaluation"]["body"]["origin"] == "manual"
    assert records["12-resolve-appeal"]["body"]["original_score"]["total"] == 75
    exported = records["14-export"]["body"]
    assert len(exported["evaluations"]) == 1
    assert exported["evaluations"][0]["reviews"][0]["previous_value"] == 5
    assert json.loads((tmp_path / "openapi.json").read_text()) == client.app.openapi()


def test_committed_postman_collection_matches_executable_scenario():
    path = ROOT / "examples" / "frontend" / "cqa.postman_collection.json"
    assert json.loads(path.read_text(encoding="utf-8")) == collection(read_scenario())
