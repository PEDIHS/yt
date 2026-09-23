import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _parse_admin_ids(value: str) -> set[int]:
    result: set[int] = set()
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError:
            raise RuntimeError(f"Invalid TELEGRAM_ADMIN_IDS value: {item}")
    return result


class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_BOT_TOKEN_FILE = os.getenv("TELEGRAM_BOT_TOKEN_FILE", "").strip()
    TELEGRAM_ADMIN_IDS = _parse_admin_ids(os.getenv("TELEGRAM_ADMIN_IDS", ""))

    PANEL_USERNAME = os.getenv("PANEL_USERNAME", "admin").strip()
    PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "").strip()
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production").strip()
    TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()

    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
    CLIENT_SECRET_FILE = os.getenv("CLIENT_SECRET_FILE", str(BASE_DIR / "client_secret.json"))
    DEFAULT_HASHTAGS = os.getenv("DEFAULT_HASHTAGS", "#Shorts #YouTubeShorts #reels")
    FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip()

    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")
    MAX_WORKERS = max(1, int(os.getenv("MAX_WORKERS", "2")))

    PANEL_HOST = os.getenv("PANEL_HOST", "0.0.0.0")
    PANEL_PORT = int(os.getenv("PANEL_PORT", "8080"))

    OAUTH_LINK_MINUTES = int(os.getenv("OAUTH_LINK_MINUTES", "10"))
    MAX_UPLOAD_HISTORY = int(os.getenv("MAX_UPLOAD_HISTORY", "100"))

    @classmethod
    def validate_panel(cls) -> None:
        if not cls.PANEL_PASSWORD:
            raise RuntimeError("PANEL_PASSWORD must be set in .env")
        if cls.SECRET_KEY == "change-me-in-production":
            raise RuntimeError("SECRET_KEY must be changed in .env")
        if not Path(cls.CLIENT_SECRET_FILE).exists():
            raise RuntimeError(f"Google OAuth client file not found: {cls.CLIENT_SECRET_FILE}")

    @classmethod
    def validate_bot(cls) -> None:
        token_file_ok = bool(cls.TELEGRAM_BOT_TOKEN_FILE and Path(cls.TELEGRAM_BOT_TOKEN_FILE).is_file())
        if not cls.TELEGRAM_BOT_TOKEN and not token_file_ok:
            raise RuntimeError("Configure TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN_FILE")
