"""Build and verify the backend in an isolated Docker project with synthetic data only."""

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from backend.config import Settings
from backend.main import create_app
from scripts.frontend_kit import ROOT, read_scenario, run_scenario


class Compose:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.project = "replimetr-smoke-" + uuid4().hex
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COMPOSE_", "CQA_", "REPLIMETR_"))
        }
        self.env.update(COMPOSE_DISABLE_ENV_FILE="1", REPLIMETR_PORT="0")

    def run(self, *args: str, timeout: int = 120) -> str:
        result = subprocess.run(
            ["docker", "compose", "--project-name", self.project, "--file", "compose.yaml", *args],
            cwd=self.directory,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return result.stdout.strip()

    def start(self, *args: str) -> None:
        self.run("up", "--detach", "--wait", "--wait-timeout", "90", *args)

    def client(self) -> httpx.Client:
        address = self.run("port", "backend", "8000")
        assert address.startswith("127.0.0.1:"), "Smoke API must be loopback-only"
        return httpx.Client(base_url="http://" + address, timeout=30, trust_env=False)

    def python(self, source: str) -> str:
        return self.run("exec", "-T", "backend", "python", "-c", source)


def prepare_context(directory: Path) -> None:
    # Do not copy the developer's .env, database, git credentials, or frontend.
    for name in (
        "Dockerfile",
        ".dockerignore",
        "compose.yaml",
        "pyproject.toml",
        "requirements-lock.txt",
    ):
        shutil.copyfile(ROOT / name, directory / name)
    for package in ("backend", "ai"):
        for path in (ROOT / package).rglob("*"):
            if path.is_file() and (
                path.suffix == ".py" or path.name in {"schema.sql", "default_checklist.json"}
            ):
                target = directory / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
    (directory / ".env.docker").write_text(
        "CQA_CORS_ORIGINS=https://frontend.example.invalid\n"
        "CQA_ORGANIZER_API_URL=\nCQA_TARIFFS_PATH=\n"
        "REPLIMETR_LITERAL_TEST=value$kept#literal\n",
        encoding="utf-8",
    )


def snapshot(client: httpx.Client, variables: dict) -> dict:
    paths = {
        "evaluation": f"/api/v1/evaluations/{variables['evaluation_id']}",
        "checklist": f"/api/v1/checklists/{variables['checklist_id']}/versions/1",
        "export": f"/api/v1/datasets/{variables['dataset_id']}/export",
        "batches": "/api/v1/evaluation-batches",
    }
    records = {}
    for name, path in paths.items():
        response = client.get(path)
        response.raise_for_status()
        records[name] = response.json()
    # Export generation time changes; the saved data and history must not.
    records["export"].pop("exported_at", None)
    return records


def check_api(client: httpx.Client, expected_schema: dict, variables: dict) -> None:
    assert client.get("/openapi.json").json() == expected_schema
    assert client.get("/docs").status_code == 200
    assert client.get("/health").json() == {
        "status": "ok",
        "database": "sqlite",
        "ai_configured": False,
    }
    request = {
        "conversation_id": variables["conversation_id"],
        "checklist_id": variables["checklist_id"],
        "checklist_version": 1,
    }
    response = client.post("/api/v1/evaluations", json=request)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_NOT_CONFIGURED"
    response = client.get("/health", headers={"Origin": "https://frontend.example.invalid"})
    assert response.headers["access-control-allow-origin"] == "https://frontend.example.invalid"
    response = client.get("/health", headers={"Origin": "https://untrusted.example.invalid"})
    assert "access-control-allow-origin" not in response.headers


def verify(compose: Compose, expected_schema: dict) -> None:
    compose.run("config", "--quiet")
    print("Building backend image (production dependencies only)...", flush=True)
    compose.run("build", "--pull", timeout=600)
    compose.start()
    print("Container healthy; checking runtime and the manual API scenario...", flush=True)
    compose.python(
        "import os, sqlite3; from pathlib import Path; from ai.app import build_evaluator; "
        "assert not build_evaluator().configured; "
        "assert os.getuid() == 10001; "
        "assert os.environ['REPLIMETR_LITERAL_TEST'] == 'value$kept#literal'; "
        "assert os.environ['CQA_DATABASE_PATH'] == '/var/lib/replimetr/cqa.sqlite3'; "
        "assert not any(Path('/app', p).exists() for p in ['.env', '.env.docker', 'frontend']); "
        "db = sqlite3.connect(os.environ['CQA_DATABASE_PATH']); "
        "assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'; db.close()"
    )
    with compose.client() as client:
        scenario = run_scenario(client, read_scenario())
        assert len(scenario["responses"]) == 16
        variables = scenario["variables"]
        check_api(client, expected_schema, variables)
        before = snapshot(client, variables)

    compose.run("restart", "backend")
    compose.start()
    with compose.client() as client:
        assert snapshot(client, variables) == before

    container_id = compose.run("ps", "--quiet", "backend")
    # Remove the container and network, but deliberately retain the named volume.
    compose.run("down", "--timeout", "30")
    compose.start()
    assert compose.run("ps", "--quiet", "backend") != container_id
    with compose.client() as client:
        assert snapshot(client, variables) == before
    print("Restart and container recreation preserved evaluations and appeals.", flush=True)

    compose.python("from pathlib import Path; Path('/var/lib/replimetr/backups').mkdir()")
    backup = "/var/lib/replimetr/backups/smoke.sqlite3"
    base = ("exec", "-T", "backend", "python", "-m", "backend.backup")
    compose.run(*base, "create", "--database", "/var/lib/replimetr/cqa.sqlite3", "--output", backup)
    compose.run(*base, "verify", "--backup", backup)
    compose.run(
        *base, "restore", "--backup", backup, "--output", "/var/lib/replimetr/restored.sqlite3"
    )
    with (compose.directory / ".env.docker").open("a", encoding="utf-8") as config:
        config.write("CQA_DATABASE_PATH=/var/lib/replimetr/restored.sqlite3\n")
    compose.start("--force-recreate")
    with compose.client() as client:
        check_api(client, expected_schema, variables)
        assert snapshot(client, variables) == before
    print("OK: Docker startup, 16 API steps, API schema, CORS, persistence, backup and restore.")


def main() -> None:
    # Fail before creating resources if Docker is unavailable (for example, stopped Desktop).
    subprocess.run(["docker", "info", "--format", "{{.OSType}}"], check=True, timeout=30)
    with TemporaryDirectory(prefix="replimetr-docker-smoke-") as temp:
        directory = Path(temp).resolve()
        prepare_context(directory)
        settings = Settings(
            database_path=directory / "unused.sqlite3", organizer_api_url="", tariffs=()
        )
        schema = create_app(settings).openapi()
        compose = Compose(directory)
        try:
            verify(compose, schema)
        except BaseException as exc:
            # This context contains synthetic data only, never the working .env.
            if isinstance(exc, subprocess.CalledProcessError):
                print(exc.stdout or "")
                print(exc.stderr or "")
            try:
                print(compose.run("logs", "--no-color", "--tail", "100", timeout=20))
            except (subprocess.SubprocessError, OSError):
                print("Docker logs unavailable; see the original failure.")
            raise
        finally:
            # The unique project owns only this run's temporary containers, volume and image.
            # No global prune or command against the real 'replimetr' project is used.
            compose.run("down", "--volumes", "--rmi", "local", "--timeout", "30")


if __name__ == "__main__":
    main()
