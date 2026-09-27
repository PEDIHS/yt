from __future__ import annotations

import json
import os
import logging
import random
import re
import threading
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
_private_client_lock = threading.Lock()
_private_client = None
_private_client_sessionid: str | None = None
_random = random.SystemRandom()

ACK_MESSAGES = (
    "اوکی، گرفتمش 👌",
    "رسید، مرسی 🙌",
    "گرفتمش 👀",
    "اوکیه، دیدمش ✌️",
    "رسید دستم 🔥",
    "اینم گرفتم 😎",
    "اوکی، رسید 👌",
    "مرسی، گرفتمش 🙌",
    "رسید پیشم 👌",
    "گرفتم، دمت گرم 🤝",
    "اوکیه، رسید 🙏",
    "دیدمش 👀",
    "اینم رسید 🔥",
    "گرفتمش، مرسی ✌️",
    "اوکی، دستم رسید 😎",
    "رسید بهم 🙌",
    "گرفتم، ممنون 👌",
    "اوکی شد، گرفتمش 👀",
    "دیدم، مرسی 🤍",
    "این یکی هم رسید 😄",
    "رسید، گرفتمش ✌️",
    "گرفتمش رفیق 🙌",
    "اوکیه، مرسی 🔥",
    "اینم دستم رسید 👌",
    "گرفتم، حله 😎",
    "رسید، دیدمش 👀",
    "اوکی، اینم گرفتم 🙏",
    "مرسی، رسید پیشم 🤝",
    "گرفتمش، عالی 👌",
    "اوکیه، گرفتم 👀",
    "رسید رفیق ✌️",
    "اینم گرفتم، مرسی 🙌",
    "دیدمش، اوکیه 😎",
    "گرفتم، رسید 🔥",
    "اوکی، مرسی بابتش 👌",
    "رسید دستم، مرسی 🤍",
    "گرفتمش داداش 😄",
    "اوکی، اینم رسید 👀",
    "مرسی، گرفتمش ✌️",
    "رسید، اوکیه 🙌",
    "گرفتم، دیدمش 👌",
    "اینم رسید پیشم 🔥",
    "اوکیه، دستم رسید 😎",
    "گرفتمش، دمت گرم 🤝",
    "رسید، ممنون 🙏",
    "دیدمش، مرسی 👀",
    "این یکی هم گرفتم ✌️",
    "اوکی، رسید بهم 🙌",
    "گرفتم، مرسی رفیق 👌",
    "رسید، حله 😎",
    "اوکیه، اینم گرفتم 🔥",
    "مرسی، دیدمش 🤍",
    "گرفتمش، رسید 👀",
    "اینم اوکیه 👌",
    "رسید دستم رفیق 🙌",
    "گرفتم، مرسی بابتش ✌️",
    "اوکی شد، رسید 😄",
    "دیدمش، دستم رسید 👀",
    "اینم گرفتم، حله 🔥",
    "رسید، گرفتم 😎",
)
REACTION_EMOJIS = ("❤️", "🔥", "😂", "👏", "😍", "👍")

PLAYWRIGHT_BROWSERS_PATH = os.getenv(
    "PLAYWRIGHT_BROWSERS_PATH",
    "/opt/yt.pedramhs.ir/playwright-browsers",
)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", PLAYWRIGHT_BROWSERS_PATH)

POLL_MIN_SECONDS = 10 * 60
POLL_MAX_SECONDS = 20 * 60
MAX_ITEMS_PER_CYCLE = 5
ACK_INITIAL_DELAY_SECONDS = (5.0, 14.0)
ACK_REPLY_DELAY_SECONDS = (3.0, 8.0)
BETWEEN_ITEMS_DELAY_SECONDS = (18.0, 45.0)
CHALLENGE_PAUSE_SECONDS = (8 * 60 * 60, 12 * 60 * 60)
RATE_LIMIT_PAUSE_SECONDS = (2 * 60 * 60, 4 * 60 * 60)
AUTH_PAUSE_SECONDS = (3 * 60 * 60, 6 * 60 * 60)
GENERIC_ERROR_PAUSE_SECONDS = (20 * 60, 40 * 60)
INBOX_URLS = (
    "https://www.instagram.com/api/v1/direct_v2/inbox/",
    "https://i.instagram.com/api/v1/direct_v2/inbox/",
)
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_-]+/?",
    re.I,
)
HASHTAG_RE = re.compile(r"(?<![\w#])#([\w\u200c]+)", re.UNICODE)
URL_RE = re.compile(r"https?://\S+", re.I)


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


