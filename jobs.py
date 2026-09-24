from __future__ import annotations

import json
import logging
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

from config import Config
from db import SessionLocal
from downloader import cleanup_download, download_external_video, download_video
from integrations import get_secret, resolve_telegram_token
from models import (
    ChannelPublishingConfig,
    InstagramDirectShare,
    OAuthRequest,
    TelegramAdmin,
    UploadJob,
    UploadJobOption,
    UploadSchedule,
    YouTubeChannel,
)
from security import new_token
from youtube import (
    discard_preflight_video,
    publish_checked_video,
    publishing_preflight_capability,
    set_video_thumbnail,
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


def _create_reconnect_url(job_id: int) -> str:
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if not job:
            return f"{Config.PUBLIC_BASE_URL}/channels"
        channel = db.get(YouTubeChannel, job.channel_id)
        admin_id = job.telegram_user_id
        if not admin_id:
            primary = (get_secret("telegram_primary_admin_id") or "").strip()
            admin_id = int(primary) if primary.isdigit() else None
        if not admin_id:
            return f"{Config.PUBLIC_BASE_URL}/channels"

        token = new_token(32)
        db.add(OAuthRequest(
            token=token,
            telegram_user_id=int(admin_id),
            label=(channel.label if channel else "YouTube Channel")[:120],
            expires_at=datetime.utcnow() + timedelta(minutes=Config.OAUTH_LINK_MINUTES),
        ))
        db.commit()
        return f"{Config.PUBLIC_BASE_URL}/telegram/connect/{token}"


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
            f"⛔ انتشار به‌خاطر کپی‌رایت/Claim متوقف شد.\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_name}\n"
            f"🧾 Job #{job_id}\n"
            f"گزارش YouTube: {(error or 'copyright/claim')[:700]}\n\n"
            f"ویدیوی Private از YouTube حذف شد و از صف فعال انتشار خارج شد."
        )
        reply_markup = None
    elif event == "queue_recovered":
        text = (
            f"♻️ صف انتشار خودکار ترمیم شد.\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_name}\n"
            f"🧾 Job #{job_id}\n"
            f"{(error or 'ویدیوی بعدی جایگزین شد و زمان‌های صف بروزرسانی شدند.')[:700]}"
        )
        reply_markup = None
    elif event == "preflight_blocked":
        text = (
            f"⚠️ انتشار قبل از Public شدن متوقف شد.\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_name}\n"
            f"🧾 Job #{job_id}\n"
            f"گزارش YouTube: {(error or 'pre-publication check failed')[:700]}\n\n"
            f"ویدیوی Private حذف شد و از صف فعال انتشار خارج شد."
        )
        reply_markup = None
    elif event == "reauth_required":
        reconnect_url = _create_reconnect_url(job_id)
        text = (
            f"🔐 دسترسی OAuth کانال برای انتشار امن کامل نیست.\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_name}\n"
            f"🧾 Job #{job_id}\n\n"
            f"برای Flow «Private → بررسی → Public/Delete» یک‌بار کانال را با دسترسی مدیریت دوباره متصل کن. "
            f"تا قبل از آن این Job آپلود جدیدی انجام نمی‌دهد."
        )
        reply_markup = {
            "inline_keyboard": [[
                {"text": "🔐 اتصال مجدد YouTube", "url": reconnect_url}
            ]]
        }
    elif event == "ready_scheduled":
        text = (
            f"✅ ویدیوی Long آماده انتشار است.\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_name}\n"
            f"🛡 بررسی قبل از انتشار پاس شد\n"
            f"🔒 ویدیو تا زمان تعیین‌شده Private می‌ماند\n"
            f"🧾 Job #{job_id}"
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


def configure_long_job(
    job_id: int,
    *,
    quality: str = "max",
    tags: str = "",
    category_id: str = "24",
    made_for_kids: bool = False,
    embeddable: bool = True,
    license_name: str = "youtube",
    notify_subscribers: bool = True,
    default_language: str = "",
    audio_language: str = "",
    thumbnail_path: Optional[str] = None,
) -> UploadJobOption:
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if not job:
            raise RuntimeError("Upload job not found")
        option = db.get(UploadJobOption, job_id)
        if option is None:
            option = UploadJobOption(job_id=job_id)
            db.add(option)
        option.content_type = "long"
        option.quality = quality if quality in {"max", "2160", "1440", "1080", "720"} else "max"
        option.tags = (tags or "").strip()[:4000]
        option.category_id = str(category_id or "24")[:10]
        option.made_for_kids = bool(made_for_kids)
        option.embeddable = bool(embeddable)
        option.license = license_name if license_name in {"youtube", "creativeCommon"} else "youtube"
        option.notify_subscribers = bool(notify_subscribers)
        option.default_language = (default_language or "").strip()[:20]
        option.audio_language = (audio_language or "").strip()[:20]
        option.thumbnail_path = thumbnail_path
        db.commit()
        db.refresh(option)
        return option


def _job_option(job_id: int) -> Optional[UploadJobOption]:
    with SessionLocal() as db:
        return db.get(UploadJobOption, job_id)


def _long_tags(raw: str) -> list[str]:
    return [
        item.strip()
        for item in (raw or "").replace("\n", ",").split(",")
        if item.strip()
    ][:100]


def _apply_job_thumbnail(job_id: int, channel_id: int, video_id: str) -> None:
    with SessionLocal() as db:
        option = db.get(UploadJobOption, job_id)
        thumb = option.thumbnail_path if option else None
    if not thumb:
        return
    path = Path(thumb)
    if not path.is_file():
        raise RuntimeError("Thumbnail file is missing")
    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type not in {"image/jpeg", "image/png"}:
        raise RuntimeError("Thumbnail must be JPEG or PNG")
    content = path.read_bytes()
    if len(content) > 2 * 1024 * 1024:
        raise RuntimeError("Thumbnail exceeds YouTube 2MB limit")
    set_video_thumbnail(channel_id, video_id, content, mime_type)


def _cleanup_long_asset(job_id: int) -> None:
    with SessionLocal() as db:
        option = db.get(UploadJobOption, job_id)
        path_value = option.thumbnail_path if option else None
        if option:
            option.thumbnail_path = None
            db.commit()
    if path_value:
        try:
            path = Path(path_value).resolve()
            if path.is_file():
                path.unlink(missing_ok=True)
            parent = path.parent
            if parent.name == f"job-{job_id}" and parent.is_dir():
                parent.rmdir()
        except Exception:
            logger.exception("Failed to clean long-video asset for Job %s", job_id)


def recover_queue_after_block(job_id: int) -> dict:
    """Compact a channel queue when a scheduled item is blocked.

    The next waiting item inherits the blocked slot and every later item moves
    one slot forward. This keeps the publishing cadence intact without
    duplicating or re-uploading the blocked media.
    """
    with SessionLocal() as db:
        blocked_job = db.get(UploadJob, job_id)
        blocked_schedule = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
        if not blocked_job or not blocked_schedule:
            return {"recovered": False, "reason": "not_scheduled"}

        blocked_time = blocked_schedule.scheduled_for
        blocked_schedule.status = "blocked"
        blocked_schedule.released_at = datetime.utcnow()

        waiting = (
            db.query(UploadSchedule, UploadJob)
            .join(UploadJob, UploadJob.id == UploadSchedule.job_id)
            .filter(
                UploadSchedule.channel_id == blocked_job.channel_id,
                UploadSchedule.status == "waiting",
                UploadSchedule.scheduled_for > blocked_time,
                UploadJob.status.in_(["scheduled", "preparing", "ready_scheduled", "reauth_required"]),
            )
            .order_by(UploadSchedule.scheduled_for.asc(), UploadSchedule.id.asc())
            .all()
        )

        if not waiting:
            db.commit()
            return {
                "recovered": False,
                "reason": "no_next_item",
                "blocked_slot": blocked_time,
            }

        previous_time = blocked_time
        moved: list[dict] = []
        for schedule, job in waiting:
            old_time = schedule.scheduled_for
            schedule.scheduled_for = previous_time
            schedule.reason = (
                (schedule.reason or "").strip()
                + f" | Queue recovery after blocked Job #{job_id}: "
                  f"{old_time.isoformat()} -> {previous_time.isoformat()}"
            )[-4000:]
            moved.append({
                "job_id": job.id,
                "old_time": old_time,
                "new_time": previous_time,
                "title": job.title,
            })
            previous_time = old_time

        db.commit()

    first = moved[0]
    _send_telegram_job_event(
        job_id,
        event="queue_recovered",
        error=(
            f"Job #{first['job_id']} جایگزین Slot شد؛ "
            f"{len(moved)} آیتم بعدی صف یک Slot جلو آمد."
        ),
    )
    return {
        "recovered": True,
        "blocked_slot": blocked_time,
        "replacement_job_id": first["job_id"],
        "moved": moved,
    }


def _mark_preflight_blocked(job_id: int, channel_id: int, video_id: str, check: dict) -> dict:
    reason = str(check.get("reason") or "YouTube preflight failed")
    try:
        discard_preflight_video(channel_id, video_id)
    except Exception:
        logger.exception("Could not delete blocked private video %s", video_id)

    blocked_status = "copyright_blocked" if check.get("copyright_signal") else "preflight_blocked"
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if job:
            job.status = blocked_status
            job.error = reason[:4000]
            job.video_id = None
            job.video_url = None
            job.finished_at = datetime.utcnow()
        option = db.get(UploadJobOption, job_id)
        if option:
            option.checked_at = datetime.utcnow()
        share = db.query(InstagramDirectShare).filter_by(upload_job_id=job_id).one_or_none()
        if share:
            share.status = blocked_status
        db.commit()

    _cleanup_long_asset(job_id)
    _send_telegram_job_event(
        job_id,
        event="copyright_blocked" if check.get("copyright_signal") else "preflight_blocked",
        error=reason,
    )
    if check.get("copyright_signal"):
        recover_queue_after_block(job_id)
    return {
        "success": False,
        "blocked": True,
        "job_id": job_id,
        "error": reason,
        "copyright_signal": bool(check.get("copyright_signal")),
    }


def _process_long_job(job_id: int, *, hold_after_check: bool = False) -> dict:
    file_path: Optional[str] = None
    staged_video_id: Optional[str] = None
    published_ok = False
    try:
        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            option = db.get(UploadJobOption, job_id)
            if not job or not option or option.content_type != "long":
                raise RuntimeError("Long-video job configuration is missing")
            channel_id = job.channel_id
            source_url = job.source_url
            title = job.title
            description = job.description
            hashtags = job.hashtags
            privacy = job.privacy
            quality = option.quality
            tags = _long_tags(option.tags)
            category_id = option.category_id
            made_for_kids = option.made_for_kids
            embeddable = option.embeddable
            license_name = option.license
            notify_subscribers = option.notify_subscribers
            default_language = option.default_language
            audio_language = option.audio_language

        capability = publishing_preflight_capability(channel_id)
        if not capability.get("ok"):
            message = (
                "YouTube OAuth is missing management permission required for "
                "private preflight publishing. Reconnect the channel once."
            )
            with SessionLocal() as db:
                job = db.get(UploadJob, job_id)
                if job:
                    job.status = "reauth_required"
                    job.error = message
                    job.started_at = None
                    db.commit()
            _send_telegram_job_event(job_id, event="reauth_required", error=message)
            return {"success": False, "needs_reauth": True, "job_id": job_id, "error": message}

        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            job.status = "downloading"
            job.started_at = job.started_at or datetime.utcnow()
            job.error = None
            db.commit()

        file_path, source_metadata = download_external_video(source_url, quality=quality)
        if not file_path:
            raise RuntimeError("Long-form media could not be downloaded")

        with SessionLocal() as db:
            option = db.get(UploadJobOption, job_id)
            job = db.get(UploadJob, job_id)
            if option:
                option.source_metadata_json = json.dumps(source_metadata, ensure_ascii=False)[:12000]
            if job:
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
            content_type="long",
            tags=tags,
            category_id=category_id,
            made_for_kids=made_for_kids,
            embeddable=embeddable,
            license_name=license_name,
            notify_subscribers=notify_subscribers,
            default_language=default_language,
            audio_language=audio_language,
        )
        staged_video_id = result["video_id"]

        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            option = db.get(UploadJobOption, job_id)
            if job:
                job.status = "checking"
                job.video_id = staged_video_id
                job.video_url = result["video_url"]
            if option:
                option.prepared_at = datetime.utcnow()
            db.commit()

        _apply_job_thumbnail(job_id, channel_id, staged_video_id)
        _send_telegram_job_event(job_id, event="checking")
        check = wait_for_video_preflight(channel_id, staged_video_id)
        if not check.get("ok"):
            staged_video_id = None
            return _mark_preflight_blocked(job_id, channel_id, result["video_id"], check)

        with SessionLocal() as db:
            option = db.get(UploadJobOption, job_id)
            if option:
                option.checked_at = datetime.utcnow()
            db.commit()

        if hold_after_check:
            with SessionLocal() as db:
                job = db.get(UploadJob, job_id)
                if job:
                    job.status = "ready_scheduled"
                    job.error = None
                db.commit()
            _cleanup_long_asset(job_id)
            _send_telegram_job_event(job_id, event="ready_scheduled")
            return {
                "success": True,
                "prepared": True,
                "job_id": job_id,
                "video_id": staged_video_id,
                "video_url": result["video_url"],
            }

        published = publish_checked_video(
            channel_id,
            staged_video_id,
            privacy=privacy,
            content_type="long",
        )
        published_ok = True
        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            if job:
                job.status = "completed"
                job.video_id = published["video_id"]
                job.video_url = published["video_url"]
                job.error = None
                job.finished_at = datetime.utcnow()
            db.commit()
        _cleanup_long_asset(job_id)
        _send_telegram_job_event(job_id, event="completed", video_url=published["video_url"])
        return {"success": True, "job_id": job_id, **published}
    except Exception as exc:
        logger.exception("Long-video Job %s failed", job_id)
        if staged_video_id and not published_ok:
            try:
                discard_preflight_video(channel_id, staged_video_id)
            except Exception:
                logger.exception("Could not clean staged long-form video %s", staged_video_id)
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


