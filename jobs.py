from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import httpx

from config import Config
from db import SessionLocal
from downloader import cleanup_download, download_video
from integrations import get_secret, resolve_telegram_token
from models import TelegramAdmin, UploadJob, YouTubeChannel
from youtube import (
    discard_preflight_video,
    publish_checked_video,
    upload_to_youtube,
    wait_for_video_preflight,
)

logger = logging.getLogger("jobs")
_executor = ThreadPoolExecutor(max_workers=Config.MAX_WORKERS, thread_name_prefix="uploads")


def _telegram_targets_for_job(job_id: int) -> list[int]:
    targets: list[int] = []
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if job and job.telegram_user_id:
            targets.append(int(job.telegram_user_id))

        primary = (get_secret("telegram_primary_admin_id") or "").strip()
        if primary.isdigit():
            targets.append(int(primary))
        elif not targets:
            admin = db.query(TelegramAdmin).order_by(TelegramAdmin.created_at.asc()).first()
            if admin:
                targets.append(int(admin.user_id))

    return list(dict.fromkeys(targets))


def _send_telegram_job_event(
    job_id: int,
    *,
    event: str,
    video_url: str = "",
    error: str = "",
) -> None:
    token = resolve_telegram_token()
    if not token:
        return

    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if not job:
            return
        channel = db.get(YouTubeChannel, job.channel_id)
        title = job.title
        channel_name = (channel.label or channel.title) if channel else f"Channel #{job.channel_id}"
        privacy = job.privacy

    if event == "uploading":
        text = (
            f"🚀 ویدیو آماده انتشار است و در حال ارسال به YouTube می‌باشد.\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_name}\n"
            f"🔐 {privacy}\n"
            f"🧾 Job #{job_id}"
        )
        reply_markup = None
    elif event == "checking":
        text = (
            f"🛡 ویدیو به‌صورت Private آپلود شد و در حال بررسی قبل از انتشار است.\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_name}\n"
            f"🧾 Job #{job_id}"
        )
        reply_markup = None
    elif event == "copyright_blocked":
        text = (
            f"⛔ انتشار متوقف شد؛ YouTube در بررسی قبل از انتشار مشکل تشخیص داد.\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_name}\n"
            f"🧾 Job #{job_id}\n"
            f"گزارش: {(error or 'YouTube preflight rejected the video')[:700]}\n\n"
            f"ویدیوی Private از YouTube حذف شد و این Job از مسیر انتشار خارج شد."
        )
        reply_markup = None
    elif event == "completed":
        text = (
            f"✅ ویدیو با موفقیت منتشر شد.\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_name}\n"
            f"🔐 {privacy}\n"
            f"🔗 {video_url}\n"
            f"🧾 Job #{job_id}"
        )
        reply_markup = {
            "inline_keyboard": [[
                {"text": "▶️ مشاهده در YouTube", "url": video_url}
            ]]
        } if video_url else None
    else:
        text = (
            f"❌ انتشار ویدیو ناموفق بود.\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_name}\n"
            f"🧾 Job #{job_id}\n"
            f"خطا: {(error or 'Unknown error')[:700]}"
        )
        reply_markup = None

    for chat_id in _telegram_targets_for_job(job_id):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=10,
            )
            if response.status_code != 200:
                logger.warning(
                    "Telegram job event failed for job %s chat %s: HTTP %s",
                    job_id,
                    chat_id,
                    response.status_code,
                )
        except Exception:
            logger.exception("Telegram job event failed for job %s", job_id)


def create_job(
    channel_id: int,
    source_url: str,
    title: str,
    *,
    source: str,
    telegram_user_id: Optional[int] = None,
    description: str = "",
    hashtags: Optional[str] = None,
    privacy: Optional[str] = None,
) -> UploadJob:
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel or not channel.is_active:
            raise RuntimeError("Selected channel is unavailable")
        job = UploadJob(
            channel_id=channel.id,
            source=source,
            telegram_user_id=telegram_user_id,
            source_url=source_url.strip(),
            title=title.strip()[:255],
            description=(description or "").strip(),
            hashtags=(hashtags if hashtags is not None else channel.default_hashtags).strip(),
            privacy=(privacy or channel.default_privacy).strip(),
            status="queued",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job


def mark_job_failed(job_id: int, error: str) -> None:
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if not job:
            return
        job.status = "failed"
        job.error = str(error)[:4000]
        job.finished_at = datetime.utcnow()
        db.commit()


def enqueue_job(job_id: int):
    return _executor.submit(process_job, job_id)


def process_job(job_id: int) -> dict:
    file_path: Optional[str] = None
    try:
        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            if not job:
                raise RuntimeError("Upload job not found")
            job.status = "downloading"
            job.started_at = datetime.utcnow()
            job.error = None
            db.commit()
            source_url = job.source_url
            channel_id = job.channel_id
            title = job.title
            hashtags = job.hashtags
            description = job.description
            privacy = job.privacy

        file_path = download_video(source_url)
        if not file_path:
            raise RuntimeError("Instagram media could not be downloaded")

        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            job.status = "uploading"
            db.commit()

        _send_telegram_job_event(job_id, event="uploading")

        result = upload_to_youtube(
            file_path=file_path,
            channel_id=channel_id,
            title=title,
            hashtags=hashtags,
            description=description,
            privacy=privacy,
            force_private=True,
        )
        video_id = result["video_id"]

        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            job.status = "checking"
            job.video_id = video_id
            job.video_url = result["video_url"]
            db.commit()

        _send_telegram_job_event(job_id, event="checking")
        check = wait_for_video_preflight(channel_id, video_id)

        if not check.get("ok"):
            reason = str(check.get("reason") or "YouTube preflight failed")
            try:
                discard_preflight_video(channel_id, video_id)
            except Exception:
                logger.exception("Could not delete blocked private video %s", video_id)

            with SessionLocal() as db:
                job = db.get(UploadJob, job_id)
                job.status = "copyright_blocked" if check.get("copyright_signal") else "preflight_blocked"
                job.error = reason[:4000]
                job.video_id = None
                job.video_url = None
                job.finished_at = datetime.utcnow()
                db.commit()

            _send_telegram_job_event(
                job_id,
                event="copyright_blocked",
                error=reason,
            )
            return {
                "success": False,
                "blocked": True,
                "job_id": job_id,
                "error": reason,
                "copyright_signal": bool(check.get("copyright_signal")),
            }

        published = publish_checked_video(
            channel_id,
            video_id,
            privacy=privacy,
        )
        result.update(published)

        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            job.status = "completed"
            job.video_id = result["video_id"]
            job.video_url = result["video_url"]
            job.finished_at = datetime.utcnow()
            db.commit()
        _send_telegram_job_event(
            job_id,
            event="completed",
            video_url=result.get("video_url") or "",
        )
        return {"success": True, "job_id": job_id, **result}
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)[:4000]
                job.finished_at = datetime.utcnow()
                db.commit()
        _send_telegram_job_event(job_id, event="failed", error=str(exc))
        return {"success": False, "job_id": job_id, "error": str(exc)}
    finally:
        cleanup_download(file_path)
