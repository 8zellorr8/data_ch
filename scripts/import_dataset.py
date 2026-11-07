"""Import a local JSON file without putting organizer URLs into browser code."""

import argparse
import json
from pathlib import Path

import httpx

from backend.models import DatasetImport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--name", default="Imported dataset")
    parser.add_argument("--group-id", default="DEV")
    args = parser.parse_args()
    try:
        raw = json.loads(args.path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict) and "isSuccess" in raw:
            if raw["isSuccess"] is not True:
                parser.error("The organizer response was unsuccessful")
            raw = {"name": args.name, "group_id": args.group_id, "records": raw["data"]["records"]}
        dataset = DatasetImport.model_validate(raw)
        response = httpx.post(
            args.api.rstrip("/") + "/api/v1/datasets", json=dataset.model_dump(), timeout=60
        )
        if response.status_code != 201:
            parser.error(
                f"Import failed (HTTP {response.status_code}); inspect server/API validation"
            )
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, httpx.RequestError) as exc:
        # Validation exceptions can include real transcript text; keep CLI errors concise.
        parser.error(
            f"Unable to import ({type(exc).__name__}); check file format and server availability"
        )


if __name__ == "__main__":
    main()
