from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from analytics import get_or_sync_channel_analytics
from config import Config
from db import SessionLocal
from integrations import get_secret, resolve_telegram_token
from models import ChannelMissionState, ChannelPublishingConfig, TelegramAdmin, YouTubeChannel

logger = logging.getLogger("missions")

FULL_YPP_CHANGE_DATE = date(2027, 2, 1)
EARLY_SUBSCRIBERS = 500
EARLY_UPLOADS_90 = 3
EARLY_SHORTS_90 = 3_000_000
EARLY_WATCH_HOURS_365 = 3_000
FULL_SUBSCRIBERS = 1_000


def _clamp_progress(current: float, target: float) -> float:
    if target <= 0:
        return 1.0
    return max(0.0, min(1.0, float(current or 0) / float(target)))


def _full_thresholds(on_date: date | None = None) -> dict:
    on_date = on_date or date.today()
    if on_date >= FULL_YPP_CHANGE_DATE:
        return {
            "subscribers": FULL_SUBSCRIBERS,
            "shorts_90": 20_000_000,
            "watch_hours_365": 8_000,
            "policy": "2027-02-01+",
            "effective_from": FULL_YPP_CHANGE_DATE.isoformat(),
        }
    return {
        "subscribers": FULL_SUBSCRIBERS,
        "shorts_90": 10_000_000,
        "watch_hours_365": 4_000,
        "policy": "current",
        "effective_until": "2027-01-31",
    }


def _content_rows(payload: dict | None) -> list[dict]:
    return ((payload or {}).get("audience", {}) or {}).get("content_types", []) or []


def _shorts_engaged_views(payload: dict | None) -> int:
    total = 0
    for row in _content_rows(payload):
        content_type = str(row.get("type") or "").upper()
        if "SHORT" in content_type:
            total += int(row.get("engaged_views") or 0)
    return total


def _longform_watch_hours(payload: dict | None) -> float:
    minutes = 0
    for row in _content_rows(payload):
        content_type = str(row.get("type") or "").upper()
        if "SHORT" in content_type or "POST" in content_type:
            continue
        minutes += int(row.get("watch_minutes") or 0)
    return round(minutes / 60.0, 1)


