import os
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

from pydantic import TypeAdapter

from .work_models import Tariff


def load_tariffs() -> tuple[Tariff, ...]:
    path = os.getenv("CQA_TARIFFS_PATH", "")
    if not path:
        return ()
    return TypeAdapter(tuple[Tariff, ...]).validate_json(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Settings:
    database_path: Path = field(
        default_factory=lambda: Path(os.getenv("CQA_DATABASE_PATH", "data/cqa.sqlite3"))
    )
    organizer_api_url: str = field(
        default_factory=lambda: os.getenv("CQA_ORGANIZER_API_URL", ""), repr=False
    )
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            x.strip()
            for x in os.getenv(
                "CQA_CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000",
            ).split(",")
            if x.strip()
        )
    )
    evaluation_timeout: float = field(
        default_factory=lambda: float(os.getenv("CQA_EVALUATION_TIMEOUT", "180"))
    )
    database_timeout: float = field(
        default_factory=lambda: float(os.getenv("CQA_DATABASE_TIMEOUT", "15"))
    )
    ai_concurrency: int = field(default_factory=lambda: int(os.getenv("CQA_AI_CONCURRENCY", "2")))
    batch_queue_limit: int = field(
        default_factory=lambda: int(os.getenv("CQA_BATCH_QUEUE_LIMIT", "1000"))
    )
    owner_lease_seconds: float = 30
    tariffs: tuple[Tariff, ...] = field(default_factory=load_tariffs)

    def __post_init__(self) -> None:
        for count in (self.ai_concurrency, self.batch_queue_limit):
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("Concurrency and queue limits must be positive integers")
        if not isfinite(self.owner_lease_seconds) or self.owner_lease_seconds < 3:
            raise ValueError("Owner lease must be at least 3 seconds")
        if len({t.model for t in self.tariffs}) != len(self.tariffs):
            raise ValueError("Only one active tariff per model is supported")
        for value in (self.evaluation_timeout, self.database_timeout):
            if not isfinite(value) or value <= 0:
                raise ValueError("Timeouts must be positive finite numbers")
