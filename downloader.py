import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx
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



_PAGE_META_KEYS = {
    "og:video",
    "og:video:url",
    "og:video:secure_url",
    "twitter:player:stream",
    "twitter:player",
}
_MAX_HTML_BYTES = 2 * 1024 * 1024


class _MediaCandidateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[str] = []
        self._json_ld = False
        self._json_chunks: list[str] = []

    def _add(self, value: str | None) -> None:
        value = (value or "").strip()
        if value and value not in self.candidates:
            self.candidates.append(value)

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = {str(k).lower(): v for k, v in attrs}
        tag = tag.lower()
        if tag == "meta":
            key = str(attrs.get("property") or attrs.get("name") or "").lower()
            if key in _PAGE_META_KEYS:
                self._add(attrs.get("content"))
        elif tag in {"video", "source"}:
            self._add(attrs.get("src"))
        elif tag == "iframe":
            src = (attrs.get("src") or "").strip()
            if any(token in src.lower() for token in ("youtube", "youtu.be", "vimeo", "dailymotion", "twitch", "player", "embed")):
                self._add(src)
        elif tag == "script" and str(attrs.get("type") or "").lower() == "application/ld+json":
            self._json_ld = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._json_ld:
            self._json_ld = False

    def handle_data(self, data: str) -> None:
        if self._json_ld:
            self._json_chunks.append(data)


def _walk_json_media(value: Any, found: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"contentUrl", "embedUrl", "url"} and isinstance(item, str):
                if item.startswith(("http://", "https://")) and item not in found:
                    found.append(item)
            else:
                _walk_json_media(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_json_media(item, found)


def _public_page_media_candidates(url: str) -> tuple[str, list[str]]:
    current = url
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    with httpx.Client(follow_redirects=False, timeout=20.0, headers=headers) as client:
        for _ in range(6):
            if not is_supported_external_url(current):
                raise ValueError("Redirect points to a private or unsupported address")
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("Redirect response has no Location header")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
                if content_type.startswith("video/") or Path(urlparse(current).path).suffix.lower() in _VIDEO_SUFFIXES:
                    return current, [current]
                if "html" not in content_type and "xhtml" not in content_type:
                    return current, []
                body = bytearray()
                for chunk in response.iter_bytes(64 * 1024):
                    body.extend(chunk)
                    if len(body) > _MAX_HTML_BYTES:
                        break
                text = bytes(body[:_MAX_HTML_BYTES]).decode(response.encoding or "utf-8", errors="replace")
                parser = _MediaCandidateParser()
                parser.feed(text)
                json_candidates: list[str] = []
                for raw in parser._json_chunks:
                    try:
                        _walk_json_media(json.loads(raw), json_candidates)
                    except Exception:
                        continue
                candidates = parser.candidates + json_candidates
                normalized: list[str] = []
                for candidate in candidates:
                    candidate = urljoin(current, candidate)
                    if is_supported_external_url(candidate) and candidate not in normalized:
                        normalized.append(candidate)
                    if len(normalized) >= 15:
                        break
                return current, normalized
    raise RuntimeError("Too many redirects")


def _is_drm_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("drm", "encrypted media", "widevine", "playready", "fairplay"))


def _normalized_info(info: dict[str, Any], url: str, *, strategy: str = "extractor") -> dict[str, Any]:
    entries = info.get("entries") if isinstance(info, dict) else None
    if entries:
        info = next((item for item in entries if item), None) or {}
    formats = info.get("formats") or [] if isinstance(info, dict) else []
    video_formats = [
        fmt for fmt in formats
        if isinstance(fmt, dict) and fmt.get("vcodec") not in {None, "none"}
    ]
    max_height = max((int(fmt.get("height") or 0) for fmt in video_formats), default=0)
    max_width = max((int(fmt.get("width") or 0) for fmt in video_formats), default=0)
    width = int(info.get("width") or max_width or 0)
    height = int(info.get("height") or max_height or 0)
    return {
        "id": str(info.get("id") or ""),
        "title": str(info.get("title") or "").strip()[:255],
        "description": str(info.get("description") or "").strip()[:5000],
        "duration": int(info.get("duration") or 0),
        "width": width,
        "height": height,
        "fps": float(info.get("fps") or 0),
        "thumbnail": str(info.get("thumbnail") or ""),
        "extractor": str(info.get("extractor_key") or info.get("extractor") or "Generic"),
        "webpage_url": str(info.get("webpage_url") or url),
        "uploader": str(info.get("uploader") or info.get("channel") or ""),
        "format_count": len(formats),
        "is_live": bool(info.get("is_live")),
        "strategy": strategy,
    }


