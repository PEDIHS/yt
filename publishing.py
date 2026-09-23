from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from analytics import get_or_sync_channel_analytics
from config import Config
from db import SessionLocal, init_db
from jobs import enqueue_job, process_job
from models import ChannelPublishingConfig, UploadJob, UploadSchedule, YouTubeChannel
from youtube import list_channel_videos

logger = logging.getLogger("publishing")
_scheduler_executor = ThreadPoolExecutor(max_workers=max(1, Config.MAX_WORKERS), thread_name_prefix="smart-publisher")

DEFAULT_PEAK_HOURS = [12, 15, 18, 21, 23]


def _utcnow() -> datetime:
    return datetime.utcnow()


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _loads(raw: str, default):
    try:
        value = json.loads(raw or "")
        return value
    except Exception:
        return default


def get_or_create_publishing_config(channel_id: int, *, commit: bool = True) -> ChannelPublishingConfig:
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")
        cfg = db.get(ChannelPublishingConfig, channel_id)
        if cfg is None:
            cfg = ChannelPublishingConfig(channel_id=channel_id)
            db.add(cfg)
            if commit:
                db.commit()
                db.refresh(cfg)
            else:
                db.flush()
        return cfg


def publishing_config_payload(channel_id: int) -> dict:
    cfg = get_or_create_publishing_config(channel_id)
    analysis = _loads(cfg.peak_analysis_json, {})
    peak_slots = _loads(cfg.peak_slots_json, [])
    manual_slots = _loads(cfg.manual_slots_json, [])
    return {
        "channel_id": cfg.channel_id,
        "enabled": bool(cfg.enabled),
        "smart_peak_enabled": bool(cfg.smart_peak_enabled),
        "videos_per_day": int(cfg.videos_per_day or 1),
        "timezone": cfg.timezone,
        "minimum_gap_minutes": int(cfg.minimum_gap_minutes or 0),
        "allowed_start_hour": int(cfg.allowed_start_hour or 0),
        "allowed_end_hour": int(cfg.allowed_end_hour or 23),
        "manual_slots": manual_slots,
        "peak_slots": peak_slots,
        "analysis": analysis,
        "last_analyzed_at": cfg.last_analyzed_at.isoformat() if cfg.last_analyzed_at else None,
    }


def update_publishing_config(
    channel_id: int,
    *,
    enabled: bool,
    smart_peak_enabled: bool,
    videos_per_day: int,
    timezone_name: str,
    minimum_gap_minutes: int,
    allowed_start_hour: int,
    allowed_end_hour: int,
    manual_slots: list[int] | None = None,
) -> dict:
    videos_per_day = max(1, min(12, int(videos_per_day or 1)))
    minimum_gap_minutes = max(30, min(720, int(minimum_gap_minutes or 180)))
    allowed_start_hour = max(0, min(23, int(allowed_start_hour)))
    allowed_end_hour = max(0, min(23, int(allowed_end_hour)))
    if allowed_end_hour <= allowed_start_hour:
        raise ValueError("End hour must be later than start hour")
    _zone(timezone_name)
    normalized_manual = sorted({
        max(0, min(23, int(hour)))
        for hour in (manual_slots or [])
        if allowed_start_hour <= int(hour) <= allowed_end_hour
    })

    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")
        cfg = db.get(ChannelPublishingConfig, channel_id)
        if cfg is None:
            cfg = ChannelPublishingConfig(channel_id=channel_id)
            db.add(cfg)
        cfg.enabled = bool(enabled)
        cfg.smart_peak_enabled = bool(smart_peak_enabled)
        cfg.videos_per_day = videos_per_day
        cfg.timezone = timezone_name
        cfg.minimum_gap_minutes = minimum_gap_minutes
        cfg.allowed_start_hour = allowed_start_hour
        cfg.allowed_end_hour = allowed_end_hour
        cfg.manual_slots_json = json.dumps(normalized_manual)
        cfg.updated_at = _utcnow()
        db.commit()
    return publishing_config_payload(channel_id)


def _parse_published_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except Exception:
        return None


