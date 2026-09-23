import os
import uuid
import logging
from typing import Optional
import yt_dlp

logger = logging.getLogger("downloader")
logging.getLogger("yt_dlp").setLevel(logging.ERROR)
DOWNLOAD_DIR = "downloads"

# Optional FFmpeg path. When unset, yt-dlp will use FFmpeg available on PATH.
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "").strip()

def _sync_download(url: str) -> Optional[str]:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    filename = f"{uuid.uuid4()}.mp4"
    filepath = os.path.join(DOWNLOAD_DIR, filename)

    ydl_opts = {
        "outtmpl": filepath,
        "format": "best[ext=mp4]/best",
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": True,
        "noplaylist": True,
        "extract_flat": False,
        "retries": 10,
        "fragment_retries": 10,
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
    }
    
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    sessionid = os.environ.get("INSTAGRAM_SESSIONID")
    if sessionid:
        ydl_opts["cookies"] = f"sessionid={sessionid};"
        logger.info("✅ استفاده از سشن اینستاگرام")

    try:
        logger.info(f"📥 در حال دانلود: {url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and info.get('requested_downloads'):
                downloaded_file = info['requested_downloads'][0].get('filepath')
                if downloaded_file and os.path.exists(downloaded_file):
                    logger.info(f"✅ دانلود شد: {downloaded_file}")
                    return downloaded_file
        
        files = os.listdir(DOWNLOAD_DIR)
        if files:
            latest_file = os.path.join(DOWNLOAD_DIR, sorted(files)[-1])
            if os.path.getsize(latest_file) > 0:
                logger.info(f"✅ فایل پیدا شد: {latest_file}")
                return latest_file
                
        logger.error("❌ فایل دانلود نشد")
        return None
        
    except Exception as e:
        logger.error(f"❌ خطا در دانلود: {e}")
        return None

def download_video(url: str) -> Optional[str]:
    return _sync_download(url)
