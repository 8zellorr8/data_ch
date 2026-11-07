import os
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path


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

    def __post_init__(self) -> None:
        for value in (self.evaluation_timeout, self.database_timeout):
            if not isfinite(value) or value <= 0:
                raise ValueError("Timeouts must be positive finite numbers")
