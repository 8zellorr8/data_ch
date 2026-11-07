"""Rehearse real HTTP, restart and submission CLIs using disposable data and no AI."""

import argparse
import json
import multiprocessing
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from multiprocessing.connection import Connection
from multiprocessing.synchronize import Event
from pathlib import Path
from threading import Thread
from types import TracebackType
from typing import Any, Self

import httpx

from scripts.frontend_kit import ROOT, DemoFailure, read_scenario, resolve, run_scenario, save_json


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DemoFailure(message)


def isolated_environment(directory: Path) -> dict[str, str]:
    # Allowlist OS essentials, not application secrets, proxy credentials or Python hooks.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(
        PYTHONPATH=str(ROOT),
        PYTHONUTF8="1",
        PYTHONUNBUFFERED="1",
        PYTHONDONTWRITEBYTECODE="1",
        CQA_DATABASE_PATH=str(directory / "rehearsal.sqlite3"),
        CQA_ORGANIZER_API_URL="",
        CQA_TARIFFS_PATH="",
    )
    return env


def serve(directory: Path, ready: Connection, stop: Event) -> None:
    """Spawn target: import the production app only after isolating its environment."""
    env = isolated_environment(directory)
    os.environ.clear()
    os.environ.update(env)
    os.chdir(directory)
    with (directory / "server.log").open("a", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            import uvicorn

            from backend.main import app

            with socket.socket() as listener:
                # Bind in the child and retain the socket: no free-port check/use race.
                listener.bind(("127.0.0.1", 0))
                server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))

                def shutdown() -> None:
                    stop.wait()
                    server.should_exit = True

                Thread(target=shutdown, daemon=True).start()
                ready.send(listener.getsockname()[1])
                ready.close()
                server.run(sockets=[listener])