def _parse_pause_until() -> datetime | None:
    raw = get_secret("instagram_direct_pause_until")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _pause_remaining_seconds() -> int:
    pause_until = _parse_pause_until()
    if not pause_until:
        return 0
    return max(0, int((pause_until - datetime.utcnow()).total_seconds()))


def _clear_runtime_pause() -> None:
    set_secret("instagram_direct_pause_until", "")
    set_secret("instagram_direct_pause_reason", "")
    set_secret("instagram_direct_runtime_status", "ready")
    set_secret("instagram_direct_last_error", "")


def _set_runtime_pause(reason: str, seconds_range: tuple[int, int], status: str) -> int:
    seconds = _random.randint(int(seconds_range[0]), int(seconds_range[1]))
    pause_until = datetime.utcnow() + timedelta(seconds=seconds)
    set_secret("instagram_direct_pause_until", pause_until.isoformat())
    set_secret("instagram_direct_pause_reason", (reason or status)[:1000])
    set_secret("instagram_direct_runtime_status", status)
    set_secret("instagram_direct_last_error", (reason or status)[:2000])
    logger.warning(
        "Instagram watcher paused until %s UTC (%s)",
        pause_until.isoformat(timespec="seconds"),
        status,
    )
    return seconds


def _instagram_error_kind(detail: str) -> str:
    value = (detail or "").lower()
    if any(token in value for token in (
        "challenge",
        "manual verification",
        "checkpoint",
        "scraping_warning",
        "challenge_required",
    )):
        return "challenge"
    if "429" in value or "rate limit" in value or "too many request" in value:
        return "rate_limited"
    if any(token in value for token in (
        "login_required",
        "session expired",
        "not authorized",
        "accounts/login",
    )):
        return "auth"
    return ""


def _pause_for_instagram_error(detail: str) -> int:
    kind = _instagram_error_kind(detail)
    if kind == "challenge":
        return _set_runtime_pause(detail, CHALLENGE_PAUSE_SECONDS, "challenge_paused")
    if kind == "rate_limited":
        return _set_runtime_pause(detail, RATE_LIMIT_PAUSE_SECONDS, "rate_limited")
    if kind == "auth":
        return _set_runtime_pause(detail, AUTH_PAUSE_SECONDS, "auth_paused")
    return _set_runtime_pause(detail, GENERIC_ERROR_PAUSE_SECONDS, "error_backoff")


def _item_timestamp_value(item: dict) -> int:
    raw = item.get("timestamp") or item.get("taken_at") or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _item_detected_at(item: dict) -> datetime:
    value = _item_timestamp_value(item)
    if value <= 0:
        return datetime.utcnow()
    try:
        if value > 10**14:
            return datetime.utcfromtimestamp(value / 1_000_000)
        if value > 10**11:
            return datetime.utcfromtimestamp(value / 1_000)
        return datetime.utcfromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return datetime.utcnow()


def _reset_private_client() -> None:
    global _private_client, _private_client_sessionid
    with _private_client_lock:
        _private_client = None
        _private_client_sessionid = None


def _instagram_private_client():
    global _private_client, _private_client_sessionid
    sessionid = resolve_instagram_session()
    if not sessionid:
        raise RuntimeError("Instagram session is not configured")

    with _private_client_lock:
        if _private_client is not None and _private_client_sessionid == sessionid:
            return _private_client

        try:
            from instagrapi import Client
        except ImportError as exc:
            raise RuntimeError("instagrapi is not installed") from exc

        client = Client()
        stored_settings = get_secret("instagram_private_api_settings")
        if stored_settings:
            try:
                settings = json.loads(stored_settings)
                if isinstance(settings, dict):
                    client.set_settings(settings)
            except Exception:
                logger.warning("Stored Instagram private API settings could not be restored")

        client.login_by_sessionid(sessionid)
        try:
            set_secret(
                "instagram_private_api_settings",
                json.dumps(client.get_settings(), ensure_ascii=False),
            )
        except Exception:
            logger.warning("Instagram private API settings could not be persisted")

        _private_client = client
        _private_client_sessionid = sessionid
        return client


def _active_linked_group_ids() -> list[str]:
    with SessionLocal() as db:
        rows = db.query(InstagramDirectGroupRoute).filter(
            InstagramDirectGroupRoute.enabled.is_(True),
            InstagramDirectGroupRoute.channel_id.isnot(None),
        ).order_by(InstagramDirectGroupRoute.thread_id.asc()).all()
        result: list[str] = []
        for route in rows:
            channel = db.get(YouTubeChannel, route.channel_id) if route.channel_id else None
            if channel and channel.is_active and route.thread_id:
                result.append(str(route.thread_id))
        return result


