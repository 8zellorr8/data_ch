from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    database_path: Path = field(default_factory=lambda: Path(os.getenv("CQA_DATABASE_PATH", "data/cqa.sqlite3")))
    organizer_api_url: str = field(default_factory=lambda: os.getenv("CQA_ORGANIZER_API_URL", ""))
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        x.strip() for x in os.getenv("CQA_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000").split(",") if x.strip()
    ))
