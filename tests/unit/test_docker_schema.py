"""Container schema checks allow the AI status extension, preserving all other contracts."""

from copy import deepcopy

import pytest

from ai.app import _configured_app
from backend.config import Settings
from backend.evaluator import UnconfiguredEvaluator
from backend.main import create_app
from scripts.docker_smoke import check_schema


@pytest.fixture
def schemas(tmp_path):
    settings = Settings(database_path=tmp_path / "schema.sqlite3", organizer_api_url="")
    baseline = create_app(settings).openapi()
    actual = _configured_app(create_app(settings), UnconfiguredEvaluator()).openapi()
    return actual, baseline


def test_container_schema_accepts_the_ai_status_extension(schemas):
    actual, baseline = schemas
    before = deepcopy(actual)
    check_schema(actual, baseline)
    assert actual == before


@pytest.mark.parametrize("change", ["extra_route", "changed_component", "missing_status"])
def test_container_schema_still_rejects_contract_drift(schemas, change):
    actual, baseline = schemas
    if change == "extra_route":
        actual["paths"]["/unexpected"] = {}
    elif change == "changed_component":
        actual["components"]["schemas"]["Health"]["description"] = "Changed contract"
    else:
        del actual["paths"]["/api/v1/ai/status"]
    with pytest.raises(AssertionError):
        check_schema(actual, baseline)
