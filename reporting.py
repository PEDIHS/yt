from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from db import SessionLocal
from integrations import delete_secret, get_secret, resolve_telegram_token, set_secret
from models import YouTubeChannel

logger = logging.getLogger("reporting")

REPORTING_GROUP_KEY = "telegram_reporting_group_id"
REPORTING_TITLE_KEY = "telegram_reporting_group_title"
REPORTING_TOPICS_KEY = "telegram_reporting_topics_json"
REPORTING_SYNC_KEY = "telegram_reporting_last_sync_at"
REPORTING_ERROR_KEY = "telegram_reporting_last_error"

SYSTEM_TOPICS: dict[str, str] = {
    "overview": "🧭 وضعیت سیستم",
    "connections": "🔌 اتصالات",
    "instagram": "📥 اینستاگرام",
    "processing": "⚙️ پردازش ویدیو",
    "publishing": "🚀 انتشار",
    "queue": "🗓 صف و زمان‌بندی",
    "copyright": "🛡 کپی‌رایت",
    "errors": "🚨 خطاها",
    "analytics": "📊 گزارش روزانه",
}

_topic_lock = threading.Lock()


def _now_tehran_text() -> str:
    try:
        return datetime.now(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def _telegram_call(method: str, payload: dict[str, Any], *, timeout: int = 15) -> dict:
    token = resolve_telegram_token()
    if not token:
        raise RuntimeError("Telegram Bot Token is not configured")

    url = f"https://api.telegram.org/bot{token}/{method}"
    response = httpx.post(url, json=payload, timeout=timeout)
    try:
        body = response.json()
    except Exception:
        body = {}

    if response.status_code == 429:
        retry_after = int(((body.get("parameters") or {}).get("retry_after") or 1))
        if 0 < retry_after <= 10:
            time.sleep(retry_after)
            response = httpx.post(url, json=payload, timeout=timeout)
            try:
                body = response.json()
            except Exception:
                body = {}

    if response.status_code != 200 or not body.get("ok"):
        description = str(body.get("description") or f"HTTP {response.status_code}")
        raise RuntimeError(f"Telegram {method} failed: {description}")
    return body.get("result") or {}


def _topic_map() -> dict[str, int]:
    raw = get_secret(REPORTING_TOPICS_KEY)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    topics = payload.get("topics") if isinstance(payload, dict) else {}
    result: dict[str, int] = {}
    if isinstance(topics, dict):
        for key, value in topics.items():
            try:
                result[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    return result


def _save_topic_map(group_id: int, topics: dict[str, int]) -> None:
    set_secret(
        REPORTING_TOPICS_KEY,
        json.dumps(
            {
                "version": 1,
                "group_id": int(group_id),
                "topics": topics,
                "updated_at": datetime.utcnow().isoformat(),
            },
            ensure_ascii=False,
        ),
    )


def reporting_status() -> dict:
    raw_group = (get_secret(REPORTING_GROUP_KEY) or "").strip()
    topics = _topic_map()
    return {
        "configured": bool(raw_group),
        "group_id": raw_group,
        "title": get_secret(REPORTING_TITLE_KEY),
        "topic_count": len(topics),
        "system_topic_count": sum(1 for key in topics if key in SYSTEM_TOPICS),
        "channel_topic_count": sum(1 for key in topics if key.startswith("channel:")),
        "last_sync_at": get_secret(REPORTING_SYNC_KEY),
        "last_error": get_secret(REPORTING_ERROR_KEY),
    }


def validate_reporting_group(group_id: int) -> dict:
    group_id = int(group_id)
    me = _telegram_call("getMe", {})
    bot_id = int(me.get("id") or 0)
    if not bot_id:
        raise RuntimeError("Could not resolve Telegram bot identity")

    chat = _telegram_call("getChat", {"chat_id": group_id})
    if str(chat.get("type") or "") != "supergroup":
        raise ValueError("گروه گزارش باید Supergroup باشد.")
    if not bool(chat.get("is_forum")):
        raise ValueError("Topics برای این گروه فعال نیست. ابتدا گروه را به Forum تبدیل و Topics را فعال کن.")

    member = _telegram_call(
        "getChatMember",
        {"chat_id": group_id, "user_id": bot_id},
    )
    status = str(member.get("status") or "")
    if status not in {"administrator", "creator"}:
        raise ValueError("ربات باید در گروه گزارشات Admin باشد.")
    if status == "administrator" and not bool(member.get("can_manage_topics")):
        raise ValueError("دسترسی Manage Topics برای ربات فعال نیست.")

    return {
        "group_id": group_id,
        "title": str(chat.get("title") or "Telegram Reporting"),
        "bot_username": str(me.get("username") or ""),
        "member_status": status,
        "can_manage_topics": True,
        "is_forum": True,
    }


def _topic_name(value: str) -> str:
    name = " ".join(str(value or "").split()).strip()
    return (name or "Report")[:128]


def _create_topic(group_id: int, name: str) -> int:
    result = _telegram_call(
        "createForumTopic",
        {"chat_id": int(group_id), "name": _topic_name(name)},
    )
    thread_id = int(result.get("message_thread_id") or 0)
    if not thread_id:
        raise RuntimeError(f"Telegram did not return a topic id for {name}")
    return thread_id


def _channel_topic_definitions() -> list[tuple[str, str]]:
    with SessionLocal() as db:
        channels = db.query(YouTubeChannel).order_by(YouTubeChannel.id.asc()).all()
        return [
            (
                f"channel:{channel.id}",
                f"📺 {(channel.label or channel.title or f'Channel {channel.id}')}",
            )
            for channel in channels
        ]


def sync_reporting_topics() -> dict:
    raw_group = (get_secret(REPORTING_GROUP_KEY) or "").strip()
    if not raw_group:
        raise RuntimeError("گروه گزارشات هنوز تنظیم نشده است.")
    group_id = int(raw_group)
    validated = validate_reporting_group(group_id)

    with _topic_lock:
        topics = _topic_map()
        definitions = list(SYSTEM_TOPICS.items()) + _channel_topic_definitions()
        created: list[dict] = []
        existing: list[dict] = []

        for key, name in definitions:
            if key in topics and int(topics[key]) > 0:
                existing.append({"key": key, "name": name, "thread_id": topics[key]})
                continue
            thread_id = _create_topic(group_id, name)
            topics[key] = thread_id
            _save_topic_map(group_id, topics)
            created.append({"key": key, "name": name, "thread_id": thread_id})
            time.sleep(0.25)

        set_secret(REPORTING_TITLE_KEY, validated["title"])
        set_secret(REPORTING_SYNC_KEY, datetime.utcnow().isoformat())
        set_secret(REPORTING_ERROR_KEY, "")
        _save_topic_map(group_id, topics)

    overview_id = topics.get("overview")
    if overview_id:
        _send_message(
            group_id,
            overview_id,
            (
                "✅ Reporting Center بروزرسانی شد.\n\n"
                f"🧩 Topic جدید: {len(created)}\n"
                f"📌 Topic موجود: {len(existing)}\n"
                f"📊 مجموع: {len(topics)}\n"
                f"🕒 {_now_tehran_text()}"
            ),
            severity="info",
        )

    return {
        **validated,
        "created": created,
        "existing": existing,
        "topic_count": len(topics),
    }


def configure_reporting_group(group_id: int) -> dict:
    group_id = int(group_id)
    validated = validate_reporting_group(group_id)
    old_group = (get_secret(REPORTING_GROUP_KEY) or "").strip()
    if old_group and old_group != str(group_id):
        _save_topic_map(group_id, {})
    elif not old_group:
        _save_topic_map(group_id, {})

    set_secret(REPORTING_GROUP_KEY, str(group_id))
    set_secret(REPORTING_TITLE_KEY, validated["title"])
    set_secret(REPORTING_ERROR_KEY, "")
    return sync_reporting_topics()


def disconnect_reporting_group() -> None:
    for key in (
        REPORTING_GROUP_KEY,
        REPORTING_TITLE_KEY,
        REPORTING_TOPICS_KEY,
        REPORTING_SYNC_KEY,
        REPORTING_ERROR_KEY,
    ):
        delete_secret(key)


def _send_message(
    group_id: int,
    thread_id: int | None,
    text: str,
    *,
    severity: str = "info",
    reply_markup: dict | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": int(group_id),
        "text": str(text or "")[:4090],
        "disable_web_page_preview": True,
        "disable_notification": severity not in {"error", "critical"},
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        _telegram_call("sendMessage", payload, timeout=10)
        return True
    except Exception as exc:
        logger.warning("Reporting message failed: %s", exc)
        set_secret(REPORTING_ERROR_KEY, str(exc)[:1000])
        return False


def report_event(
    *,
    category: str,
    title: str,
    message: str = "",
    severity: str = "info",
    channel_id: int | None = None,
    job_id: int | None = None,
    fields: dict[str, Any] | None = None,
    reply_markup: dict | None = None,
) -> bool:
    raw_group = (get_secret(REPORTING_GROUP_KEY) or "").strip()
    if not raw_group:
        return False

    try:
        group_id = int(raw_group)
    except ValueError:
        return False

    topics = _topic_map()
    category = category if category in SYSTEM_TOPICS else "overview"
    topic_keys: list[str] = [category]
    if severity in {"error", "critical"} and category != "errors":
        topic_keys.append("errors")
    if channel_id is not None:
        topic_keys.append(f"channel:{int(channel_id)}")

    # Deduplicate thread IDs; a message can be mirrored to its category and channel topic.
    thread_ids: list[int] = []
    for key in topic_keys:
        thread_id = topics.get(key)
        if thread_id and int(thread_id) not in thread_ids:
            thread_ids.append(int(thread_id))

    channel_name = ""
    if channel_id is not None:
        with SessionLocal() as db:
            channel = db.get(YouTubeChannel, int(channel_id))
            if channel:
                channel_name = channel.label or channel.title or f"Channel #{channel_id}"

    severity_icon = {
        "info": "ℹ️",
        "success": "✅",
        "warning": "⚠️",
        "error": "🚨",
        "critical": "🛑",
    }.get(severity, "ℹ️")

    lines = [f"{severity_icon} {title}", "", f"🕒 {_now_tehran_text()}"]
    if channel_name:
        lines.append(f"📺 {channel_name}")
    if job_id is not None:
        lines.append(f"🧾 Job #{int(job_id)}")
    for key, value in (fields or {}).items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {str(value)[:700]}")
    if message:
        lines.extend(["", str(message)[:2400]])
    text = "\n".join(lines)

    sent = False
    if not thread_ids:
        # Fallback to General if topics were not synced yet.
        return _send_message(group_id, None, text, severity=severity, reply_markup=reply_markup)

    for thread_id in thread_ids:
        sent = _send_message(
            group_id,
            thread_id,
            text,
            severity=severity,
            reply_markup=reply_markup,
        ) or sent
    return sent
