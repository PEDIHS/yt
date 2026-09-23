import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yt_dlp

from config import Config, DOWNLOAD_DIR

logger = logging.getLogger("downloader")
logging.getLogger("yt_dlp").setLevel(logging.WARNING)

_ALLOWED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}
_ALLOWED_PREFIXES = ("/reel/", "/reels/", "/p/", "/tv/")


def is_supported_instagram_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in {"http", "https"} and parsed.hostname in _ALLOWED_HOSTS and parsed.path.startswith(_ALLOWED_PREFIXES)
    except Exception:
        return False


def download_video(url: str) -> Optional[str]:
    if not is_supported_instagram_url(url):
        raise ValueError("Unsupported Instagram URL")

    job_dir = DOWNLOAD_DIR / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(job_dir / "%(id)s.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "overwrites": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
        },
    }

    if Config.FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = Config.FFMPEG_PATH

    sessionid = os.getenv("INSTAGRAM_SESSIONID", "").strip()
    if sessionid:
        ydl_opts["http_headers"]["Cookie"] = f"sessionid={sessionid}"

    try:
        logger.info("Downloading Instagram media: %s", url)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None

        candidates = [p for p in job_dir.iterdir() if p.is_file() and p.stat().st_size > 0]
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(candidates[0])
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


def cleanup_download(file_path: Optional[str]) -> None:
    if not file_path:
        return
    try:
        path = Path(file_path).resolve()
        if DOWNLOAD_DIR.resolve() not in path.parents:
            return
        shutil.rmtree(path.parent, ignore_errors=True)
    except Exception:
        logger.exception("Failed to clean download directory")