def _ydl_probe(url: str, *, referer: str = "") -> dict[str, Any]:
    opts = _external_ydl_opts(None, "max")
    opts.update({"skip_download": True})
    if referer:
        opts["http_headers"]["Referer"] = referer
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or not isinstance(info, dict):
        raise RuntimeError("Media metadata could not be read")
    return _normalized_info(info, url)


def _probe_page_candidates(url: str) -> dict[str, Any]:
    page_url, candidates = _public_page_media_candidates(url)
    errors: list[str] = []
    for candidate in candidates:
        try:
            if candidate == page_url:
                result = _direct_http_probe(candidate)
                result["strategy"] = "direct"
                result["discovered_from"] = page_url
                return result
            try:
                result = _ydl_probe(candidate, referer=page_url)
                result["strategy"] = "embedded"
                result["discovered_from"] = page_url
                return result
            except Exception as exc:
                if _is_drm_failure(exc):
                    raise
                result = _direct_http_probe(candidate, referer=page_url)
                result["strategy"] = "embedded-direct"
                result["discovered_from"] = page_url
                return result
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("No downloadable video stream was discovered on this public page" + (f": {errors[-1][:180]}" if errors else ""))


def _external_ydl_opts(job_dir: Path | None = None, quality: str = "max") -> dict[str, Any]:
    opts: dict[str, Any] = {
        "format": _QUALITY_FORMATS.get(str(quality), _QUALITY_FORMATS["max"]),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
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
        free = shutil.disk_usage(DOWNLOAD_DIR).free
        opts["max_filesize"] = max(0, free - 1024 * 1024 * 1024)
    if Config.FFMPEG_PATH:
        opts["ffmpeg_location"] = Config.FFMPEG_PATH
    return opts


def _direct_filename(url: str, content_type: str = "") -> str:
    raw = Path(unquote(urlparse(url).path)).name
    suffix = Path(raw).suffix.lower()
    if suffix not in _VIDEO_SUFFIXES:
        suffix = {
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
            "video/x-matroska": ".mkv",
        }.get((content_type or "").split(";", 1)[0].lower(), ".mp4")
    stem = Path(raw).stem if raw else "direct-video"
    safe_stem = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in stem)[:100]
    return f"{safe_stem or 'direct-video'}{suffix}"


def _direct_http_probe(url: str, *, referer: str = "") -> dict[str, Any]:
    current = url
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
        "Range": "bytes=0-0",
        "Accept": "*/*",
    }
    if referer:
        headers["Referer"] = referer
    with httpx.Client(follow_redirects=False, timeout=20.0, headers=headers) as client:
        for _ in range(6):
            if not is_supported_external_url(current):
                raise ValueError("Redirect points to a private or unsupported address")
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("Redirect response has no Location header")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
                suffix = Path(urlparse(current).path).suffix.lower()
                if not content_type.startswith("video/") and suffix not in _VIDEO_SUFFIXES:
                    raise RuntimeError(f"Direct URL is not recognized as video media ({content_type or 'unknown type'})")
                size_raw = response.headers.get("content-length") or "0"
                try:
                    size = int(size_raw)
                except ValueError:
                    size = 0
                name = _direct_filename(current, content_type)
                return {
                    "id": "",
                    "title": Path(name).stem.replace("_", " ").strip()[:255],
                    "description": "",
                    "duration": 0,
                    "width": 0,
                    "height": 0,
                    "fps": 0.0,
                    "thumbnail": "",
                    "extractor": "Direct HTTP",
                    "webpage_url": current,
                    "uploader": urlparse(current).hostname or "",
                    "file_size": size,
                }
    raise RuntimeError("Too many redirects")


def _download_direct_http_video(url: str, job_dir: Path, *, referer: str = "") -> tuple[Path, dict[str, Any]]:
    current = url
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
        "Accept": "*/*",
    }
    if referer:
        headers["Referer"] = referer
    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(30.0, read=60.0), headers=headers) as client:
        for _ in range(6):
            if not is_supported_external_url(current):
                raise ValueError("Redirect points to a private or unsupported address")
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("Redirect response has no Location header")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
                suffix = Path(urlparse(current).path).suffix.lower()
                if not content_type.startswith("video/") and suffix not in _VIDEO_SUFFIXES:
                    raise RuntimeError(f"Direct URL is not recognized as video media ({content_type or 'unknown type'})")

                expected = 0
                try:
                    expected = int(response.headers.get("content-length") or 0)
                except ValueError:
                    expected = 0
                free = shutil.disk_usage(DOWNLOAD_DIR).free
                reserve = 1024 * 1024 * 1024
                if expected and expected > max(0, free - reserve):
                    raise RuntimeError("Not enough free disk space for this video")

                target = job_dir / _direct_filename(current, content_type)
                written = 0
                with target.open("wb") as fh:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        written += len(chunk)
                        if written > max(0, free - reserve):
                            raise RuntimeError("Download stopped to preserve server disk space")

                if not _has_video_stream(target):
                    raise RuntimeError("Downloaded direct file has no valid video stream")
                return target, {
                    "id": "",
                    "title": target.stem.replace("_", " ")[:255],
                    "duration": 0,
                    "width": 0,
                    "height": 0,
                    "fps": 0.0,
                    "extractor": "Direct HTTP",
                    "webpage_url": current,
                    "file_size": target.stat().st_size,
                }
    raise RuntimeError("Too many redirects")