def _public_uploads_90(payload: dict | None) -> int:
    start_raw = (payload or {}).get("start_date") or ""
    try:
        start_date = date.fromisoformat(start_raw)
    except ValueError:
        start_date = date.today()
    count = 0
    for item in (payload or {}).get("latest_videos", []) or []:
        if str(item.get("privacy") or "").lower() != "public":
            continue
        published = str(item.get("published_at") or "")
        try:
            published_date = datetime.fromisoformat(published.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if published_date >= start_date:
            count += 1
    return min(count, 12)


def _state_for_channel(channel_id: int) -> ChannelMissionState:
    with SessionLocal() as db:
        state = db.get(ChannelMissionState, channel_id)
        if state is None:
            state = ChannelMissionState(channel_id=channel_id)
            db.add(state)
            db.commit()
            db.refresh(state)
        return state


def set_mission_settings(channel_id: int, *, enabled: bool, daily_report: bool) -> None:
    with SessionLocal() as db:
        state = db.get(ChannelMissionState, channel_id)
        if state is None:
            state = ChannelMissionState(channel_id=channel_id)
            db.add(state)
        state.enabled = bool(enabled)
        state.daily_report = bool(daily_report)
        db.commit()


def build_channel_mission(
    channel_id: int,
    *,
    refresh: bool = False,
    reference_date: date | None = None,
) -> dict:
    state = _state_for_channel(channel_id)
    max_age = 0 if refresh else 360
    data90, error90 = get_or_sync_channel_analytics(channel_id, 90, max_age_minutes=max_age)
    data365, error365 = get_or_sync_channel_analytics(channel_id, 365, max_age_minutes=max_age)

    # Older cache snapshots predate engagedViews support. Refresh once so the
    # Shorts YPP mission never falls back to the newer raw public-view metric.
    rows90 = _content_rows(data90)
    if data90 and rows90 and not any("engaged_views" in row for row in rows90):
        data90, error90 = get_or_sync_channel_analytics(channel_id, 90, max_age_minutes=0)

    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")
        subscribers = int(
            ((data90 or {}).get("current", {}) or {}).get("subscriber_count")
            or channel.subscriber_count
            or 0
        )
        channel_title = channel.title
        channel_label = channel.label
        thumbnail_url = channel.thumbnail_url or ""
        is_active = bool(channel.is_active)
        cfg = db.get(ChannelPublishingConfig, channel_id)
        timezone_name = cfg.timezone if cfg else "Asia/Tehran"

    shorts90 = _shorts_engaged_views(data90)
    long_hours365 = _longform_watch_hours(data365)
    uploads90 = _public_uploads_90(data90)
    thresholds = _full_thresholds(reference_date)

    full_sub = _clamp_progress(subscribers, thresholds["subscribers"])
    full_shorts = _clamp_progress(shorts90, thresholds["shorts_90"])
    full_watch = _clamp_progress(long_hours365, thresholds["watch_hours_365"])
    full_path = max(full_shorts, full_watch)
    full_progress = min(full_sub, full_path)
    best_path = "shorts" if full_shorts >= full_watch else "watch_hours"

    early_sub = _clamp_progress(subscribers, EARLY_SUBSCRIBERS)
    early_uploads = _clamp_progress(uploads90, EARLY_UPLOADS_90)
    early_shorts = _clamp_progress(shorts90, EARLY_SHORTS_90)
    early_watch = _clamp_progress(long_hours365, EARLY_WATCH_HOURS_365)
    early_progress = min(early_sub, early_uploads, max(early_shorts, early_watch))

    upcoming = None
    on_date = reference_date or date.today()
    if on_date < FULL_YPP_CHANGE_DATE:
        upcoming = {
            "effective_from": FULL_YPP_CHANGE_DATE.isoformat(),
            "subscribers": FULL_SUBSCRIBERS,
            "shorts_90": 20_000_000,
            "watch_hours_365": 8_000,
        }

    return {
        "channel_id": channel_id,
        "channel_title": channel_title,
        "channel_label": channel_label,
        "thumbnail_url": thumbnail_url,
        "is_active": is_active,
        "timezone": timezone_name,
        "enabled": bool(state.enabled),
        "daily_report": bool(state.daily_report),
        "last_report_date": state.last_report_date,
        "metrics": {
            "subscribers": subscribers,
            "shorts_engaged_views_90": shorts90,
            "longform_watch_hours_365": long_hours365,
            "public_uploads_90": uploads90,
        },
        "early": {
            "progress": round(early_progress * 100, 1),
            "remaining": round((1 - early_progress) * 100, 1),
            "subscriber_progress": round(early_sub * 100, 1),
            "upload_progress": round(early_uploads * 100, 1),
            "shorts_progress": round(early_shorts * 100, 1),
            "watch_progress": round(early_watch * 100, 1),
            "targets": {
                "subscribers": EARLY_SUBSCRIBERS,
                "uploads_90": EARLY_UPLOADS_90,
                "shorts_90": EARLY_SHORTS_90,
                "watch_hours_365": EARLY_WATCH_HOURS_365,
            },
        },
        "full": {
            "progress": round(full_progress * 100, 1),
            "remaining": round((1 - full_progress) * 100, 1),
            "subscriber_progress": round(full_sub * 100, 1),
            "shorts_progress": round(full_shorts * 100, 1),
            "watch_progress": round(full_watch * 100, 1),
            "best_path": best_path,
            "targets": thresholds,
        },
        "upcoming": upcoming,
        "analytics_error": error90 or error365 or "",
        "approximate": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _compact(value: int | float) -> str:
    value = float(value or 0)
    for amount, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= amount:
            return f"{value / amount:.1f}{suffix}".replace(".0", "")
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}"


def mission_report_text(mission: dict) -> str:
    metrics = mission["metrics"]
    full = mission["full"]
    targets = full["targets"]
    best = "Shorts" if full["best_path"] == "shorts" else "Long-form Watch Hours"
    remaining_pct = max(0.0, float(full["remaining"]))

    lines = [
        f"🎯 Mission درآمدزایی · {mission['channel_label']}",
        "",
        f"💰 پیشرفت Full YPP: {full['progress']:.1f}%",
        f"⏳ {remaining_pct:.1f}% تا رسیدن به معیار عددی درآمدزایی باقی مانده",
        f"🧭 مسیر جلوتر: {best}",
        "",
        f"👥 Subscribers: {_compact(metrics['subscribers'])} / {_compact(targets['subscribers'])}",
        f"⚡ Qualified/Engaged Shorts (90d): ≈ {_compact(metrics['shorts_engaged_views_90'])} / {_compact(targets['shorts_90'])}",
        f"🕒 Long-form Watch Hours (365d): ≈ {_compact(metrics['longform_watch_hours_365'])} / {_compact(targets['watch_hours_365'])}",
        "",
        f"🌱 Early YPP: {mission['early']['progress']:.1f}%",
    ]
    if mission.get("upcoming"):
        upcoming = mission["upcoming"]
        lines += [
            "",
            "📅 تغییر اعلام‌شده YouTube از 2027-02-01 برای درخواست‌های جدید:",
            f"1,000 Subs + {_compact(upcoming['shorts_90'])} Shorts یا {_compact(upcoming['watch_hours_365'])}h Watch",
        ]
    lines += [
        "",
        "ℹ️ این عدد برآورد API است؛ بخش Earn در YouTube Studio مرجع نهایی Qualified metrics و تأیید YPP است.",
    ]
    return "\n".join(lines)


def _primary_admin_id() -> int | None:
    raw = (get_secret("telegram_primary_admin_id") or "").strip()
    if raw.isdigit():
        return int(raw)
    if Config.TELEGRAM_ADMIN_IDS:
        return sorted(Config.TELEGRAM_ADMIN_IDS)[0]
    with SessionLocal() as db:
        row = db.query(TelegramAdmin).order_by(TelegramAdmin.created_at.asc()).first()
        return int(row.user_id) if row else None


def _send_telegram(text: str) -> bool:
    token = resolve_telegram_token()
    chat_id = _primary_admin_id()
    if not token or not chat_id:
        return False
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        return response.status_code == 200
    except Exception:
        logger.exception("Mission Telegram report failed")
        return False


def dispatch_due_mission_reports(now_utc: datetime | None = None) -> int:
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    with SessionLocal() as db:
        channel_ids = [
            channel.id
            for channel in db.query(YouTubeChannel).filter(YouTubeChannel.is_active.is_(True)).all()
        ]

    sent = 0
    for channel_id in channel_ids:
        with SessionLocal() as db:
            state = db.get(ChannelMissionState, channel_id)
            if state is None:
                state = ChannelMissionState(channel_id=channel_id)
                db.add(state)
                db.commit()
                db.refresh(state)
            if not state.enabled or not state.daily_report:
                continue
            cfg = db.get(ChannelPublishingConfig, channel_id)
            timezone_name = cfg.timezone if cfg else "Asia/Tehran"
            try:
                tz = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                tz = ZoneInfo("Asia/Tehran")
            local_now = now_utc.astimezone(tz)
            local_date = local_now.date().isoformat()
            if local_now.hour != 0 or state.last_report_date == local_date:
                continue

        try:
            mission = build_channel_mission(channel_id, refresh=True, reference_date=local_now.date())
            if not _send_telegram(mission_report_text(mission)):
                continue
            with SessionLocal() as db:
                state = db.get(ChannelMissionState, channel_id)
                if state:
                    state.last_report_date = local_date
                    db.commit()
            sent += 1
        except Exception:
            logger.exception("Daily mission report failed for channel %s", channel_id)

    return sent
