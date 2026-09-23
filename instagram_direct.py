from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

import httpx

from db import SessionLocal, init_db
from integrations import get_secret, resolve_instagram_cookie_blob, resolve_instagram_session, resolve_telegram_token, set_secret
from models import InstagramDirectShare, TelegramAdmin, YouTubeChannel

logger = logging.getLogger("instagram-direct")

POLL_SECONDS = 30
INBOX_URLS = (
    "https://www.instagram.com/api/v1/direct_v2/inbox/",
    "https://i.instagram.com/api/v1/direct_v2/inbox/",
)
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_-]+/?",
    re.I,
)


def _cookies() -> dict[str, str]:
    cookies: dict[str, str] = {}
    raw = resolve_instagram_cookie_blob() or ""
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and "instagram.com" in parts[0].lower():
            name, value = parts[-2], parts[-1]
            if name and value:
                cookies[name] = value
    if not cookies.get("sessionid"):
        sessionid = resolve_instagram_session()
        if sessionid:
            cookies["sessionid"] = sessionid
    return cookies


def _headers(cookies: dict[str, str]) -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "X-IG-App-ID": "936619743392459",
        "X-CSRFToken": cookies.get("csrftoken", ""),
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/direct/inbox/",
    }


def fetch_primary_inbox(limit: int = 20) -> dict:
    cookies = _cookies()
    if not cookies.get("sessionid"):
        raise RuntimeError("Instagram session is not configured")

    last_error = ""
    params = {
        "persistentBadging": "true",
        "use_unified_inbox": "true",
        "limit": str(max(1, min(50, limit))),
    }
    headers = _headers(cookies)
    for url in INBOX_URLS:
        try:
            response = httpx.get(
                url,
                params=params,
                headers=headers,
                cookies=cookies,
                follow_redirects=False,
                timeout=15,
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Instagram inbox returned invalid JSON") from exc
            if isinstance(payload, dict):
                return payload
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location", "")
            if "login" in location:
                raise RuntimeError("Instagram session expired")
        if response.status_code in {401, 403}:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code == 429:
            raise RuntimeError("Instagram rate limited Direct inbox polling")
        last_error = f"HTTP {response.status_code}"

    raise RuntimeError(f"Instagram Direct inbox unavailable: {last_error or 'unknown error'}")


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _find_media_node(value: Any) -> dict | None:
    if isinstance(value, dict):
        product_type = str(value.get("product_type") or "").lower()
        code = value.get("code")
        if code and (
            product_type in {"clips", "feed"}
            or "media_type" in value
            or "image_versions2" in value
            or "video_versions" in value
        ):
            return value
        for key in ("media_share", "clip", "clips_media", "media", "xma_media_share"):
            child = value.get(key)
            found = _find_media_node(child)
            if found:
                return found
        for child in value.values():
            found = _find_media_node(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_media_node(child)
            if found:
                return found
    return None


def extract_shared_media(item: dict) -> dict | None:
    item_type = str(item.get("item_type") or item.get("type") or "").lower()
    plausible = {
        "media_share",
        "reel_share",
        "clip",
        "xma_media_share",
        "link",
        "text",
    }
    if item_type and item_type not in plausible:
        return None

    for text in _walk_strings(item):
        match = INSTAGRAM_URL_RE.search(text)
        if match:
            url = match.group(0)
            media_type = "reel" if "/reel" in url else "post"
            return {
                "url": url,
                "media_type": media_type,
                "title": "Instagram Reel" if media_type == "reel" else "Instagram Post",
                "thumbnail_url": "",
            }

    node = _find_media_node(item)
    if not node:
        return None

    code = str(node.get("code") or "").strip()
    if not code:
        return None
    product_type = str(node.get("product_type") or "").lower()
    is_reel = product_type == "clips" or bool(node.get("clips_metadata"))
    media_type = "reel" if is_reel else "post"
    path = "reel" if is_reel else "p"
    url = f"https://www.instagram.com/{path}/{code}/"

    caption = node.get("caption")
    if isinstance(caption, dict):
        caption_text = str(caption.get("text") or "").strip()
    else:
        caption_text = ""
    first_line = re.sub(r"\s+", " ", caption_text).strip()
    title = first_line[:180] if first_line else ("Instagram Reel" if is_reel else "Instagram Post")

    thumbnail_url = ""
    versions = ((node.get("image_versions2") or {}).get("candidates") or [])
    if versions and isinstance(versions[0], dict):
        thumbnail_url = str(versions[0].get("url") or "")

    return {
        "url": url,
        "media_type": media_type,
        "title": title,
        "thumbnail_url": thumbnail_url,
    }


def _sender_for_item(thread: dict, item: dict) -> tuple[str, str]:
    sender_id = str(item.get("user_id") or item.get("sender_id") or "")
    for user in thread.get("users", []) or []:
        uid = str(user.get("pk") or user.get("id") or "")
        if sender_id and uid == sender_id:
            return sender_id, str(user.get("username") or user.get("full_name") or "")
    users = thread.get("users", []) or []
    if users:
        user = users[0]
        return sender_id or str(user.get("pk") or user.get("id") or ""), str(user.get("username") or user.get("full_name") or "")
    return sender_id, ""


def _primary_admin_id() -> int | None:
    raw = (get_secret("telegram_primary_admin_id") or "").strip()
    if raw.isdigit():
        return int(raw)
    with SessionLocal() as db:
        row = db.query(TelegramAdmin).order_by(TelegramAdmin.created_at.asc()).first()
        return int(row.user_id) if row else None


def _channel_buttons(share_id: int) -> list[list[dict]]:
    with SessionLocal() as db:
        channels = db.query(YouTubeChannel).filter(YouTubeChannel.is_active.is_(True)).order_by(YouTubeChannel.label.asc()).all()
        rows = []
        for channel in channels:
            label = (channel.label or channel.title or f"Channel {channel.id}")[:45]
            rows.append([{
                "text": f"📺 {label}",
                "callback_data": f"igch:{share_id}:{channel.id}",
            }])
        rows.append([{"text": "❌ رد کردن", "callback_data": f"igcancel:{share_id}"}])
        return rows


def notify_pending_share(share_id: int) -> None:
    token = resolve_telegram_token()
    admin_id = _primary_admin_id()
    if not token or not admin_id:
        logger.warning("Cannot notify Instagram share: Telegram token/admin is missing")
        return

    with SessionLocal() as db:
        share = db.get(InstagramDirectShare, share_id)
        if not share or share.status not in {"pending_channel", "notified"}:
            return
        sender = f"@{share.sender_username}" if share.sender_username else (share.sender_id or "Unknown")
        text = (
            f"📥 Instagram Direct جدید\n\n"
            f"👤 فرستنده: {sender}\n"
            f"🎞 نوع: {'Reel' if share.media_type == 'reel' else 'Post'}\n"
            f"📝 {share.title_hint or 'بدون عنوان'}\n"
            f"🔗 {share.media_url}\n\n"
            f"می‌خواهید برای کدام کانال ثبت شود؟"
        )

    payload = {
        "chat_id": admin_id,
        "text": text,
        "disable_web_page_preview": False,
        "reply_markup": {"inline_keyboard": _channel_buttons(share_id)},
    }
    response = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=15,
    )
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200 or not body.get("ok"):
        raise RuntimeError(f"Telegram notification failed with HTTP {response.status_code}")

    message_id = (body.get("result") or {}).get("message_id")
    with SessionLocal() as db:
        share = db.get(InstagramDirectShare, share_id)
        if share:
            share.status = "notified"
            share.telegram_message_id = int(message_id) if message_id else None
            db.commit()


def ingest_inbox(payload: dict, *, notify: bool = True) -> int:
    inbox = payload.get("inbox") or payload
    threads = inbox.get("threads", []) if isinstance(inbox, dict) else []
    created_ids: list[int] = []

    with SessionLocal() as db:
        for thread in threads:
            thread_id = str(thread.get("thread_id") or thread.get("thread_v2_id") or "")
            items = thread.get("items", []) or []
            for item in items:
                media = extract_shared_media(item)
                if not media:
                    continue
                item_id = str(item.get("item_id") or item.get("id") or item.get("client_context") or "")
                timestamp = str(item.get("timestamp") or "")
                item_key = item_id or f"{thread_id}:{timestamp}:{media['url']}"
                exists = db.query(InstagramDirectShare.id).filter_by(item_key=item_key).first()
                if exists:
                    continue
                sender_id, sender_username = _sender_for_item(thread, item)
                row = InstagramDirectShare(
                    item_key=item_key[:255],
                    thread_id=thread_id[:255],
                    item_id=item_id[:255],
                    sender_id=sender_id[:128],
                    sender_username=sender_username[:255],
                    media_url=media["url"],
                    media_type=media["media_type"],
                    title_hint=media["title"][:255],
                    thumbnail_url=media["thumbnail_url"],
                    raw_json=json.dumps(item, ensure_ascii=False)[:100000],
                    status="pending_channel" if notify else "ignored_baseline",
                    detected_at=datetime.utcnow(),
                )
                db.add(row)
                db.flush()
                created_ids.append(row.id)
        db.commit()

    if notify:
        for share_id in created_ids:
            try:
                notify_pending_share(share_id)
            except Exception:
                logger.exception("Failed to notify Telegram for Instagram share %s", share_id)
    return len(created_ids)


def poll_once() -> int:
    payload = fetch_primary_inbox()
    bootstrapped = get_secret("instagram_direct_bootstrapped") == "1"
    count = ingest_inbox(payload, notify=bootstrapped)
    if not bootstrapped:
        set_secret("instagram_direct_bootstrapped", "1")
        logger.info("Instagram Direct baseline created with %s existing shared media items", count)
        return 0
    return count


def run_watcher() -> None:
    init_db()
    logger.info("Instagram Direct watcher started")
    backoff = POLL_SECONDS
    while True:
        try:
            count = poll_once()
            if count:
                logger.info("Detected %s new Instagram shared media items", count)
            backoff = POLL_SECONDS
        except Exception as exc:
            logger.warning("Instagram Direct poll failed: %s", exc)
            backoff = min(300, max(POLL_SECONDS, backoff * 2))
        time.sleep(backoff)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    run_watcher()