def analyze_peak_slots(channel_id: int, days: int = 90) -> dict:
    cfg = get_or_create_publishing_config(channel_id)
    tz = _zone(cfg.timezone)

    analytics, analytics_error = get_or_sync_channel_analytics(
        channel_id,
        days,
        max_age_minutes=360,
    )
    video_page = list_channel_videos(channel_id, max_results=50)
    videos = video_page.get("items", [])

    period_by_video = {}
    if analytics:
        for row in analytics.get("top_videos", []) or []:
            period_by_video[str(row.get("video_id") or "")] = row

    hour_buckets: dict[int, list[float]] = {}
    weekday_buckets: dict[int, list[float]] = {}
    now = datetime.now(timezone.utc)

    for item in videos:
        published = _parse_published_at(item.get("published_at"))
        if not published:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        local = published.astimezone(tz)
        age_days = max(1.0, (now - published.astimezone(timezone.utc)).total_seconds() / 86400)
        views = max(0, int(item.get("views") or 0))
        likes = max(0, int(item.get("likes") or 0))
        comments = max(0, int(item.get("comments") or 0))
        period = period_by_video.get(str(item.get("video_id") or ""), {})
        period_views = max(0, int(period.get("period_views") or 0))
        period_watch = max(0, int(period.get("period_watch_minutes") or 0))

        recent_velocity = period_views / max(1.0, min(float(days), age_days))
        lifetime_velocity = views / age_days
        engagement = ((likes + comments * 2) / max(1, views)) * 1000
        watch_signal = math.log1p(period_watch) if period_watch else 0.0
        score = (
            math.log1p(recent_velocity) * 7.0
            + math.log1p(lifetime_velocity) * 4.0
            + min(20.0, engagement)
            + watch_signal
        )
        recency_weight = max(0.45, min(1.0, 120.0 / max(30.0, age_days)))
        score *= recency_weight

        hour_buckets.setdefault(local.hour, []).append(score)
        weekday_buckets.setdefault(local.weekday(), []).append(score)

    def aggregate(bucket: dict[int, list[float]]) -> list[dict]:
        rows = []
        for key, values in bucket.items():
            rows.append({
                "key": int(key),
                "samples": len(values),
                "score": round(sum(values) / max(1, len(values)), 2),
            })
        rows.sort(key=lambda row: (row["score"], row["samples"]), reverse=True)
        return rows

    hour_rank = aggregate(hour_buckets)
    weekday_rank = aggregate(weekday_buckets)
    allowed_hours = [
        row for row in hour_rank
        if cfg.allowed_start_hour <= row["key"] <= cfg.allowed_end_hour
    ]
    chosen = allowed_hours[:max(3, min(6, cfg.videos_per_day + 2))]

    if len(chosen) < 3:
        fallback = [
            hour for hour in DEFAULT_PEAK_HOURS
            if cfg.allowed_start_hour <= hour <= cfg.allowed_end_hour
        ]
        for hour in fallback:
            if any(row["key"] == hour for row in chosen):
                continue
            chosen.append({"key": hour, "samples": 0, "score": 0.0})
            if len(chosen) >= max(3, min(6, cfg.videos_per_day + 2)):
                break

    peak_slots = [
        {
            "hour": row["key"],
            "score": row["score"],
            "samples": row["samples"],
        }
        for row in chosen
    ]

    audience = (analytics or {}).get("audience", {}) or {}
    top_country = ((audience.get("countries") or [{}])[0]).get("country") or ""
    top_device = ((audience.get("devices") or [{}])[0]).get("device") or ""
    confidence = "low"
    if len(videos) >= 25:
        confidence = "high"
    elif len(videos) >= 10:
        confidence = "medium"

    analysis = {
        "algorithm": "historical-publish-performance-v1",
        "days": days,
        "video_samples": len(videos),
        "confidence": confidence,
        "timezone": cfg.timezone,
        "top_country": top_country,
        "top_device": top_device,
        "analytics_error": analytics_error or "",
        "best_hours": peak_slots,
        "best_weekdays": [
            {"weekday": row["key"], "score": row["score"], "samples": row["samples"]}
            for row in weekday_rank[:4]
        ],
        "note": (
            "YouTube's public Analytics API does not expose the Studio viewer-online hourly heatmap. "
            "Slots are inferred from this channel's historical publish times and performance."
        ),
        "generated_at": _utcnow().isoformat(),
    }

    with SessionLocal() as db:
        cfg_db = db.get(ChannelPublishingConfig, channel_id)
        if cfg_db is None:
            cfg_db = ChannelPublishingConfig(channel_id=channel_id)
            db.add(cfg_db)
        cfg_db.peak_slots_json = json.dumps(peak_slots, ensure_ascii=False)
        cfg_db.peak_analysis_json = json.dumps(analysis, ensure_ascii=False)
        cfg_db.last_analyzed_at = _utcnow()
        db.commit()
    return analysis


def _slot_hours(cfg: ChannelPublishingConfig) -> list[dict]:
    manual = _loads(cfg.manual_slots_json, [])
    if not cfg.smart_peak_enabled and manual:
        return [{"hour": int(hour), "score": 0.0, "samples": 0} for hour in manual]

    peaks = _loads(cfg.peak_slots_json, [])
    if peaks:
        return [
            {
                "hour": int(row.get("hour", 0)),
                "score": float(row.get("score", 0) or 0),
                "samples": int(row.get("samples", 0) or 0),
            }
            for row in peaks
            if cfg.allowed_start_hour <= int(row.get("hour", -1)) <= cfg.allowed_end_hour
        ]

    fallback = [
        hour for hour in DEFAULT_PEAK_HOURS
        if cfg.allowed_start_hour <= hour <= cfg.allowed_end_hour
    ]
    return [{"hour": hour, "score": 0.0, "samples": 0} for hour in fallback]