def probe_external_video(url: str) -> dict[str, Any]:
    if not is_supported_external_url(url):
        raise ValueError("Only public http/https media URLs are allowed")

    first_error: Exception | None = None
    try:
        return _ydl_probe(url)
    except Exception as exc:
        first_error = exc
        if _is_drm_failure(exc):
            raise RuntimeError("This source appears to use DRM/encrypted playback and cannot be imported") from exc

    try:
        result = _direct_http_probe(url)
        result["strategy"] = "direct"
        result["format_count"] = 1
        return result
    except Exception:
        pass

    try:
        return _probe_page_candidates(url)
    except Exception as exc:
        detail = str(exc).strip() or str(first_error or "unsupported source")
        raise RuntimeError(
            "No downloadable public video was found. The page may require login, use DRM, block server-side extraction, "
            f"or expose no public media stream. Detail: {detail[:260]}"
        ) from exc




def _apply_source_cookies(opts: dict[str, Any], job_dir: Path, url: str) -> None:
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
        return
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


def _download_with_ytdlp(
    url: str,
    job_dir: Path,
    quality: str,
    *,
    strategy: str,
    referer: str = "",
) -> tuple[Path, dict[str, Any]]:
    opts = _external_ydl_opts(job_dir, quality)
    _apply_source_cookies(opts, job_dir, url)
    if referer:
        opts["http_headers"]["Referer"] = referer
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info or not isinstance(info, dict):
            raise RuntimeError("Media download returned no metadata")
    video_path = _select_downloaded_video(job_dir)
    metadata = _normalized_info(info, url, strategy=strategy)
    metadata["file_size"] = video_path.stat().st_size
    return video_path, metadata


def _download_from_page_candidates(url: str, job_dir: Path, quality: str) -> tuple[Path, dict[str, Any]]:
    page_url, candidates = _public_page_media_candidates(url)
    errors: list[str] = []
    for candidate in candidates:
        try:
            if candidate == page_url:
                path, metadata = _download_direct_http_video(candidate, job_dir)
                metadata["strategy"] = "direct"
            else:
                try:
                    path, metadata = _download_with_ytdlp(
                        candidate,
                        job_dir,
                        quality,
                        strategy="embedded",
                        referer=page_url,
                    )
                except Exception as exc:
                    if _is_drm_failure(exc):
                        raise
                    path, metadata = _download_direct_http_video(candidate, job_dir, referer=page_url)
                    metadata["strategy"] = "embedded-direct"
            metadata["discovered_from"] = page_url
            return path, metadata
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError(
        "No downloadable embedded video stream was found"
        + (f": {errors[-1][:220]}" if errors else "")
    )


def download_external_video(url: str, *, quality: str = "max") -> tuple[str, dict[str, Any]]:
    if not is_supported_external_url(url):
        raise ValueError("Only public http/https media URLs are allowed")

    job_dir = DOWNLOAD_DIR / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        logger.info("Smart downloading long-form media: %s", url)
        first_error: Exception | None = None

        try:
            video_path, metadata = _download_with_ytdlp(url, job_dir, quality, strategy="extractor")
        except Exception as exc:
            first_error = exc
            if _is_drm_failure(exc):
                raise RuntimeError("This source appears to use DRM/encrypted playback and cannot be imported") from exc

            try:
                logger.info("Extractor failed; trying safe direct-media fallback")
                video_path, metadata = _download_direct_http_video(url, job_dir)
                metadata["strategy"] = "direct"
            except Exception:
                logger.info("Direct-media fallback failed; scanning public page for embedded media")
                try:
                    video_path, metadata = _download_from_page_candidates(url, job_dir, quality)
                except Exception as page_exc:
                    detail = str(page_exc).strip() or str(first_error or "unsupported source")
                    raise RuntimeError(
                        "No downloadable public video was found. The page may require login, use DRM, block server-side "
                        f"extraction, or expose no public media stream. Detail: {detail[:300]}"
                    ) from page_exc

        logger.info(
            "Long-form media ready via %s: %s (%s bytes)",
            metadata.get("strategy") or "unknown",
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