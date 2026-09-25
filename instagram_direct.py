from __future__ import annotations

import json
import os
import logging
from concurrent.futures import ThreadPoolExecutor
import re
import time
from datetime import datetime, timedelta
from typing import Any
from pathlib import Path

import httpx

from db import SessionLocal, init_db
from integrations import get_secret, resolve_instagram_cookie_blob, resolve_instagram_session, resolve_telegram_token, set_secret
from models import InstagramDirectGroupRoute, InstagramDirectShare, TelegramAdmin, YouTubeChannel

logger = logging.getLogger("instagram-direct")
logging.getLogger("httpx").setLevel(logging.WARNING)
_last_disconnect_alert_at: datetime | None = None
_ack_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="instagram-ack")
PLAYWRIGHT_BROWSERS_PATH = os.getenv(
    "PLAYWRIGHT_BROWSERS_PATH",
    "/opt/yt.pedramhs.ir/playwright-browsers",
)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", PLAYWRIGHT_BROWSERS_PATH)

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




def _thread_identifier(thread: dict) -> str:
    return str(thread.get("thread_v2_id") or thread.get("thread_id") or "").strip()


def _thread_members(thread: dict) -> list[str]:
    members: list[str] = []
    for user in thread.get("users", []) or []:
        value = str(user.get("username") or user.get("full_name") or user.get("pk") or "").strip()
        if value and value not in members:
            members.append(value)
    return members


def _is_group_thread(thread: dict) -> bool:
    raw = thread.get("is_group")
    if raw is True or raw == 1 or str(raw).lower() == "true":
        return True
    participants = int(thread.get("participants_count") or 0)
    return participants >= 3 or len(thread.get("users", []) or []) >= 2


def extract_instagram_groups(payload: dict) -> list[dict]:
    inbox = payload.get("inbox") or payload
    threads = inbox.get("threads", []) if isinstance(inbox, dict) else []
    groups: list[dict] = []
    for thread in threads:
        if not isinstance(thread, dict) or not _is_group_thread(thread):
            continue
        thread_id = _thread_identifier(thread)
        if not thread_id:
            continue
        members = _thread_members(thread)
        title = str(thread.get("thread_title") or "").strip()
        if not title:
            title = "Instagram Group " + thread_id[-6:]
        groups.append({
            "thread_id": thread_id[:255],
            "thread_title": title[:255],
            "member_count": int(thread.get("participants_count") or (len(members) + 1)),
            "members": members[:30],
        })
    return groups


def _upsert_group_metadata(db, payload: dict) -> list[InstagramDirectGroupRoute]:
    now = datetime.utcnow()
    rows: list[InstagramDirectGroupRoute] = []
    for group in extract_instagram_groups(payload):
        row = db.get(InstagramDirectGroupRoute, group["thread_id"])
        if row is None:
            row = InstagramDirectGroupRoute(
                thread_id=group["thread_id"],
                discovered_at=now,
            )
            db.add(row)
        row.thread_title = group["thread_title"]
        row.member_count = group["member_count"]
        row.members_json = json.dumps(group["members"], ensure_ascii=False)
        row.last_seen_at = now
        rows.append(row)
    return rows


def sync_instagram_groups(limit: int = 50) -> dict:
    payload = fetch_primary_inbox(limit)
    groups = extract_instagram_groups(payload)
    with SessionLocal() as db:
        _upsert_group_metadata(db, payload)
        db.commit()
    return {"groups": groups, "count": len(groups)}


def list_instagram_group_routes() -> list[dict]:
    with SessionLocal() as db:
        routes = db.query(InstagramDirectGroupRoute).order_by(
            InstagramDirectGroupRoute.enabled.desc(),
            InstagramDirectGroupRoute.thread_title.asc(),
        ).all()
        channels = {row.id: row for row in db.query(YouTubeChannel).all()}
        result = []
        for route in routes:
            try:
                members = json.loads(route.members_json or "[]")
            except Exception:
                members = []
            channel = channels.get(route.channel_id) if route.channel_id else None
            result.append({
                "thread_id": route.thread_id,
                "thread_title": route.thread_title,
                "member_count": route.member_count,
                "members": members if isinstance(members, list) else [],
                "channel_id": route.channel_id,
                "channel_label": (channel.label or channel.title) if channel else "",
                "enabled": bool(route.enabled and channel and channel.is_active),
                "configured": bool(route.channel_id),
                "last_seen_at": route.last_seen_at,
                "last_routed_at": route.last_routed_at,
                "routed_count": int(route.routed_count or 0),
            })
        return result