def _existing_local_slots(db, channel_id: int, tz: ZoneInfo, local_date) -> list[datetime]:
    schedules = db.query(UploadSchedule).filter(
        UploadSchedule.channel_id == channel_id,
        UploadSchedule.status.in_(["waiting", "releasing"]),
    ).all()
    values = []
    for row in schedules:
        aware = row.scheduled_for.replace(tzinfo=timezone.utc).astimezone(tz)
        if aware.date() == local_date:
            values.append(aware)
    return values


def next_smart_slot(channel_id: int, *, after_utc: datetime | None = None) -> tuple[datetime, float, str]:
    after_utc = after_utc or _utcnow()
    if after_utc.tzinfo is None:
        after_aware = after_utc.replace(tzinfo=timezone.utc)
    else:
        after_aware = after_utc.astimezone(timezone.utc)

    with SessionLocal() as db:
        cfg = db.get(ChannelPublishingConfig, channel_id)
        if cfg is None:
            cfg = ChannelPublishingConfig(channel_id=channel_id)
            db.add(cfg)
            db.commit()
            db.refresh(cfg)

        tz = _zone(cfg.timezone)
        slot_rows = _slot_hours(cfg)
        if not slot_rows:
            slot_rows = [{"hour": 18, "score": 0.0, "samples": 0}]
        best_for_day = sorted(slot_rows, key=lambda row: row["score"], reverse=True)[:max(1, cfg.videos_per_day)]
        best_for_day = sorted(best_for_day, key=lambda row: row["hour"])

        local_now = after_aware.astimezone(tz)
        for offset in range(0, 21):
            date_local = local_now.date() + timedelta(days=offset)
            existing = _existing_local_slots(db, channel_id, tz, date_local)
            if len(existing) >= cfg.videos_per_day:
                continue

            for row in best_for_day:
                candidate_local = datetime(
                    date_local.year,
                    date_local.month,
                    date_local.day,
                    int(row["hour"]),
                    0,
                    tzinfo=tz,
                )
                if candidate_local <= local_now + timedelta(minutes=5):
                    continue
                if any(
                    abs((candidate_local - other).total_seconds()) < cfg.minimum_gap_minutes * 60
                    for other in existing
                ):
                    continue
                candidate_utc = candidate_local.astimezone(timezone.utc).replace(tzinfo=None)
                reason = (
                    f"Smart slot {candidate_local.strftime('%Y-%m-%d %H:%M')} {cfg.timezone}; "
                    f"historical score={float(row['score']):.2f}; samples={int(row['samples'])}"
                )
                return candidate_utc, float(row["score"]), reason

    raise RuntimeError("No publishing slot is available in the next 21 days")


def schedule_job_smart(job_id: int) -> UploadSchedule:
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if not job:
            raise RuntimeError("Upload job not found")
        channel_id = job.channel_id

    scheduled_for, score, reason = next_smart_slot(channel_id)
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        schedule = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
        if schedule is None:
            schedule = UploadSchedule(job_id=job_id, channel_id=channel_id, scheduled_for=scheduled_for)
            db.add(schedule)
        schedule.scheduled_for = scheduled_for
        schedule.schedule_mode = "smart"
        schedule.status = "waiting"
        schedule.score = score
        schedule.reason = reason
        schedule.released_at = None
        job.status = "scheduled"
        db.commit()
        db.refresh(schedule)
        return schedule


def local_datetime_to_utc(channel_id: int, value: str) -> datetime:
    value = (value or "").strip()
    if not value:
        raise ValueError("Scheduled date/time is required")
    try:
        local_naive = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid scheduled date/time") from exc
    cfg = get_or_create_publishing_config(channel_id)
    tz = _zone(cfg.timezone)
    if local_naive.tzinfo is None:
        local_aware = local_naive.replace(tzinfo=tz)
    else:
        local_aware = local_naive.astimezone(tz)
    return local_aware.astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_channel_local(channel_id: int, value: datetime) -> datetime:
    cfg = get_or_create_publishing_config(channel_id)
    tz = _zone(cfg.timezone)
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware.astimezone(tz)