class LocalServer:
    """Own one spawned process; stop only that process, including startup failures."""

    def __init__(self, directory: Path) -> None:
        context = multiprocessing.get_context("spawn")
        self.stop_requested = context.Event()
        self.ready, sender = context.Pipe(duplex=False)
        self.sender = sender
        self.process = context.Process(target=serve, args=(directory, sender, self.stop_requested))

    def __enter__(self) -> Self:
        try:
            self.process.start()
            self.sender.close()
            require(self.ready.poll(20), "Local server did not bind a port within 20 seconds")
            try:
                port = self.ready.recv()
            except EOFError:
                raise DemoFailure(
                    "Local server exited during startup before reporting a port"
                ) from None
            self.url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            with httpx.Client(timeout=0.5, trust_env=False) as client:
                while self.process.is_alive() and time.monotonic() < deadline:
                    try:
                        response = client.get(self.url + "/health")
                        if response.status_code == 200:
                            require(
                                response.json()["ai_configured"] is False, "AI must be disabled"
                            )
                            return self
                    except httpx.RequestError:
                        pass  # Only startup health reads are retried; never a mutation.
                    time.sleep(0.05)
            raise DemoFailure("Local server failed to become healthy; inspect the startup stage")
        except BaseException:
            if self.process.pid is not None:
                self.close(strict=False)
            raise
        finally:
            self.sender.close()
            self.ready.close()

    def close(self, *, strict: bool = True) -> None:
        self.stop_requested.set()
        self.process.join(10)
        forced = self.process.is_alive()
        if forced:
            self.process.terminate()
            self.process.join(5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(5)
        require(not self.process.is_alive(), "Could not stop the owned rehearsal process")
        if strict:
            require(
                not forced and self.process.exitcode == 0, "Rehearsal server did not exit cleanly"
            )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close(strict=exc_type is None)


def call(
    client: httpx.Client,
    method: str,
    path: str,
    status: int = 200,
    body: dict[str, Any] | None = None,
) -> Any:
    response = client.request(method, path, json=body)
    require(
        response.status_code == status,
        f"{method} {path}: expected {status}, got {response.status_code}",
    )
    return response.json()


def verify_manual(client: httpx.Client) -> tuple[dict[str, Any], dict[str, Any]]:
    scenario = read_scenario()
    bodies = {step["name"]: step.get("body") for step in scenario["steps"]}
    result = run_scenario(client, scenario)
    variables = result["variables"]
    original = result["responses"]["12-resolve-appeal"]["body"]
    decision = resolve(bodies["12-resolve-appeal"], variables)
    decision["expected_revision"] = 1
    replay = call(
        client, "POST", f"/api/v1/appeals/{variables['appeal_id']}/decision", 409, decision
    )
    require(replay["error"]["code"] == "APPEAL_CLOSED", "Repeated decision was not rejected")
    require(
        call(client, "GET", f"/api/v1/evaluations/{original['id']}") == original,
        "Repeated decision changed the stored assessment",
    )

    definition = deepcopy(bodies["07-custom-checklist"])
    definition["criteria"][1]["rule"] = "Synthetic version 2: explain where to find the guide."
    version = call(
        client,
        "POST",
        f"/api/v1/checklists/{variables['checklist_id']}/versions",
        201,
        {"expected_version": 1, "definition": definition},
    )
    require(version["version"] == 2, "New checklist version was not created")
    manual = resolve(bodies["08-manual-evaluation"], variables)
    revised = call(
        client, "POST", "/api/v1/evaluations/manual", 201, {**manual, "checklist_version": 2}
    )
    require(revised["checklist"]["criteria"] == version["criteria"], "New rubric was not applied")
    require(
        call(client, "GET", f"/api/v1/evaluations/{original['id']}") == original,
        "A new rubric changed the old assessment",
    )

    ai_request = {
        key: manual[key] for key in ("conversation_id", "checklist_id", "checklist_version")
    }
    refused = call(client, "POST", "/api/v1/evaluations", 503, ai_request)
    require(refused["error"]["code"] == "AI_NOT_CONFIGURED", "Expected unconfigured AI refusal")
    history = call(
        client, "GET", f"/api/v1/conversations/{variables['conversation_id']}/evaluations"
    )
    require(
        history["total"] == 2 and all(r["origin"] == "manual" for r in history["items"]),
        "Refused AI request created an assessment",
    )
    print(
        "PASS: 16 existing API steps, rubric v2, appeal replay; expected AI_NOT_CONFIGURED",
        flush=True,
    )
    return variables, manual


def synthetic_dataset(split: str) -> dict[str, Any]:
    example = next(s for s in read_scenario()["steps"] if s["name"] == "02-import")["body"][
        "records"
    ][0]
    records = []
    for index in range(65 if split == "dev" else 10):
        record = deepcopy(example)
        record["conversation_id"] = f"synthetic-rehearsal-{split}-{index + 1}"
        if split == "dev":
            record["language"] = "RU" if index < 30 or index >= 60 else "KZ" if index < 45 else "EN"
            if index >= 60:
                record["channel"] = "voice call"
                for turn, message in enumerate(record["transcript"]):
                    message["timestamp"] = f"2026-01-01T09:00:0{turn}+00:00"
        if record["language"] == "KZ":
            record["transcript"][0]["text"] = "Сәлеметсіз бе! Нұсқаулық қайда?"
            record["transcript"][1]["text"] = (
                "Сәлеметсіз бе, мен Аннамын. Нұсқаулық Көмек бөлімінде. Сау болыңыз!"
            )
        elif record["language"] == "EN":
            record["transcript"][0]["text"] = "Hello! Where is the guide?"
            record["transcript"][1]["text"] = "Hello, I'm Anna. The guide is in Help. Goodbye!"
        records.append(record)
    return {
        "name": f"SYNTHETIC REHEARSAL {split.upper()} — no AI",
        "group_id": "REHEARSAL",
        "records": records,
    }


def cli(directory: Path, module: str, *args: str | Path, expected: int = 0) -> str:
    try:
        result = subprocess.run(
            [sys.executable, "-m", module, *map(str, args)],
            cwd=directory,
            env=isolated_environment(directory),
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise DemoFailure(f"{module}: exceeded the 30-second CLI timeout") from None
    require(
        result.returncode == expected,
        f"{module}: expected exit {expected}, got {result.returncode}; {result.stderr.strip()[:300]}",
    )
    return result.stdout


def verify_submission(
    client: httpx.Client, directory: Path, url: str, split: str, manual: dict[str, Any]
) -> str:
    payload = synthetic_dataset(split)
    dataset_id: str = call(client, "POST", "/api/v1/datasets", 201, payload)["id"]
    ids = [r["conversation_id"] for r in payload["records"]]
    selection: dict[str, Any] = {
        "split": split,
        "team_name": "SYNTHETIC REHEARSAL — DO NOT SUBMIT",
        "model_mode": "local",
        "model_names": ["not-run-no-ai-rehearsal"],
        "items": [{"conversation_id": cid, "error": "AI_NOT_CONFIGURED"} for cid in ids],
    }
    manifest = directory / f"{split}-manifest.json"
    local_file, remote_file = directory / f"{split}-local.json", directory / f"{split}-remote.json"
    save_json(manifest, selection)
    shared = ("--dataset-id", dataset_id, "--manifest", manifest)
    local = ("--database", directory / "rehearsal.sqlite3", *shared)
    remote = ("--api", url, *shared)
    cli(directory, "backend.submission_cli", *local, "--output", local_file)
    cli(directory, "backend.submission_cli", *local, "--output", local_file, "--check")
    cli(directory, "backend.submission_remote", *remote, "--output", remote_file)
    document = json.loads(local_file.read_text(encoding="utf-8"))
    require(
        document == json.loads(remote_file.read_text(encoding="utf-8")), "Local/HTTP exports differ"
    )
    require(
        [row["conversation_id"] for row in document["results"]] == ids, "Export lost source IDs"
    )
    require(
        all(
            row["status"] == "error"
            and row["criteria"] == []
            and row["score"] is None
            and row["error"] == "AI_NOT_CONFIGURED"
            for row in document["results"]
        ),
        "An absent AI outcome was replaced by a score",
    )
    for source in (local, remote):
        report = json.loads(
            cli(
                directory,
                "backend.submission_check",
                *source,
                "--results-file",
                remote_file,
                "--json",
                expected=1,
            )
        )
        require(
            report["status"] == "export_ready_with_errors"
            and report["file_status"] == "matches"
            and report["successful_results"] == 0
            and report["error_results"] == len(ids)
            and report["failed_conversation_ids"] == ids,
            "Readiness hid unscored dialogues",
        )
    document["results"][0]["score"] = {"final_score": 0, "critical_flag": False}
    altered = directory / f"{split}-tampered.json"
    save_json(altered, document)
    report = json.loads(
        cli(
            directory,
            "backend.submission_check",
            *remote,
            "--results-file",
            altered,
            "--json",
            expected=2,
        )
    )
    require(report["issue_code"] == "RESULTS_MISMATCH", "Changed export was not rejected")
    cli(directory, "backend.submission_cli", *local, "--output", altered, "--check", expected=2)

    if split == "prod":
        conversation = call(client, "GET", f"/api/v1/datasets/{dataset_id}/conversations")["items"][
            0
        ]
        assessment = call(
            client,
            "POST",
            "/api/v1/evaluations/manual",
            201,
            {**manual, "conversation_id": conversation["id"]},
        )
        selection["items"][0] = {"conversation_id": ids[0], "evaluation_id": assessment["id"]}
        rejected = call(client, "POST", f"/api/v1/datasets/{dataset_id}/submission", 422, selection)
        require(
            rejected["error"]["code"] == "INVALID_SUBMISSION"
            and "original AI evaluations" in rejected["error"]["message"],
            "Manual assessment was not rejected as an AI submission",
        )
        save_json(manifest, selection)
        report = json.loads(
            cli(directory, "backend.submission_check", *local, "--json", expected=2)
        )
        require(report["issue_code"] == "INVALID_SUBMISSION", "CLI accepted a manual AI substitute")
    print(
        f"PASS: {split.upper()} {len(ids)} explicit errors, export parity, readiness exit 1, tamper rejection",
        flush=True,
    )
    return dataset_id


def snapshot(client: httpx.Client, dataset_ids: list[str], checklist_id: str) -> dict[str, Any]:
    datasets = []
    for dataset_id in dataset_ids:
        data = call(client, "GET", f"/api/v1/datasets/{dataset_id}/export")
        data.pop("exported_at", None)
        require(
            all(row["origin"] == "manual" for row in data["evaluations"]),
            "Unexpected AI assessment",
        )
        datasets.append(data)
    return {
        "datasets": datasets,
        "versions": [
            call(client, "GET", f"/api/v1/checklists/{checklist_id}/versions/{v}") for v in (1, 2)
        ],
        "batches": call(client, "GET", "/api/v1/evaluation-batches"),
    }


def rehearse() -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    require(
        not temp_root.is_relative_to(ROOT), "System temp directory must be outside the repository"
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="replimetr-rehearsal-", dir=temp_root) as temp:
        directory = Path(temp).resolve()
        with LocalServer(directory) as server:
            with httpx.Client(base_url=server.url, timeout=10, trust_env=False) as client:
                variables, manual = verify_manual(client)
                dataset_ids = [variables["dataset_id"]]
                for split in ("dev", "prod"):
                    dataset_ids.append(
                        verify_submission(client, directory, server.url, split, manual)
                    )
                before = snapshot(client, dataset_ids, variables["checklist_id"])
        with LocalServer(directory) as server:
            with httpx.Client(base_url=server.url, timeout=10, trust_env=False) as client:
                require(
                    snapshot(client, dataset_ids, variables["checklist_id"]) == before,
                    "Restart changed saved data, rubric versions, assessments or appeal history",
                )
        print(
            "PASS: process restart preserved all three datasets, versions and assessment history",
            flush=True,
        )
    print(
        f"OK: no-AI rehearsal completed in {time.monotonic() - started:.2f}s; temporary files removed"
    )
    print("This does not measure AI accuracy, provider availability or real PROD processing time.")


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        rehearse()
    except (
        DemoFailure,
        OSError,
        ValueError,
        KeyError,
        IndexError,
        EOFError,
        subprocess.SubprocessError,
        httpx.RequestError,
    ) as exc:
        message = str(exc) if isinstance(exc, DemoFailure) else f"{type(exc).__name__}: {exc}"
        print(f"Rehearsal failed: {message}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("Rehearsal interrupted; owned processes stopped", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