def fetch_linked_group_threads(limit: int = 20) -> dict:
    """Fetch only explicitly linked Instagram group threads.

    The background watcher uses this instead of the whole Direct inbox so
    unrelated DMs and unlinked groups are never scanned for media.
    """
    thread_ids = _active_linked_group_ids()
    if not thread_ids:
        return {"inbox": {"threads": []}}

    client = _instagram_private_client()
    threads: list[dict] = []
    errors: list[str] = []
    params = {
        "visual_message_return_type": "unseen",
        "direction": "older",
        "seq_id": "40065",
        "limit": str(max(1, min(50, limit))),
    }

    for thread_id in thread_ids:
        try:
            result = client.private_request(
                f"direct_v2/threads/{thread_id}/",
                params=params,
            )
            thread = result.get("thread") if isinstance(result, dict) else None
            if isinstance(thread, dict):
                threads.append(thread)
            else:
                errors.append(f"{thread_id}: empty thread")
        except Exception as exc:
            errors.append(f"{thread_id}: {type(exc).__name__}: {exc}")
            logger.warning("Linked Instagram group %s could not be fetched: %s", thread_id, exc)

    if errors and not threads:
        _reset_private_client()
        raise RuntimeError("Instagram linked-group polling failed: " + "; ".join(errors[:3]))

    return {"inbox": {"threads": threads}}


def _send_instagram_reaction(
    thread_id: str,
    item_id: str,
    emoji: str,
    *,
    client_context: str = "",
    target_item_type: str = "",
) -> None:
    if not thread_id or not item_id:
        return
    client = _instagram_private_client()
    ok = client.direct_send_reaction(
        int(thread_id),
        int(item_id),
        emoji=emoji,
        client_context=client_context or None,
        action_source="reaction_sheet",
        target_item_type=target_item_type or None,
    )
    if not ok:
        raise RuntimeError("Instagram did not confirm the message reaction")


def _send_instagram_text_private(
    thread_id: str,
    text: str,
    *,
    reply_to_item_id: str = "",
    reply_to_client_context: str = "",
) -> None:
    if not thread_id:
        raise ValueError("Instagram thread id is missing")

    client = _instagram_private_client()
    if not reply_to_item_id:
        client.direct_answer(int(thread_id), text)
        return

    token = client.generate_mutation_token()
    payload = {
        "action": "send_item",
        "is_x_transport_forward": "false",
        "send_silently": "false",
        "is_shh_mode": "0",
        "send_attribution": "message_button",
        "client_context": token,
        "device_id": client.android_device_id,
        "mutation_token": token,
        "btt_dual_send": "false",
        "nav_chain": (
            "1qT:feed_timeline:1,1qT:feed_timeline:2,1qT:feed_timeline:3,"
            "7Az:direct_inbox:4,7Az:direct_inbox:5,5rG:direct_thread:7"
        ),
        "is_ae_dual_send": "false",
        "offline_threading_id": token,
        "thread_ids": json.dumps([int(thread_id)]),
        "text": text,
        "replied_to_action_source": "swipe",
        "replied_to_item_id": str(reply_to_item_id),
    }
    if reply_to_client_context:
        payload["replied_to_client_context"] = str(reply_to_client_context)

    result = client.private_request(
        "direct_v2/threads/broadcast/text/",
        data=client.with_default_data(payload),
        with_signature=False,
    )
    if isinstance(result, dict) and result.get("status") not in {None, "ok"}:
        raise RuntimeError("Instagram did not confirm the direct reply")


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