def _prepare_scheduled_long_job(job_id: int) -> dict:
    result = _process_long_job(job_id, hold_after_check=True)
    with SessionLocal() as db:
        schedule = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
        if schedule:
            if result.get("prepared"):
                # Keep it eligible for the normal due-time release scan.
                schedule.status = "waiting"
            elif result.get("blocked"):
                schedule.status = "blocked"
                schedule.released_at = datetime.utcnow()
            elif result.get("needs_reauth"):
                schedule.status = "reauth_required"
            else:
                schedule.status = "failed"
                schedule.released_at = datetime.utcnow()
            db.commit()
    return result


def prepare_long_job_for_schedule(job_id: int):
    return _executor.submit(_prepare_scheduled_long_job, job_id)


def finalize_prepared_long_job(job_id: int) -> dict:
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        option = db.get(UploadJobOption, job_id)
        if not job or not option or option.content_type != "long":
            raise RuntimeError("Long-video job not found")
        channel_id = job.channel_id
        video_id = job.video_id
        privacy = job.privacy

    if not video_id:
        return _process_long_job(job_id, hold_after_check=False)

    capability = publishing_preflight_capability(channel_id)
    if not capability.get("ok"):
        message = "YouTube management permission is required before scheduled publication."
        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            if job:
                job.status = "reauth_required"
                job.error = message
                db.commit()
        _send_telegram_job_event(job_id, event="reauth_required", error=message)
        return {"success": False, "needs_reauth": True, "job_id": job_id, "error": message}

    check = wait_for_video_preflight(channel_id, video_id, timeout_seconds=90, poll_seconds=5)
    if not check.get("ok"):
        return _mark_preflight_blocked(job_id, channel_id, video_id, check)

    published = publish_checked_video(
        channel_id,
        video_id,
        privacy=privacy,
        content_type="long",
    )
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if job:
            job.status = "completed"
            job.video_id = published["video_id"]
            job.video_url = published["video_url"]
            job.error = None
            job.finished_at = datetime.utcnow()
        db.commit()
    _send_telegram_job_event(job_id, event="completed", video_url=published["video_url"])
    return {"success": True, "job_id": job_id, **published}


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