def set_instagram_group_route(thread_id: str, channel_id: int | None) -> InstagramDirectGroupRoute:
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        raise ValueError("Instagram group id is missing")
    with SessionLocal() as db:
        route = db.get(InstagramDirectGroupRoute, thread_id)
        if not route:
            raise RuntimeError("Instagram group was not found; refresh the group list first")
        if channel_id is None:
            route.channel_id = None
            route.enabled = False
        else:
            channel = db.get(YouTubeChannel, int(channel_id))
            if not channel or not channel.is_active:
                raise RuntimeError("Selected YouTube channel is unavailable")
            route.channel_id = channel.id
            route.enabled = True
        route.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(route)
        return route


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


def _playwright_cookie_list() -> list[dict]:
    raw = resolve_instagram_cookie_blob() or ""
    cookies = []
    for raw_line in raw.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
            http_only = True
        elif line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _include_subdomains, path, secure, expires, name, value = parts[:7]
        if "instagram.com" not in domain.lower() or not name or not value:
            continue
        item = {
            "name": name,
            "value": value,
            "domain": domain if domain.startswith(".") else f".{domain}",
            "path": path or "/",
            "secure": str(secure).upper() == "TRUE",
            "httpOnly": http_only,
            "sameSite": "Lax",
        }
        try:
            expiry = int(expires or "0")
            if expiry > 0:
                item["expires"] = expiry
        except ValueError:
            pass
        cookies.append(item)
    if not any(item.get("name") == "sessionid" for item in cookies):
        sessionid = resolve_instagram_session()
        if sessionid:
            cookies.append({
                "name": "sessionid",
                "value": sessionid,
                "domain": ".instagram.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            })
    return cookies


def send_instagram_received_ack(thread_id: str, text: str = "دریافت شد") -> None:
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        raise ValueError("Instagram thread id is missing")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed") from exc

    cookies = _playwright_cookie_list()
    if not any(item.get("name") == "sessionid" for item in cookies):
        raise RuntimeError("Instagram session is not configured")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = browser.new_context(
                locale="fa-IR",
                viewport={"width": 1280, "height": 900},
            )
            context.add_cookies(cookies)
            page = context.new_page()

            # Bootstrap the web session first. A valid sessionid is enough for
            # Instagram to mint browser-only cookies (mid, ig_did, rur, etc.).
            page.goto(
                "https://www.instagram.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            page.wait_for_timeout(2500)
            home_url = page.url.lower()
            if "/accounts/login" in home_url:
                raise RuntimeError("Instagram web session is not authorized")

            page.goto(
                f"https://www.instagram.com/direct/t/{thread_id}/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            page.wait_for_timeout(3500)
            current_url = page.url.lower()
            if "/accounts/login" in current_url or "/direct/t/" not in current_url:
                raise RuntimeError("Instagram Direct thread could not be opened")

            candidates = [
                page.locator('div[contenteditable="true"][role="textbox"]'),
                page.locator('textarea[placeholder]'),
                page.locator('[contenteditable="true"]'),
            ]
            textbox = None
            for locator in candidates:
                if locator.count():
                    textbox = locator.last
                    break
            if textbox is None:
                raise RuntimeError("Instagram Direct message box was not found")

            textbox.click(timeout=5000)
            textbox.fill(text)
            textbox.press("Enter")
            page.wait_for_timeout(1200)
        finally:
            browser.close()


def _ack_share_safely(share_id: int, thread_id: str, text: str = "دریافت شد") -> None:
    try:
        send_instagram_received_ack(thread_id, text)
        logger.info("Instagram acknowledgement sent for share %s", share_id)
    except Exception as exc:
        logger.warning("Instagram Direct acknowledgement failed for share %s: %s", share_id, exc)




def _notify_auto_route_failure(share_id: int, error: str) -> None:
    token = resolve_telegram_token()
    admin_id = _primary_admin_id()
    if not token or not admin_id:
        return
    with SessionLocal() as db:
        share = db.get(InstagramDirectShare, share_id)
        if not share:
            return
        channel = db.get(YouTubeChannel, share.selected_channel_id) if share.selected_channel_id else None
        channel_name = (channel.label or channel.title) if channel else "Unknown channel"
        title = share.title_hint or share.media_url
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": admin_id,
                "text": (
                    "⚠️ Instagram Group Auto Route ناموفق بود\n\n"
                    f"🎬 {title[:180]}\n"
                    f"📺 {channel_name}\n"
                    f"🧾 Share #{share_id}\n"
                    f"خطا: {error[:700]}"
                ),
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception:
        logger.exception("Could not send Instagram group route failure alert")


def _auto_queue_share(share_id: int) -> bool:
    from jobs import create_job, mark_job_failed
    from publishing import schedule_job_smart

    with SessionLocal() as db:
        share = db.get(InstagramDirectShare, share_id)
        if not share or share.status not in {"auto_route_pending", "auto_route_failed"}:
            return False
        channel_id = share.selected_channel_id
        channel = db.get(YouTubeChannel, channel_id) if channel_id else None
        if not channel or not channel.is_active:
            share.status = "auto_route_failed"
            db.commit()
            raise RuntimeError("Mapped YouTube channel is unavailable")

        duplicate = db.query(InstagramDirectShare).filter(
            InstagramDirectShare.id != share.id,
            InstagramDirectShare.media_url == share.media_url,
            InstagramDirectShare.selected_channel_id == channel_id,
            InstagramDirectShare.upload_job_id.isnot(None),
            InstagramDirectShare.status.in_([
                "scheduled", "queued", "completed", "copyright_blocked",
                "preflight_blocked", "reauth_required",
            ]),
        ).order_by(InstagramDirectShare.id.desc()).first()
        if duplicate:
            share.status = "superseded"
            db.commit()
            logger.info(
                "Instagram group share %s superseded by share %s / job %s",
                share.id, duplicate.id, duplicate.upload_job_id,
            )
            return False

        claimed = db.query(InstagramDirectShare).filter(
            InstagramDirectShare.id == share_id,
            InstagramDirectShare.status.in_(["auto_route_pending", "auto_route_failed"]),
            InstagramDirectShare.upload_job_id.is_(None),
        ).update(
            {
                InstagramDirectShare.status: "auto_queuing",
                InstagramDirectShare.selected_at: datetime.utcnow(),
                InstagramDirectShare.confirmed_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
        db.commit()
        if claimed != 1:
            return False

        share = db.get(InstagramDirectShare, share_id)
        source_url = share.media_url
        title = (share.title_hint or (
            "Instagram Reel" if share.media_type == "reel" else "Instagram Post"
        ))[:255]

    job = None
    try:
        job = create_job(
            channel_id=channel_id,
            source_url=source_url,
            title=title,
            source="instagram-group",
        )
        with SessionLocal() as db:
            share = db.get(InstagramDirectShare, share_id)
            if share:
                share.upload_job_id = job.id
                db.commit()

        schedule = schedule_job_smart(job.id)
        with SessionLocal() as db:
            share = db.get(InstagramDirectShare, share_id)
            route = db.get(InstagramDirectGroupRoute, share.thread_id) if share else None
            if share:
                share.status = "scheduled"
            if route:
                route.routed_count = int(route.routed_count or 0) + 1
                route.last_routed_at = datetime.utcnow()
            db.commit()
        logger.info(
            "Instagram group share %s routed to channel %s as Job %s at %s",
            share_id, channel_id, job.id, schedule.scheduled_for,
        )
        return True
    except Exception as exc:
        if job is not None:
            try:
                mark_job_failed(job.id, f"Instagram group auto route failed: {exc}")
            except Exception:
                logger.exception("Could not mark failed Instagram group Job %s", job.id)
        with SessionLocal() as db:
            share = db.get(InstagramDirectShare, share_id)
            if share:
                share.status = "auto_route_failed"
                db.commit()
        _notify_auto_route_failure(share_id, str(exc))
        raise


def notify_pending_share(share_id: int) -> None:
    token = resolve_telegram_token()
    admin_id = _primary_admin_id()
    if not token or not admin_id:
        logger.warning("Cannot notify Instagram share: Telegram token/admin is missing")
        return

    with SessionLocal() as db:
        share = db.get(InstagramDirectShare, share_id)
        if not share:
            return
        if share.status == "notified" and share.telegram_message_id:
            return
        if share.status not in {"pending_channel", "notified"}:
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
    auto_ids: list[int] = []
    manual_ids: list[int] = []

    with SessionLocal() as db:
        _upsert_group_metadata(db, payload)
        db.flush()
        for thread in threads:
            thread_id = _thread_identifier(thread)
            route = db.get(InstagramDirectGroupRoute, thread_id) if thread_id else None
            auto_channel_id = None
            if notify and route and route.enabled and route.channel_id:
                channel = db.get(YouTubeChannel, route.channel_id)
                if channel and channel.is_active:
                    auto_channel_id = channel.id
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
                recent_duplicate = db.query(InstagramDirectShare.id).filter(
                    InstagramDirectShare.media_url == media["url"],
                    InstagramDirectShare.sender_id == sender_id[:128],
                    InstagramDirectShare.detected_at >= datetime.utcnow() - timedelta(minutes=10),
                ).first()
                if recent_duplicate:
                    continue
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
                    status=(
                        "auto_route_pending"
                        if auto_channel_id
                        else ("pending_channel" if notify else "ignored_baseline")
                    ),
                    selected_channel_id=auto_channel_id,
                    detected_at=datetime.utcnow(),
                )
                db.add(row)
                db.flush()
                created_ids.append(row.id)
                if auto_channel_id:
                    auto_ids.append(row.id)
                elif notify:
                    manual_ids.append(row.id)
        db.commit()

    if notify:
        for share_id in auto_ids:
            routed = False
            try:
                routed = _auto_queue_share(share_id)
            except Exception:
                logger.exception("Instagram group auto route failed for share %s", share_id)
            with SessionLocal() as db:
                share = db.get(InstagramDirectShare, share_id)
                thread_id = share.thread_id if share else ""
            if thread_id:
                ack_text = "دریافت شد؛ وارد صف انتشار شد ✅" if routed else "دریافت شد"
                _ack_executor.submit(_ack_share_safely, share_id, thread_id, ack_text)

        for share_id in manual_ids:
            try:
                notify_pending_share(share_id)
            except Exception:
                logger.exception("Failed to notify Telegram for Instagram share %s", share_id)

            with SessionLocal() as db:
                share = db.get(InstagramDirectShare, share_id)
                thread_id = share.thread_id if share else ""
            if thread_id:
                _ack_executor.submit(_ack_share_safely, share_id, thread_id)
    return len(created_ids)


def notify_instagram_disconnect(detail: str) -> None:
    global _last_disconnect_alert_at
    now = datetime.utcnow()
    if _last_disconnect_alert_at and now - _last_disconnect_alert_at < timedelta(hours=6):
        return

    token = resolve_telegram_token()
    admin_id = _primary_admin_id()
    if not token or not admin_id:
        return

    payload = {
        "chat_id": admin_id,
        "text": (
            "⚠️ Instagram Direct قطع شده است.\n\n"
            "Watcher دیگر Inbox را نمی‌خواند، بنابراین Shareهای جدید در Telegram نمایش داده نمی‌شوند.\n"
            "از پنل Integrations یک cookies.txt تازه و کامل برای Instagram آپلود کن.\n\n"
            f"وضعیت: {detail[:180]}"
        ),
        "disable_web_page_preview": True,
    }
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=15,
        )
        if response.status_code == 200:
            _last_disconnect_alert_at = now
    except Exception:
        logger.exception("Failed to send Instagram disconnect alert")


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
            detail = str(exc)
            logger.warning("Instagram Direct poll failed: %s", detail)
            if "session expired" in detail.lower() or "login" in detail.lower():
                notify_instagram_disconnect(detail)
            backoff = min(300, max(POLL_SECONDS, backoff * 2))
        time.sleep(backoff)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    run_watcher()