def _caption_metadata(caption_text: str, fallback_title: str) -> dict[str, Any]:
    caption_text = str(caption_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    tags: list[str] = []
    seen: set[str] = set()
    for match in HASHTAG_RE.finditer(caption_text):
        tag = match.group(1).strip("_")
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= 80:
            break

    first_meaningful = ""
    for raw_line in caption_text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        line = URL_RE.sub("", line)
        line = HASHTAG_RE.sub("", line)
        line = re.sub(r"\s+", " ", line).strip(" \t-–—|•·.,،؛;:")
        if line:
            first_meaningful = line
            break

    return {
        "title": (first_meaningful or fallback_title).strip()[:180],
        "description": first_meaningful[:1000],
        "hashtags": " ".join(f"#{tag}" for tag in tags),
        "tags": tags,
    }


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

    node = _find_media_node(item)
    if node:
        code = str(node.get("code") or "").strip()
        if code:
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
            fallback_title = "Instagram Reel" if is_reel else "Instagram Post"
            caption_meta = _caption_metadata(caption_text, fallback_title)

            thumbnail_url = ""
            versions = ((node.get("image_versions2") or {}).get("candidates") or [])
            if versions and isinstance(versions[0], dict):
                thumbnail_url = str(versions[0].get("url") or "")

            return {
                "url": url,
                "media_type": media_type,
                "thumbnail_url": thumbnail_url,
                **caption_meta,
            }

    for text_value in _walk_strings(item):
        match = INSTAGRAM_URL_RE.search(text_value)
        if match:
            url = match.group(0)
            media_type = "reel" if "/reel" in url else "post"
            fallback_title = "Instagram Reel" if media_type == "reel" else "Instagram Post"
            return {
                "url": url,
                "media_type": media_type,
                "title": fallback_title,
                "description": "",
                "hashtags": "",
                "tags": [],
                "thumbnail_url": "",
            }
    return None


def share_upload_metadata(share: InstagramDirectShare) -> dict[str, Any]:
    fallback_title = share.title_hint or (
        "Instagram Reel" if share.media_type == "reel" else "Instagram Post"
    )
    try:
        item = json.loads(share.raw_json or "{}")
    except Exception:
        item = {}
    media = extract_shared_media(item) if isinstance(item, dict) else None
    if not media:
        return {
            "title": fallback_title[:180],
            "description": "",
            "hashtags": "",
            "tags": [],
        }
    return {
        "title": str(media.get("title") or fallback_title)[:180],
        "description": str(media.get("description") or "")[:1000],
        "hashtags": str(media.get("hashtags") or "")[:4000],
        "tags": list(media.get("tags") or [])[:80],
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


def send_instagram_received_ack(
    thread_id: str,
    text: str,
    *,
    reply_to_item_id: str = "",
    reply_to_client_context: str = "",
) -> None:
    try:
        _send_instagram_text_private(
            thread_id,
            text,
            reply_to_item_id=reply_to_item_id,
            reply_to_client_context=reply_to_client_context,
        )
        return
    except Exception as exc:
        detail = str(exc)
        if reply_to_item_id or _instagram_error_kind(detail):
            raise
        logger.warning("Instagram private text send failed; trying browser fallback: %s", exc)
        _reset_private_client()

    _send_instagram_received_ack_browser(thread_id, text)


def _send_instagram_received_ack_browser(thread_id: str, text: str) -> None:
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


def _ack_share_safely(
    share_id: int,
    thread_id: str,
    item_id: str,
    text: str,
    reaction_emoji: str,
) -> None:
    client_context = ""
    target_item_type = ""
    with SessionLocal() as db:
        share = db.get(InstagramDirectShare, share_id)
        if share and share.raw_json:
            try:
                raw = json.loads(share.raw_json)
                client_context = str(raw.get("client_context") or "")
                target_item_type = str(raw.get("item_type") or "")
            except Exception:
                pass

    time.sleep(_random.uniform(*ACK_INITIAL_DELAY_SECONDS))

    if item_id:
        try:
            _send_instagram_reaction(
                thread_id,
                item_id,
                reaction_emoji,
                client_context=client_context,
                target_item_type=target_item_type,
            )
            logger.info("Instagram reaction %s sent for share %s", reaction_emoji, share_id)
        except Exception as exc:
            detail = str(exc)
            logger.warning("Instagram reaction failed for share %s: %s", share_id, detail)
            _reset_private_client()
            if _instagram_error_kind(detail):
                raise

    time.sleep(_random.uniform(*ACK_REPLY_DELAY_SECONDS))
    try:
        send_instagram_received_ack(
            thread_id,
            text,
            reply_to_item_id=item_id,
            reply_to_client_context=client_context,
        )
        logger.info("Instagram direct reply sent for share %s", share_id)
    except Exception as exc:
        detail = str(exc)
        logger.warning("Instagram Direct reply failed for share %s: %s", share_id, detail)
        _reset_private_client()
        if _instagram_error_kind(detail):
            raise


def _process_pending_group_shares(limit: int = MAX_ITEMS_PER_CYCLE) -> int:
    with SessionLocal() as db:
        rows = (
            db.query(InstagramDirectShare.id)
            .filter(InstagramDirectShare.status == "auto_route_pending")
            .order_by(
                InstagramDirectShare.detected_at.asc(),
                InstagramDirectShare.id.asc(),
            )
            .limit(max(1, int(limit)))
            .all()
        )
        share_ids = [int(row[0]) for row in rows]

    processed = 0
    for index, share_id in enumerate(share_ids):
        try:
            _auto_queue_share(share_id)
        except Exception as exc:
            detail = str(exc)
            logger.exception("Instagram group auto route failed for share %s", share_id)
            if _instagram_error_kind(detail):
                raise
            continue

        with SessionLocal() as db:
            share = db.get(InstagramDirectShare, share_id)
            thread_id = share.thread_id if share else ""
            item_id = share.item_id if share else ""

        if thread_id:
            _ack_share_safely(
                share_id,
                thread_id,
                item_id,
                _random.choice(ACK_MESSAGES),
                _random.choice(REACTION_EMOJIS),
            )
        processed += 1

        if index < len(share_ids) - 1:
            time.sleep(_random.uniform(*BETWEEN_ITEMS_DELAY_SECONDS))

    return processed



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
        metadata = share_upload_metadata(share)
        title = metadata["title"][:255]
        description = metadata["description"]
        hashtags = metadata["hashtags"]

    job = None
    try:
        job = create_job(
            channel_id=channel_id,
            source_url=source_url,
            title=title,
            source="instagram-group",
            description=description,
            hashtags=hashtags,
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
    candidates: list[tuple[int, dict, str, int]] = []

    with SessionLocal() as db:
        for thread in threads:
            if not isinstance(thread, dict) or not _is_group_thread(thread):
                continue

            thread_id = _thread_identifier(thread)
            if not thread_id:
                continue

            route = db.get(InstagramDirectGroupRoute, thread_id)
            if not route or not route.enabled or not route.channel_id:
                continue

            channel = db.get(YouTubeChannel, route.channel_id)
            if not channel or not channel.is_active:
                continue

            for item in thread.get("items", []) or []:
                media = extract_shared_media(item)
                if media:
                    candidates.append((_item_timestamp_value(item), item, thread_id, channel.id))

        # Instagram usually returns newest first. Persist oldest first so the
        # processing queue follows the order in which people sent the Reels.
        candidates.sort(key=lambda row: row[0])

        created_ids: list[int] = []
        for _ts, item, thread_id, auto_channel_id in candidates:
            media = extract_shared_media(item)
            if not media:
                continue

            item_id = str(item.get("item_id") or item.get("id") or item.get("client_context") or "")
            timestamp = str(item.get("timestamp") or "")
            item_key = item_id or f"{thread_id}:{timestamp}:{media['url']}"
            exists = db.query(InstagramDirectShare.id).filter_by(item_key=item_key).first()
            if exists:
                continue

            sender_id, sender_username = _sender_for_item(
                {"thread_id": thread_id, "users": []},
                item,
            )
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
                status="auto_route_pending" if notify else "ignored_baseline",
                selected_channel_id=auto_channel_id,
                detected_at=_item_detected_at(item),
            )
            db.add(row)
            db.flush()
            created_ids.append(row.id)

        db.commit()
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
    payload = fetch_linked_group_threads()
    bootstrapped = get_secret("instagram_direct_bootstrapped") == "1"
    discovered = ingest_inbox(payload, notify=bootstrapped)
    if not bootstrapped:
        set_secret("instagram_direct_bootstrapped", "1")
        logger.info("Instagram Direct baseline created with %s existing shared media items", discovered)
        set_secret("instagram_direct_runtime_status", "ready")
        return 0

    processed = _process_pending_group_shares(MAX_ITEMS_PER_CYCLE)
    set_secret("instagram_direct_runtime_status", "ready")
    set_secret("instagram_direct_last_success_at", datetime.utcnow().isoformat())
    set_secret("instagram_direct_last_error", "")
    return discovered + processed


def run_watcher() -> None:
    init_db()
    logger.info("Instagram linked-group watcher started (10-20 minute adaptive polling)")
    sleep_seconds = _random.randint(POLL_MIN_SECONDS, POLL_MAX_SECONDS)

    while True:
        pause_remaining = _pause_remaining_seconds()
        if pause_remaining > 0:
            # Local wait only: no Instagram request is made during the circuit-breaker pause.
            time.sleep(min(60, pause_remaining))
            continue

        try:
            count = poll_once()
            if count:
                logger.info("Handled %s Instagram group discovery/processing events", count)
            _clear_runtime_pause()
            sleep_seconds = _random.randint(POLL_MIN_SECONDS, POLL_MAX_SECONDS)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("Instagram Direct cycle failed: %s", detail)
            kind = _instagram_error_kind(detail)
            if kind in {"challenge", "auth"}:
                notify_instagram_disconnect(detail)
            sleep_seconds = _pause_for_instagram_error(detail)

        time.sleep(sleep_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    run_watcher()