def schedule_job_manual(job_id: int, scheduled_for_utc: datetime) -> UploadSchedule:
    if scheduled_for_utc <= _utcnow() + timedelta(minutes=2):
        raise ValueError("Scheduled time must be at least 2 minutes in the future")
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if not job:
            raise RuntimeError("Upload job not found")
        schedule = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
        if schedule is None:
            schedule = UploadSchedule(job_id=job_id, channel_id=job.channel_id, scheduled_for=scheduled_for_utc)
            db.add(schedule)
        schedule.scheduled_for = scheduled_for_utc
        schedule.schedule_mode = "manual"
        schedule.status = "waiting"
        schedule.score = 0.0
        schedule.reason = "Manual schedule"
        schedule.released_at = None
        job.status = "scheduled"
        db.commit()
        db.refresh(schedule)
        return schedule


def cancel_scheduled_job(job_id: int) -> None:
    with SessionLocal() as db:
        schedule = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
        job = db.get(UploadJob, job_id)
        if not schedule or schedule.status not in {"waiting", "releasing"}:
            raise RuntimeError("Scheduled job is not waiting")
        schedule.status = "cancelled"
        if job:
            job.status = "cancelled"
            job.finished_at = _utcnow()
        db.commit()


def release_job_now(job_id: int):
    with SessionLocal() as db:
        schedule = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
        job = db.get(UploadJob, job_id)
        if not job:
            raise RuntimeError("Upload job not found")
        if schedule and schedule.status in {"waiting", "releasing"}:
            schedule.status = "released"
            schedule.released_at = _utcnow()
        job.status = "queued"
        job.error = None
        db.commit()
    return enqueue_job(job_id)


def queue_snapshot(channel_id: int | None = None, limit: int = 100) -> list[dict]:
    with SessionLocal() as db:
        query = db.query(UploadSchedule, UploadJob, YouTubeChannel).join(
            UploadJob, UploadJob.id == UploadSchedule.job_id
        ).join(YouTubeChannel, YouTubeChannel.id == UploadSchedule.channel_id)
        if channel_id:
            query = query.filter(UploadSchedule.channel_id == channel_id)
        rows = query.filter(
            UploadSchedule.status.in_(["waiting", "releasing"])
        ).order_by(UploadSchedule.scheduled_for.asc()).limit(limit).all()
        return [
            {
                "schedule_id": schedule.id,
                "job_id": job.id,
                "channel_id": channel.id,
                "channel_label": channel.label,
                "channel_title": channel.title,
                "title": job.title,
                "source_url": job.source_url,
                "privacy": job.privacy,
                "scheduled_for": schedule.scheduled_for,
                "schedule_mode": schedule.schedule_mode,
                "status": schedule.status,
                "score": schedule.score,
                "reason": schedule.reason,
                "source": job.source,
            }
            for schedule, job, channel in rows
        ]


def reschedule_channel_queue(channel_id: int) -> int:
    with SessionLocal() as db:
        waiting = db.query(UploadSchedule).filter(
            UploadSchedule.channel_id == channel_id,
            UploadSchedule.status == "waiting",
            UploadSchedule.schedule_mode == "smart",
        ).order_by(UploadSchedule.created_at.asc()).all()
        job_ids = [row.job_id for row in waiting]
        for row in waiting:
            db.delete(row)
        for job_id in job_ids:
            job = db.get(UploadJob, job_id)
            if job:
                job.status = "queued"
        db.commit()

    count = 0
    for job_id in job_ids:
        schedule_job_smart(job_id)
        count += 1
    return count


def _mark_due_for_release(limit: int = 8) -> list[int]:
    now = _utcnow()
    with SessionLocal() as db:
        stale = db.query(UploadSchedule).filter(
            UploadSchedule.status == "releasing",
            UploadSchedule.scheduled_for < now - timedelta(minutes=15),
        ).all()
        for row in stale:
            row.status = "waiting"

        due = db.query(UploadSchedule).filter(
            UploadSchedule.status == "waiting",
            UploadSchedule.scheduled_for <= now,
        ).order_by(UploadSchedule.scheduled_for.asc()).limit(limit).all()
        ids = []
        for row in due:
            row.status = "releasing"
            ids.append(row.job_id)
        db.commit()
        return ids


def _process_scheduled_job(job_id: int) -> None:
    result = process_job(job_id)
    with SessionLocal() as db:
        row = db.query(UploadSchedule).filter_by(job_id=job_id).one_or_none()
        if row:
            row.status = "released" if result.get("success") else "failed"
            row.released_at = _utcnow()
            db.commit()


def run_scheduler() -> None:
    init_db()
    logger.info("Smart publishing scheduler started")
    while True:
        try:
            due_ids = _mark_due_for_release()
            for job_id in due_ids:
                _scheduler_executor.submit(_process_scheduled_job, job_id)
        except Exception:
            logger.exception("Smart publishing scheduler loop failed")
        time.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    run_scheduler()
