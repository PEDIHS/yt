from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from config import Config
from db import SessionLocal
from downloader import cleanup_download, download_video
from models import UploadJob, YouTubeChannel
from youtube import upload_to_youtube

logger = logging.getLogger("jobs")
_executor = ThreadPoolExecutor(max_workers=Config.MAX_WORKERS, thread_name_prefix="uploads")


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

        result = upload_to_youtube(
            file_path=file_path,
            channel_id=channel_id,
            title=title,
            hashtags=hashtags,
            description=description,
            privacy=privacy,
        )

        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            job.status = "completed"
            job.video_id = result["video_id"]
            job.video_url = result["video_url"]
            job.finished_at = datetime.utcnow()
            db.commit()
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
        return {"success": False, "job_id": job_id, "error": str(exc)}
    finally:
        cleanup_download(file_path)