def _sync_reauth_schedule_result(job_id: int, result: dict) -> dict:
    with SessionLocal() as db:
        schedule = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
        if not schedule:
            return result
        if result.get("prepared"):
            schedule.status = "waiting"
            schedule.released_at = None
        elif result.get("success"):
            schedule.status = "released"
            schedule.released_at = datetime.utcnow()
        elif result.get("blocked"):
            schedule.status = "blocked"
            schedule.released_at = datetime.utcnow()
        elif result.get("needs_reauth"):
            schedule.status = "reauth_required"
        else:
            schedule.status = "failed"
            schedule.released_at = datetime.utcnow()
        db.commit()
    return result


def resume_reauth_job(job_id: int) -> dict:
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if not job:
            return {"success": False, "job_id": job_id, "error": "Upload job not found"}
        option = db.get(UploadJobOption, job_id)
        schedule = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
        channel_id = job.channel_id
        video_id = job.video_id
        privacy = job.privacy
        is_long = bool(option and option.content_type == "long")
        schedule_is_future = bool(
            schedule
            and schedule.scheduled_for > datetime.utcnow()
            and schedule.status in {"waiting", "reauth_required"}
        )

    capability = publishing_preflight_capability(channel_id)
    if not capability.get("ok"):
        return {
            "success": False,
            "needs_reauth": True,
            "job_id": job_id,
            "error": "YouTube management permission is still missing.",
        }

    if is_long:
        if not video_id:
            if schedule_is_future:
                with SessionLocal() as db:
                    row = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
                    if row and row.status == "reauth_required":
                        row.status = "waiting"
                        db.commit()
                return _sync_reauth_schedule_result(
                    job_id,
                    _process_long_job(job_id, hold_after_check=True),
                )
            return _sync_reauth_schedule_result(
                job_id,
                _process_long_job(job_id, hold_after_check=False),
            )

        if schedule_is_future:
            try:
                with SessionLocal() as db:
                    job = db.get(UploadJob, job_id)
                    row = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
                    if job:
                        job.status = "checking"
                        job.error = None
                    if row and row.status == "reauth_required":
                        row.status = "waiting"
                    db.commit()

                _send_telegram_job_event(job_id, event="checking")
                check = wait_for_video_preflight(channel_id, video_id)
                if not check.get("ok"):
                    return _mark_preflight_blocked(job_id, channel_id, video_id, check)

                with SessionLocal() as db:
                    job = db.get(UploadJob, job_id)
                    option = db.get(UploadJobOption, job_id)
                    if job:
                        job.status = "ready_scheduled"
                        job.error = None
                    if option:
                        option.checked_at = datetime.utcnow()
                    db.commit()
                _cleanup_long_asset(job_id)
                _send_telegram_job_event(job_id, event="ready_scheduled")
                return _sync_reauth_schedule_result(job_id, {
                    "success": True,
                    "prepared": True,
                    "job_id": job_id,
                    "video_id": video_id,
                    "video_url": f"https://youtube.com/watch?v={video_id}",
                })
            except Exception as exc:
                logger.exception("Could not resume scheduled long Job %s", job_id)
                with SessionLocal() as db:
                    job = db.get(UploadJob, job_id)
                    if job:
                        job.status = "reauth_required"
                        job.error = str(exc)[:4000]
                        db.commit()
                return {"success": False, "job_id": job_id, "error": str(exc)}

        return _sync_reauth_schedule_result(
            job_id,
            finalize_prepared_long_job(job_id),
        )

    if not video_id:
        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            if job:
                job.status = "queued"
                job.error = None
                db.commit()
        return process_job(job_id)

    try:
        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            if job:
                job.status = "checking"
                job.error = None
                db.commit()

        _send_telegram_job_event(job_id, event="checking")
        check = wait_for_video_preflight(channel_id, video_id)
        if not check.get("ok"):
            reason = str(check.get("reason") or "YouTube preflight failed")
            try:
                discard_preflight_video(channel_id, video_id)
            except Exception:
                logger.exception("Could not delete blocked private video %s", video_id)

            blocked_status = "copyright_blocked" if check.get("copyright_signal") else "preflight_blocked"
            with SessionLocal() as db:
                job = db.get(UploadJob, job_id)
                if job:
                    job.status = blocked_status
                    job.error = reason[:4000]
                    job.video_id = None
                    job.video_url = None
                    job.finished_at = datetime.utcnow()
                share = db.query(InstagramDirectShare).filter_by(upload_job_id=job_id).one_or_none()
                if share:
                    share.status = blocked_status
                db.commit()

            _send_telegram_job_event(
                job_id,
                event="copyright_blocked" if check.get("copyright_signal") else "preflight_blocked",
                error=reason,
            )
            if check.get("copyright_signal"):
                recover_queue_after_block(job_id)
            return {
                "success": False,
                "blocked": True,
                "job_id": job_id,
                "error": reason,
                "copyright_signal": bool(check.get("copyright_signal")),
            }

        published = publish_checked_video(channel_id, video_id, privacy=privacy)
        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            if job:
                job.status = "completed"
                job.video_id = published["video_id"]
                job.video_url = published["video_url"]
                job.error = None
                job.finished_at = datetime.utcnow()
            share = db.query(InstagramDirectShare).filter_by(upload_job_id=job_id).one_or_none()
            if share:
                share.status = "completed"
            db.commit()

        _send_telegram_job_event(
            job_id,
            event="completed",
            video_url=published.get("video_url") or "",
        )
        return {"success": True, "job_id": job_id, **published}
    except Exception as exc:
        logger.exception("Could not resume reauthorized Job %s", job_id)
        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            if job:
                job.status = "reauth_required"
                job.error = str(exc)[:4000]
                db.commit()
        return {"success": False, "job_id": job_id, "error": str(exc)}


