"""Run the frontend handoff scenario against a temporary backend or a running API."""

import argparse
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples" / "frontend" / "scenario.json"
VARIABLE = re.compile(r"\{\{([a-z_]+)\}\}")


class DemoFailure(Exception):
    """The integration scenario did not produce the documented result."""


def read_scenario() -> dict[str, Any]:
    scenario: dict[str, Any] = json.loads(SCENARIO.read_text(encoding="utf-8"))
    for step in scenario["steps"]:
        if "body_file" in step:
            step["body"] = json.loads(
                (SCENARIO.parent / step.pop("body_file")).read_text(encoding="utf-8")
            )
    return scenario


def resolve(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = VARIABLE.fullmatch(value)
        if match:
            # Revision numbers stay numbers when inserted into a JSON request.
            return variables[match[1]]
        return VARIABLE.sub(lambda match: str(variables[match[1]]), value)
    if isinstance(value, dict):
        return {key: resolve(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, variables) for item in value]
    return value


def at_path(body: Any, path: list[str | int]) -> Any:
    for key in path:
        body = body[key]
    return body


def run_scenario(client: httpx.Client, scenario: dict[str, Any]) -> dict[str, Any]:
    variables: dict[str, Any] = {}
    responses: dict[str, Any] = {}
    for step in scenario["steps"]:
        body = resolve(step["body"], variables) if "body" in step else None
        response = client.request(step["method"], resolve(step["path"], variables), json=body)
        if response.status_code != step["status"]:
            raise DemoFailure(
                f"{step['name']}: expected HTTP {step['status']}, got {response.status_code}"
            )
        payload = response.json()
        for check in step.get("checks", []):
            if at_path(payload, check["path"]) != check["equals"]:
                raise DemoFailure(f"{step['name']}: unexpected response field {check['path']}")
        for key, path in step.get("capture", {}).items():
            variables[key] = at_path(payload, path)
        responses[step["name"]] = {"status": response.status_code, "body": payload}
    return {"variables": variables, "responses": responses}


def collection(scenario: dict[str, Any]) -> dict[str, Any]:
    items = []
    for step in scenario["steps"]:
        request: dict[str, Any] = {
            "method": step["method"],
            "url": "{{base_url}}" + step["path"],
            "header": [],
        }
        if "body" in step:
            raw = json.dumps(step["body"], ensure_ascii=False, indent=2)
            # JSON numeric placeholders must not be quoted in the Postman body.
            for name in scenario["numeric_variables"]:
                raw = raw.replace('"{{' + name + '}}"', "{{" + name + "}}")
            request["header"] = [{"key": "Content-Type", "value": "application/json"}]
            request["body"] = {
                "mode": "raw",
                "raw": raw,
                "options": {"raw": {"language": "json"}},
            }
        script = [
            f'pm.test("HTTP {step["status"]}", () => '
            f"pm.response.to.have.status({step['status']}));",
            f"if (pm.response.code === {step['status']}) {{",
            "  const body = pm.response.json();",
        ]
        for check in step.get("checks", []):
            accessor = "body" + "".join(f"[{json.dumps(key)}]" for key in check["path"])
            expected = json.dumps(check["equals"], ensure_ascii=False)
            label = json.dumps("Response " + str(check["path"]))
            script.append(
                f"  pm.test({label}, () => pm.expect({accessor}).to.deep.equal({expected}));"
            )
        for key, path in step.get("capture", {}).items():
            accessor = "body" + "".join(f"[{json.dumps(part)}]" for part in path)
            script.append(f"  pm.collectionVariables.set({json.dumps(key)}, {accessor});")
        script.append("}")
        items.append(
            {
                "name": step["name"],
                "request": request,
                "event": [
                    {"listen": "test", "script": {"type": "text/javascript", "exec": script}}
                ],
            }
        )
    return {
        "info": {
            "name": "CQA frontend handoff (manual synthetic demo)",
            "description": scenario["description"],
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [{"key": "base_url", "value": "http://127.0.0.1:8000"}],
        "item": items,
    }


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export_kit(client: httpx.Client, output: Path) -> int:
    scenario = read_scenario()
    result = run_scenario(client, scenario)
    response = client.get("/openapi.json")
    if response.status_code != 200:
        raise DemoFailure(f"OpenAPI export failed (HTTP {response.status_code})")
    output.mkdir(parents=True, exist_ok=True)
    save_json(output / "responses.json", result)
    save_json(output / "openapi.json", response.json())
    save_json(output / "cqa.postman_collection.json", collection(scenario))
    return len(result["responses"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", help="Optional running API origin; creates synthetic demo records")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "frontend-kit")
    args = parser.parse_args()
    try:
        if args.api:
            url = urlsplit(args.api)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
            ):
                parser.error("--api must be an HTTP(S) origin without credentials or query")
            with httpx.Client(base_url=args.api.rstrip("/"), timeout=30) as client:
                count = export_kit(client, args.output)
        else:
            from fastapi.testclient import TestClient

            from backend.config import Settings
            from backend.main import create_app

            # A separate database and explicit settings keep real data/secrets out of the kit.
            with TemporaryDirectory(prefix="cqa-frontend-") as temp:
                settings = Settings(database_path=Path(temp) / "demo.sqlite3", organizer_api_url="")
                with TestClient(create_app(settings)) as client:
                    count = export_kit(client, args.output)
        print(f"OK: {count} API steps. Kit saved to {args.output.resolve()}")
    except (DemoFailure, httpx.RequestError, OSError, ValueError, KeyError, IndexError) as exc:
        message = str(exc) if isinstance(exc, DemoFailure) else type(exc).__name__
        parser.exit(1, f"Frontend kit failed: {message}\n")


if __name__ == "__main__":
    main()
