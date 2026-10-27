import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    default_case_number: str
    interval_minutes: int
    database_path: Path
    browser_data_dir: Path
    headless: bool
    timeout_ms: int

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is required")
        database_path = Path(os.getenv("DATABASE_PATH", "data/edrsr.sqlite3"))
        browser_data_dir = Path(os.getenv("BROWSER_DATA_DIR", "data/browser-profile"))
        database_path.parent.mkdir(parents=True, exist_ok=True)
        browser_data_dir.mkdir(parents=True, exist_ok=True)
        return cls(token, os.getenv("DEFAULT_CASE_NUMBER", "761/29150/26"),
                   int(os.getenv("CHECK_INTERVAL_MINUTES", "60")), database_path,
                   browser_data_dir, _bool("HEADLESS", True), int(os.getenv("EDRSR_TIMEOUT_MS", "45000")))