def resume_reauth_jobs_for_channel(channel_id: int) -> list[int]:
    with SessionLocal() as db:
        job_ids = [
            row.id
            for row in db.query(UploadJob).filter(
                UploadJob.channel_id == channel_id,
                UploadJob.status == "reauth_required",
            ).order_by(UploadJob.id.asc()).all()
        ]
    for job_id in job_ids:
        _executor.submit(resume_reauth_job, job_id)
    return job_ids


def process_job(job_id: int) -> dict:
    option = _job_option(job_id)
    if option and option.content_type == "long":
        return _process_long_job(job_id, hold_after_check=False)

    file_path: Optional[str] = None
    staged_video_id: Optional[str] = None
    published_ok = False
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

        capability = publishing_preflight_capability(channel_id)
        if not capability.get("ok"):
            message = (
                "YouTube OAuth is missing management permission required for "
                "private preflight publishing. Reconnect the channel once."
            )
            with SessionLocal() as db:
                job = db.get(UploadJob, job_id)
                if job:
                    job.status = "reauth_required"
                    job.error = message
                    job.started_at = None
                    db.commit()
            _send_telegram_job_event(job_id, event="reauth_required", error=message)
            return {
                "success": False,
                "needs_reauth": True,
                "job_id": job_id,
                "error": message,
            }

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
        staged_video_id = video_id

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

            blocked_status = "copyright_blocked" if check.get("copyright_signal") else "preflight_blocked"
            with SessionLocal() as db:
                job = db.get(UploadJob, job_id)
                job.status = blocked_status
                job.error = reason[:4000]
                job.video_id = None
                job.video_url = None
                job.finished_at = datetime.utcnow()
                share = db.query(InstagramDirectShare).filter_by(upload_job_id=job_id).one_or_none()
                if share:
                    share.status = blocked_status
                db.commit()

            staged_video_id = None
            _send_telegram_job_event(
                job_id,
                event="copyright_blocked" if check.get("copyright_signal") else "preflight_blocked",
                error=reason,
            )
            if check.get("copyright_signal"):
                recover_queue_after_block(job_id)
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
        published_ok = True

        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            job.status = "completed"
            job.video_id = result["video_id"]
            job.video_url = result["video_url"]
            job.finished_at = datetime.utcnow()
            share = db.query(InstagramDirectShare).filter_by(upload_job_id=job_id).one_or_none()
            if share:
                share.status = "completed"
            db.commit()
        _send_telegram_job_event(
            job_id,
            event="completed",
            video_url=result.get("video_url") or "",
        )
        return {"success": True, "job_id": job_id, **result}
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        if staged_video_id and not published_ok:
            try:
                discard_preflight_video(channel_id, staged_video_id)
            except Exception:
                logger.exception("Could not clean up staged private video %s", staged_video_id)
        with SessionLocal() as db:
            job = db.get(UploadJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)[:4000]
                job.finished_at = datetime.utcnow()
                share = db.query(InstagramDirectShare).filter_by(upload_job_id=job_id).one_or_none()
                if share:
                    share.status = "failed"
                db.commit()
        _send_telegram_job_event(job_id, event="failed", error=str(exc))
        return {"success": False, "job_id": job_id, "error": str(exc)}
    finally:
        cleanup_download(file_path)
