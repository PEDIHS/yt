import ipaddress
import logging
import os
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yt_dlp

from config import Config, DOWNLOAD_DIR
from integrations import build_instagram_cookie_blob, resolve_instagram_cookie_blob, resolve_instagram_session

logger = logging.getLogger("downloader")
logging.getLogger("yt_dlp").setLevel(logging.WARNING)

_ALLOWED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}
_ALLOWED_PREFIXES = ("/reel/", "/reels/", "/p/", "/tv/")
_VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
_IGNORED_DOWNLOAD_NAMES = {"instagram.cookies.txt"}
_QUALITY_FORMATS = {
    "max": "bv*+ba/b",
    "2160": "bv*[height<=2160]+ba/b[height<=2160]/best[height<=2160]",
    "1440": "bv*[height<=1440]+ba/b[height<=1440]/best[height<=1440]",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]/best[height<=720]",
}


def is_supported_instagram_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in {"http", "https"} and parsed.hostname in _ALLOWED_HOSTS and parsed.path.startswith(_ALLOWED_PREFIXES)
    except Exception:
        return False


def _is_public_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for entry in addresses:
        raw = entry[4][0]
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def is_supported_external_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        return _is_public_host(parsed.hostname)
    except Exception:
        return False


def _external_ydl_opts(job_dir: Path | None = None, quality: str = "max") -> dict[str, Any]:
    opts: dict[str, Any] = {
        "format": _QUALITY_FORMATS.get(str(quality), _QUALITY_FORMATS["max"]),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 8,
        "fragment_retries": 8,
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 30,
        "overwrites": True,
        "continuedl": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36"
        },
    }
    if job_dir is not None:
        opts["outtmpl"] = str(job_dir / "%(id)s.%(ext)s")
    if Config.FFMPEG_PATH:
        opts["ffmpeg_location"] = Config.FFMPEG_PATH
    return opts


def probe_external_video(url: str) -> dict[str, Any]:
    if not is_supported_external_url(url):
        raise ValueError("Only public http/https media URLs are allowed")

    opts = _external_ydl_opts(None, "max")
    opts.update({"skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("Media metadata could not be read")

    entries = info.get("entries") if isinstance(info, dict) else None
    if entries:
        info = next((item for item in entries if item), None) or {}
    if not isinstance(info, dict):
        raise RuntimeError("Media metadata is invalid")

    return {
        "id": str(info.get("id") or ""),
        "title": str(info.get("title") or "").strip()[:255],
        "description": str(info.get("description") or "").strip()[:5000],
        "duration": int(info.get("duration") or 0),
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "fps": float(info.get("fps") or 0),
        "thumbnail": str(info.get("thumbnail") or ""),
        "extractor": str(info.get("extractor_key") or info.get("extractor") or ""),
        "webpage_url": str(info.get("webpage_url") or url),
        "uploader": str(info.get("uploader") or info.get("channel") or ""),
    }


def download_external_video(url: str, *, quality: str = "max") -> tuple[str, dict[str, Any]]:
    if not is_supported_external_url(url):
        raise ValueError("Only public http/https media URLs are allowed")

    job_dir = DOWNLOAD_DIR / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)
    opts = _external_ydl_opts(job_dir, quality)

    # Reuse the configured Instagram session only for Instagram URLs.
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() in _ALLOWED_HOSTS:
        cookie_blob = resolve_instagram_cookie_blob()
        if not cookie_blob:
            sessionid = resolve_instagram_session()
            if sessionid:
                cookie_blob = build_instagram_cookie_blob(sessionid)
        if cookie_blob:
            cookie_path = job_dir / "instagram.cookies.txt"
            cookie_path.write_text(cookie_blob, encoding="utf-8")
            cookie_path.chmod(0o600)
            opts["cookiefile"] = str(cookie_path)

    try:
        logger.info("Downloading external long-form media: %s", url)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise RuntimeError("Media download returned no metadata")

        video_path = _select_downloaded_video(job_dir)
        metadata = {
            "id": str(info.get("id") or ""),
            "title": str(info.get("title") or "")[:255],
            "duration": int(info.get("duration") or 0),
            "width": int(info.get("width") or 0),
            "height": int(info.get("height") or 0),
            "fps": float(info.get("fps") or 0),
            "extractor": str(info.get("extractor_key") or info.get("extractor") or ""),
            "webpage_url": str(info.get("webpage_url") or url),
            "file_size": video_path.stat().st_size,
        }
        logger.info(
            "Long-form media ready: %s (%s bytes)",
            video_path.name,
            video_path.stat().st_size,
        )
        return str(video_path), metadata
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


def _has_video_stream(path: Path) -> bool:
    if path.suffix.lower() not in _VIDEO_SUFFIXES:
        return False
    if path.stat().st_size < 1024:
        return False

    ffprobe = shutil.which("ffprobe")
    if not ffprobe and Config.FFMPEG_PATH:
        ffmpeg_path = Path(Config.FFMPEG_PATH)
        candidate = ffmpeg_path.with_name("ffprobe") if ffmpeg_path.is_file() else ffmpeg_path / "ffprobe"
        if candidate.exists():
            ffprobe = str(candidate)

    if not ffprobe:
        # Extension filtering is still much safer than accepting arbitrary
        # auxiliary files such as cookies.txt.
        return True

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.returncode == 0 and "video" in (result.stdout or "").lower()
    except Exception:
        logger.exception("ffprobe validation failed for %s", path.name)
        return False


def _select_downloaded_video(job_dir: Path) -> Path:
    candidates = []
    for path in job_dir.iterdir():
        if not path.is_file() or path.name in _IGNORED_DOWNLOAD_NAMES:
            continue
        if _has_video_stream(path):
            candidates.append(path)

    if not candidates:
        found = ", ".join(
            f"{p.name} ({p.stat().st_size} bytes)"
            for p in job_dir.iterdir()
            if p.is_file()
        )
        raise RuntimeError(f"Download produced no valid video file. Files: {found or 'none'}")

    # Prefer the largest validated media file. This avoids picking small
    # metadata/sidecar files even when they have a newer mtime.
    candidates.sort(key=lambda p: (p.stat().st_size, p.stat().st_mtime), reverse=True)
    return candidates[0]


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

    cookie_blob = resolve_instagram_cookie_blob()
    if not cookie_blob:
        sessionid = resolve_instagram_session()
        if sessionid:
            cookie_blob = build_instagram_cookie_blob(sessionid)

    if cookie_blob:
        cookie_path = job_dir / "instagram.cookies.txt"
        cookie_path.write_text(cookie_blob, encoding="utf-8")
        cookie_path.chmod(0o600)
        ydl_opts["cookiefile"] = str(cookie_path)

    try:
        logger.info("Downloading Instagram media: %s", url)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None

        video_path = _select_downloaded_video(job_dir)
        logger.info(
            "Instagram video ready: %s (%s bytes)",
            video_path.name,
            video_path.stat().st_size,
        )
        return str(video_path)
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
