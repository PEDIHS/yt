from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import secrets
from datetime import datetime
import time
from typing import Optional

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

from config import Config
from db import SessionLocal
from models import YouTubeChannel
from security import decrypt_secret, encrypt_secret

logger = logging.getLogger("youtube")

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"
YOUTUBE_FORCE_SSL_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
YOUTUBE_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"

SCOPES = [
    YOUTUBE_UPLOAD_SCOPE,
    YOUTUBE_READ_SCOPE,
    YOUTUBE_MANAGE_SCOPE,
    YOUTUBE_FORCE_SSL_SCOPE,
    YOUTUBE_ANALYTICS_SCOPE,
]


def _pkce_code_verifier(state: str) -> str:
    digest = hmac.new(
        Config.SECRET_KEY.encode("utf-8"),
        state.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def oauth_flow(redirect_uri: str, state: Optional[str] = None) -> Flow:
    kwargs = {"autogenerate_code_verifier": False}
    if state:
        kwargs["state"] = state
        kwargs["code_verifier"] = _pkce_code_verifier(state)
    flow = Flow.from_client_secrets_file(
        Config.CLIENT_SECRET_FILE,
        scopes=SCOPES,
        **kwargs,
    )
    flow.redirect_uri = redirect_uri
    return flow


def build_authorization_url(redirect_uri: str, *, select_account: bool = False) -> tuple[str, str]:
    state = secrets.token_urlsafe(32)
    flow = oauth_flow(redirect_uri, state=state)
    url, returned_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent select_account" if select_account else "consent",
    )
    return url, returned_state


def youtube_data_service(credentials: Credentials):
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def youtube_analytics_service(credentials: Credentials):
    return build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)


def _channel_payload_from_item(item: dict) -> dict:
    snippet = item.get("snippet", {}) or {}
    stats = item.get("statistics", {}) or {}
    thumbnails = snippet.get("thumbnails", {}) or {}
    thumb = (thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}).get("url")
    return {
        "youtube_channel_id": item["id"],
        "title": snippet.get("title") or "YouTube Channel",
        "custom_url": snippet.get("customUrl") or "",
        "description": snippet.get("description") or "",
        "thumbnail_url": thumb or "",
        "subscriber_count": int(stats.get("subscriberCount", 0) or 0),
        "view_count": int(stats.get("viewCount", 0) or 0),
        "video_count": int(stats.get("videoCount", 0) or 0),
        "uploads_playlist_id": item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads") or "",
    }


def _channel_payloads(service) -> list[dict]:
    payloads: list[dict] = []
    seen: set[str] = set()
    page_token: str | None = None
    while True:
        kwargs = {
            "part": "snippet,statistics,status,contentDetails",
            "mine": True,
            "maxResults": 50,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.channels().list(**kwargs).execute()
        for item in response.get("items", []) or []:
            channel_id = str(item.get("id") or "").strip()
            if not channel_id or channel_id in seen:
                continue
            seen.add(channel_id)
            payloads.append(_channel_payload_from_item(item))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    if not payloads:
        raise RuntimeError("No YouTube channel is available for this Google account")
    return payloads


def _channel_payload(service) -> dict:
    return _channel_payloads(service)[0]


def _channel_payload_by_id(service, youtube_channel_id: str) -> dict:
    response = service.channels().list(
        part="snippet,statistics,status,contentDetails",
        id=youtube_channel_id,
        maxResults=1,
    ).execute()
    items = response.get("items", []) or []
    if not items:
        raise RuntimeError("This OAuth credential no longer has access to the selected YouTube channel")
    return _channel_payload_from_item(items[0])


def connect_channels(credentials: Credentials, label: str = "") -> list[YouTubeChannel]:
    service = youtube_data_service(credentials)
    payloads = _channel_payloads(service)
    token_encrypted = encrypt_secret(credentials.to_json())
    connected: list[YouTubeChannel] = []
    single_label = (label or "").strip()[:120] if len(payloads) == 1 else ""

    with SessionLocal() as db:
        for payload in payloads:
            channel_values = {k: v for k, v in payload.items() if k != "uploads_playlist_id"}
            channel = db.query(YouTubeChannel).filter_by(
                youtube_channel_id=payload["youtube_channel_id"]
            ).one_or_none()
            if channel is None:
                channel = YouTubeChannel(
                    label=(single_label or payload["title"]).strip()[:120],
                    token_encrypted=token_encrypted,
                    default_hashtags=Config.DEFAULT_HASHTAGS,
                    default_privacy="public",
                    **channel_values,
                )
                db.add(channel)
            else:
                if single_label:
                    channel.label = single_label
                channel.token_encrypted = token_encrypted
                channel.title = payload["title"]
                channel.custom_url = payload["custom_url"]
                channel.description = payload["description"]
                channel.thumbnail_url = payload["thumbnail_url"]
                channel.subscriber_count = payload["subscriber_count"]
                channel.view_count = payload["view_count"]
                channel.video_count = payload["video_count"]
                channel.is_active = True
            channel.last_synced_at = datetime.utcnow()
            db.flush()
            connected.append(channel)

        db.commit()
        for channel in connected:
            db.refresh(channel)
        return connected


def connect_channel(credentials: Credentials, label: str) -> YouTubeChannel:
    return connect_channels(credentials, label)[0]


def discover_related_channels(channel_id: int) -> list[YouTubeChannel]:
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")
        credentials = credentials_for_channel(db, channel)
        db.commit()
    return connect_channels(credentials)



def credentials_for_channel(db, channel: YouTubeChannel) -> Credentials:
    info = json.loads(decrypt_secret(channel.token_encrypted))
    stored_scopes = info.get("scopes") or SCOPES
    creds = Credentials.from_authorized_user_info(info, stored_scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        channel.token_encrypted = encrypt_secret(creds.to_json())
        channel.updated_at = datetime.utcnow()
        db.flush()
    if not creds.valid:
        raise RuntimeError("YouTube authorization is no longer valid; reconnect this channel")
    return creds


def granted_scopes(credentials: Credentials) -> set[str]:
    scopes = set(credentials.scopes or [])
    scopes.update(credentials.granted_scopes or [])
    return scopes


def channel_authorization_state(channel_id: int) -> dict:
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")
        creds = credentials_for_channel(db, channel)
        scopes = granted_scopes(creds)
        return {
            "upload": YOUTUBE_UPLOAD_SCOPE in scopes or YOUTUBE_MANAGE_SCOPE in scopes,
            "read": YOUTUBE_READ_SCOPE in scopes or YOUTUBE_MANAGE_SCOPE in scopes,
            "manage": YOUTUBE_MANAGE_SCOPE in scopes,
            "force_ssl": YOUTUBE_FORCE_SSL_SCOPE in scopes,
            "analytics": YOUTUBE_ANALYTICS_SCOPE in scopes,
            "scopes": sorted(scopes),
        }


def refresh_channel(channel_id: int) -> YouTubeChannel:
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")
        creds = credentials_for_channel(db, channel)
        payload = _channel_payload_by_id(youtube_data_service(creds), channel.youtube_channel_id)
        channel.title = payload["title"]
        channel.custom_url = payload["custom_url"]
        channel.description = payload["description"]
        channel.thumbnail_url = payload["thumbnail_url"]
        channel.subscriber_count = payload["subscriber_count"]
        channel.view_count = payload["view_count"]
        channel.video_count = payload["video_count"]
        channel.last_synced_at = datetime.utcnow()
        db.commit()
        db.refresh(channel)
        return channel



def _manager_service(channel_id: int):
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")
        creds = credentials_for_channel(db, channel)
        expected_channel_id = channel.youtube_channel_id
        channel_title = channel.title
        db.commit()
    return youtube_data_service(creds), expected_channel_id, channel_title



def _owned_channel_resource(service, expected_channel_id: str, part: str) -> dict:
    response = service.channels().list(part=part, id=expected_channel_id).execute()
    items = response.get("items", [])
    if not items or items[0].get("id") != expected_channel_id:
        raise PermissionError("Channel does not belong to this connection")
    return items[0]


def get_channel_studio_data(channel_id: int) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    item = _owned_channel_resource(
        service,
        expected_channel_id,
        "snippet,brandingSettings,status,localizations,statistics",
    )
    snippet = item.get("snippet", {}) or {}
    branding = item.get("brandingSettings", {}) or {}
    channel_settings = branding.get("channel", {}) or {}
    image_settings = branding.get("image", {}) or {}
    status = item.get("status", {}) or {}
    thumbnails = snippet.get("thumbnails", {}) or {}
    thumb = (thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}).get("url", "")
    return {
        "id": expected_channel_id,
        "title": snippet.get("title") or "",
        "custom_url": snippet.get("customUrl") or "",
        "description": channel_settings.get("description", snippet.get("description") or ""),
        "thumbnail_url": thumb,
        "country": channel_settings.get("country") or snippet.get("country") or "",
        "default_language": channel_settings.get("defaultLanguage") or snippet.get("defaultLanguage") or "",
        "keywords": channel_settings.get("keywords") or "",
        "tracking_analytics_id": channel_settings.get("trackingAnalyticsAccountId") or "",
        "unsubscribed_trailer": channel_settings.get("unsubscribedTrailer") or "",
        "banner_url": image_settings.get("bannerExternalUrl") or "",
        "localizations": item.get("localizations") or {},
        "status": status,
        "statistics": item.get("statistics") or {},
    }


def _branding_update_payload(service, expected_channel_id: str) -> dict:
    item = _owned_channel_resource(service, expected_channel_id, "brandingSettings")
    branding = item.get("brandingSettings", {}) or {}
    current_channel = branding.get("channel", {}) or {}
    allowed = (
        "country",
        "description",
        "defaultLanguage",
        "keywords",
        "trackingAnalyticsAccountId",
        "unsubscribedTrailer",
    )
    channel_settings = {key: current_channel[key] for key in allowed if current_channel.get(key) not in (None, "")}
    payload = {"channel": channel_settings}
    banner_url = (branding.get("image", {}) or {}).get("bannerExternalUrl")
    if banner_url:
        payload["image"] = {"bannerExternalUrl": banner_url}
    return payload


def update_channel_branding(
    channel_id: int,
    *,
    description: str = "",
    keywords: str = "",
    country: str = "",
    default_language: str = "",
    tracking_analytics_id: str = "",
    unsubscribed_trailer: str = "",
) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    branding = _branding_update_payload(service, expected_channel_id)
    settings = branding.setdefault("channel", {})
    values = {
        "description": (description or "").strip()[:1000],
        "keywords": (keywords or "").strip()[:500],
        "country": (country or "").strip().upper()[:2],
        "defaultLanguage": (default_language or "").strip()[:20],
        "trackingAnalyticsAccountId": (tracking_analytics_id or "").strip()[:120],
        "unsubscribedTrailer": (unsubscribed_trailer or "").strip()[:32],
    }
    for key, value in values.items():
        if value:
            settings[key] = value
        else:
            settings.pop(key, None)
    result = service.channels().update(
        part="brandingSettings",
        body={"id": expected_channel_id, "brandingSettings": branding},
    ).execute()
    refresh_channel(channel_id)
    return result


def _image_dimensions(content: bytes, mime_type: str) -> tuple[int, int] | tuple[None, None]:
    if mime_type == "image/png" and len(content) >= 24 and content[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
    if mime_type == "image/jpeg" and content[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(content):
            if content[i] != 0xFF:
                i += 1
                continue
            marker = content[i + 1]
            i += 2
            if marker in {0xD8, 0xD9}:
                continue
            if i + 2 > len(content):
                break
            length = int.from_bytes(content[i:i + 2], "big")
            if length < 2 or i + length > len(content):
                break
            if marker in {0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF} and length >= 7:
                return int.from_bytes(content[i + 5:i + 7], "big"), int.from_bytes(content[i + 3:i + 5], "big")
            i += length
    return None, None


def update_channel_audience(channel_id: int, made_for_kids: bool) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    return service.channels().update(
        part="status",
        body={
            "id": expected_channel_id,
            "status": {"selfDeclaredMadeForKids": bool(made_for_kids)},
        },
    ).execute()


def upload_channel_banner(channel_id: int, content: bytes, mime_type: str) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    if mime_type not in {"image/jpeg", "image/png", "application/octet-stream"}:
        raise ValueError("Banner must be JPEG or PNG")
    if not content:
        raise ValueError("Banner file is empty")
    if len(content) > 6 * 1024 * 1024:
        raise ValueError("Banner is larger than YouTube's 6MB limit")
    detected_type = mime_type
    if mime_type == "application/octet-stream":
        if content[:8] == b"\x89PNG\r\n\x1a\n":
            detected_type = "image/png"
        elif content[:2] == b"\xff\xd8":
            detected_type = "image/jpeg"
    width, height = _image_dimensions(content, detected_type)
    if width and height:
        if width < 2048 or height < 1152:
            raise ValueError(f"Banner is {width}×{height}; YouTube requires at least 2048×1152")
        if abs((width / height) - (16 / 9)) > 0.03:
            raise ValueError("Banner must use a 16:9 aspect ratio")
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=detected_type, resumable=False)
    uploaded = service.channelBanners().insert(media_body=media).execute()
    banner_url = uploaded.get("url")
    if not banner_url:
        raise RuntimeError("YouTube did not return a banner URL")
    branding = _branding_update_payload(service, expected_channel_id)
    branding["image"] = {"bannerExternalUrl": banner_url}
    result = service.channels().update(
        part="brandingSettings",
        body={"id": expected_channel_id, "brandingSettings": branding},
    ).execute()
    return result


def set_channel_watermark(
    channel_id: int,
    content: bytes,
    mime_type: str,
    *,
    timing_type: str = "offsetFromStart",
    offset_ms: int = 0,
    duration_ms: int | None = None,
    target_channel_id: str = "",
) -> None:
    service, expected_channel_id, _ = _manager_service(channel_id)
    if mime_type not in {"image/jpeg", "image/png", "application/octet-stream"}:
        raise ValueError("Watermark must be JPEG or PNG")
    if not content:
        raise ValueError("Watermark file is empty")
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("Watermark is larger than YouTube's 10MB limit")
    if timing_type not in {"offsetFromStart", "offsetFromEnd"}:
        timing_type = "offsetFromStart"
    offset_ms = max(0, int(offset_ms or 0))
    if timing_type == "offsetFromEnd":
        offset_ms = max(1000, offset_ms)
    timing = {"type": timing_type, "offsetMs": offset_ms}
    if duration_ms is not None and int(duration_ms) > 0:
        duration_ms = max(1000, int(duration_ms))
        if timing_type == "offsetFromEnd":
            duration_ms = min(duration_ms, offset_ms)
        timing["durationMs"] = duration_ms
    body = {"timing": timing}
    target = (target_channel_id or "").strip()
    if target:
        body["targetChannelId"] = target[:64]
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
    service.watermarks().set(channelId=expected_channel_id, body=body, media_body=media).execute()


def remove_channel_watermark(channel_id: int) -> None:
    service, expected_channel_id, _ = _manager_service(channel_id)
    service.watermarks().unset(channelId=expected_channel_id).execute()


def update_channel_localization(channel_id: int, language: str, title: str, description: str) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    item = _owned_channel_resource(service, expected_channel_id, "brandingSettings,localizations")
    default_language = (item.get("brandingSettings", {}).get("channel", {}) or {}).get("defaultLanguage")
    if not default_language:
        raise ValueError("Set a default language before adding localizations")
    language = (language or "").strip()
    if not language:
        raise ValueError("Localization language is required")
    localizations = dict(item.get("localizations") or {})
    localizations[language] = {
        "title": (title or "").strip()[:100],
        "description": (description or "").strip()[:1000],
    }
    if not localizations[language]["title"]:
        raise ValueError("Localized title is required")
    return service.channels().update(
        part="localizations",
        body={"id": expected_channel_id, "localizations": localizations},
    ).execute()


def delete_channel_localization(channel_id: int, language: str) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    item = _owned_channel_resource(service, expected_channel_id, "localizations")
    localizations = dict(item.get("localizations") or {})
    localizations.pop((language or "").strip(), None)
    return service.channels().update(
        part="localizations",
        body={"id": expected_channel_id, "localizations": localizations},
    ).execute()


def list_channel_sections(channel_id: int) -> list[dict]:
    service, expected_channel_id, _ = _manager_service(channel_id)
    response = service.channelSections().list(
        part="snippet,contentDetails",
        channelId=expected_channel_id,
    ).execute()
    sections = []
    for item in response.get("items", []):
        snippet = item.get("snippet", {}) or {}
        details = item.get("contentDetails", {}) or {}
        sections.append({
            "id": item.get("id") or "",
            "type": snippet.get("type") or "",
            "title": snippet.get("title") or "",
            "position": int(snippet.get("position", 0) or 0),
            "playlists": details.get("playlists") or [],
            "channels": details.get("channels") or [],
        })
    sections.sort(key=lambda row: row["position"])
    return sections


def _section_body(section_type: str, title: str, position: int, playlist_ids: list[str], channel_ids: list[str]) -> dict:
    allowed_types = {
        "allPlaylists", "completedEvents", "liveEvents", "multipleChannels", "multiplePlaylists",
        "popularUploads", "recentUploads", "singlePlaylist", "subscriptions", "upcomingEvents",
    }
    if section_type not in allowed_types:
        raise ValueError("Invalid channel section type")
    snippet = {"type": section_type, "position": max(0, int(position or 0))}
    if section_type in {"multiplePlaylists", "multipleChannels"}:
        clean_title = (title or "").strip()[:100]
        if not clean_title:
            raise ValueError("This section type requires a title")
        snippet["title"] = clean_title
    body = {"snippet": snippet}
    if section_type == "singlePlaylist":
        if len(playlist_ids) != 1:
            raise ValueError("Single playlist section requires exactly one playlist")
        body["contentDetails"] = {"playlists": playlist_ids}
    elif section_type == "multiplePlaylists":
        if not playlist_ids:
            raise ValueError("Choose at least one playlist")
        body["contentDetails"] = {"playlists": playlist_ids}
    elif section_type == "multipleChannels":
        if not channel_ids:
            raise ValueError("Enter at least one channel ID")
        body["contentDetails"] = {"channels": channel_ids}
    return body


def create_channel_section(
    channel_id: int,
    section_type: str,
    title: str = "",
    position: int = 0,
    playlist_ids: list[str] | None = None,
    channel_ids: list[str] | None = None,
) -> dict:
    service, _, _ = _manager_service(channel_id)
    body = _section_body(section_type, title, position, playlist_ids or [], channel_ids or [])
    return service.channelSections().insert(part="snippet,contentDetails", body=body).execute()


def update_channel_section(
    channel_id: int,
    section_id: str,
    section_type: str,
    title: str = "",
    position: int = 0,
    playlist_ids: list[str] | None = None,
    channel_ids: list[str] | None = None,
) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    existing = service.channelSections().list(part="snippet", id=section_id).execute().get("items", [])
    if not existing or existing[0].get("snippet", {}).get("channelId") != expected_channel_id:
        raise PermissionError("Channel section does not belong to this channel")
    body = _section_body(section_type, title, position, playlist_ids or [], channel_ids or [])
    body["id"] = section_id
    return service.channelSections().update(part="snippet,contentDetails", body=body).execute()


def delete_channel_section(channel_id: int, section_id: str) -> None:
    service, expected_channel_id, _ = _manager_service(channel_id)
    existing = service.channelSections().list(part="snippet", id=section_id).execute().get("items", [])
    if not existing or existing[0].get("snippet", {}).get("channelId") != expected_channel_id:
        raise PermissionError("Channel section does not belong to this channel")
    service.channelSections().delete(id=section_id).execute()



def _owned_video(service, expected_channel_id: str, video_id: str, part: str = "snippet,status,statistics,contentDetails") -> dict:
    # Ownership validation always needs snippet.channelId. Callers may only
    # need status/statistics, but omitting snippet would create a false
    # "does not belong" result.
    requested_parts = [item.strip() for item in (part or "").split(",") if item.strip()]
    if "snippet" not in requested_parts:
        requested_parts.insert(0, "snippet")
    response = service.videos().list(part=",".join(dict.fromkeys(requested_parts)), id=video_id).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError("Video not found")
    item = items[0]
    if item.get("snippet", {}).get("channelId") != expected_channel_id:
        raise PermissionError("This video does not belong to the selected channel")
    return item


def _manager_thumbnail(snippet: dict) -> str:
    thumbs = snippet.get("thumbnails", {}) or {}
    for key in ("maxres", "standard", "high", "medium", "default"):
        url = (thumbs.get(key) or {}).get("url")
        if url:
            return url
    return ""


def _manager_duration(value: str | None) -> int:
    import re
    match = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (int(x or 0) for x in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _manager_video_payload(item: dict) -> dict:
    snippet = item.get("snippet", {}) or {}
    status = item.get("status", {}) or {}
    statistics = item.get("statistics", {}) or {}
    details = item.get("contentDetails", {}) or {}
    return {
        "video_id": item.get("id", ""),
        "title": snippet.get("title") or "Untitled",
        "description": snippet.get("description") or "",
        "tags": snippet.get("tags") or [],
        "category_id": snippet.get("categoryId") or "22",
        "published_at": snippet.get("publishedAt") or "",
        "thumbnail_url": _manager_thumbnail(snippet),
        "channel_id": snippet.get("channelId") or "",
        "channel_title": snippet.get("channelTitle") or "",
        "privacy": status.get("privacyStatus") or "private",
        "embeddable": bool(status.get("embeddable", True)),
        "license": status.get("license") or "youtube",
        "made_for_kids": bool(status.get("madeForKids", status.get("selfDeclaredMadeForKids", False))),
        "self_declared_made_for_kids": status.get("selfDeclaredMadeForKids"),
        "upload_status": status.get("uploadStatus") or "",
        "views": int(statistics.get("viewCount", 0) or 0),
        "likes": int(statistics.get("likeCount", 0) or 0),
        "comments": int(statistics.get("commentCount", 0) or 0),
        "duration_seconds": _manager_duration(details.get("duration")),
        "caption": details.get("caption") == "true",
        "definition": details.get("definition") or "",
        "url": f"https://youtube.com/watch?v={item.get('id', '')}",
    }


def list_channel_videos(channel_id: int, page_token: str = "", max_results: int = 24) -> dict:
    service, expected_channel_id, channel_title = _manager_service(channel_id)
    channel_response = service.channels().list(part="contentDetails", id=expected_channel_id).execute()
    channel_items = channel_response.get("items", [])
    if not channel_items:
        raise RuntimeError("YouTube channel is no longer available")
    uploads = channel_items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not uploads:
        return {"items": [], "next_page_token": "", "channel_title": channel_title}

    kwargs = {
        "part": "contentDetails",
        "playlistId": uploads,
        "maxResults": max(1, min(50, int(max_results or 24))),
    }
    if page_token:
        kwargs["pageToken"] = page_token
    page = service.playlistItems().list(**kwargs).execute()
    ids = [
        item.get("contentDetails", {}).get("videoId")
        for item in page.get("items", [])
        if item.get("contentDetails", {}).get("videoId")
    ]
    by_id = {}
    if ids:
        response = service.videos().list(
            part="snippet,status,statistics,contentDetails",
            id=",".join(ids),
            maxResults=len(ids),
        ).execute()
        by_id = {item["id"]: _manager_video_payload(item) for item in response.get("items", [])}
    return {
        "items": [by_id[video_id] for video_id in ids if video_id in by_id],
        "next_page_token": page.get("nextPageToken") or "",
        "channel_title": channel_title,
    }


def get_video_manager_data(channel_id: int, video_id: str) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    return _manager_video_payload(_owned_video(service, expected_channel_id, video_id))


def update_video_metadata(
    channel_id: int,
    video_id: str,
    *,
    title: str,
    description: str,
    tags: list[str] | None,
    privacy: str,
    category_id: str = "22",
    made_for_kids: bool = False,
    embeddable: bool = True,
) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    current = _owned_video(service, expected_channel_id, video_id, part="snippet,status")
    snippet = current.get("snippet", {}) or {}
    status = current.get("status", {}) or {}

    body = {
        "id": video_id,
        "snippet": {
            "title": (title or snippet.get("title") or "Untitled").strip()[:100],
            "description": (description or "").strip()[:5000],
            "tags": [tag.strip() for tag in (tags or []) if tag.strip()][:500],
            "categoryId": str(category_id or snippet.get("categoryId") or "22"),
        },
        "status": {
            "privacyStatus": privacy if privacy in {"public", "unlisted", "private"} else status.get("privacyStatus", "private"),
            "embeddable": bool(embeddable),
            "license": status.get("license") or "youtube",
            "selfDeclaredMadeForKids": bool(made_for_kids),
            "publicStatsViewable": bool(status.get("publicStatsViewable", True)),
        },
    }
    if snippet.get("defaultLanguage"):
        body["snippet"]["defaultLanguage"] = snippet["defaultLanguage"]
    if snippet.get("defaultAudioLanguage"):
        body["snippet"]["defaultAudioLanguage"] = snippet["defaultAudioLanguage"]

    updated = service.videos().update(part="snippet,status", body=body).execute()
    return _manager_video_payload(updated)


def delete_video(channel_id: int, video_id: str) -> None:
    service, expected_channel_id, _ = _manager_service(channel_id)
    _owned_video(service, expected_channel_id, video_id, part="snippet")
    service.videos().delete(id=video_id).execute()


def set_video_thumbnail(channel_id: int, video_id: str, content: bytes, mime_type: str) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    _owned_video(service, expected_channel_id, video_id, part="snippet")
    if mime_type not in {"image/jpeg", "image/png", "application/octet-stream"}:
        raise ValueError("Thumbnail must be JPEG or PNG")
    if not content:
        raise ValueError("Thumbnail file is empty")
    if len(content) > 50 * 1024 * 1024:
        raise ValueError("Thumbnail is larger than 50MB")
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
    return service.thumbnails().set(videoId=video_id, media_body=media).execute()


def list_channel_playlists(channel_id: int) -> list[dict]:
    service, expected_channel_id, _ = _manager_service(channel_id)
    response = service.playlists().list(
        part="snippet,status,contentDetails",
        channelId=expected_channel_id,
        maxResults=50,
    ).execute()
    result = []
    for item in response.get("items", []):
        snippet = item.get("snippet", {}) or {}
        result.append({
            "playlist_id": item.get("id", ""),
            "title": snippet.get("title") or "Untitled",
            "description": snippet.get("description") or "",
            "thumbnail_url": _manager_thumbnail(snippet),
            "privacy": item.get("status", {}).get("privacyStatus") or "private",
            "item_count": int(item.get("contentDetails", {}).get("itemCount", 0) or 0),
        })
    return result


def create_playlist(channel_id: int, title: str, description: str = "", privacy: str = "private") -> dict:
    service, _, _ = _manager_service(channel_id)
    body = {
        "snippet": {"title": (title or "New Playlist").strip()[:150], "description": (description or "").strip()[:5000]},
        "status": {"privacyStatus": privacy if privacy in {"public", "unlisted", "private"} else "private"},
    }
    return service.playlists().insert(part="snippet,status", body=body).execute()


def update_playlist(channel_id: int, playlist_id: str, title: str, description: str, privacy: str) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    response = service.playlists().list(part="snippet,status", id=playlist_id).execute()
    items = response.get("items", [])
    if not items or items[0].get("snippet", {}).get("channelId") != expected_channel_id:
        raise PermissionError("Playlist does not belong to this channel")
    body = {
        "id": playlist_id,
        "snippet": {"title": (title or "Playlist").strip()[:150], "description": (description or "").strip()[:5000]},
        "status": {"privacyStatus": privacy if privacy in {"public", "unlisted", "private"} else "private"},
    }
    return service.playlists().update(part="snippet,status", body=body).execute()


def delete_playlist(channel_id: int, playlist_id: str) -> None:
    service, expected_channel_id, _ = _manager_service(channel_id)
    response = service.playlists().list(part="snippet", id=playlist_id).execute()
    items = response.get("items", [])
    if not items or items[0].get("snippet", {}).get("channelId") != expected_channel_id:
        raise PermissionError("Playlist does not belong to this channel")
    service.playlists().delete(id=playlist_id).execute()


def add_video_to_playlist(channel_id: int, playlist_id: str, video_id: str) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    _owned_video(service, expected_channel_id, video_id, part="snippet")
    playlist = service.playlists().list(part="snippet", id=playlist_id).execute().get("items", [])
    if not playlist or playlist[0].get("snippet", {}).get("channelId") != expected_channel_id:
        raise PermissionError("Playlist does not belong to this channel")
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }
    }
    return service.playlistItems().insert(part="snippet", body=body).execute()


def list_video_comments(channel_id: int, video_id: str, moderation_status: str = "published") -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    _owned_video(service, expected_channel_id, video_id, part="snippet")
    kwargs = {
        "part": "snippet,replies",
        "videoId": video_id,
        "maxResults": 50,
        "textFormat": "plainText",
        "order": "time",
    }
    if moderation_status in {"published", "heldForReview"}:
        kwargs["moderationStatus"] = moderation_status
    response = service.commentThreads().list(**kwargs).execute()
    items = []
    for thread in response.get("items", []):
        top = thread.get("snippet", {}).get("topLevelComment", {}) or {}
        snippet = top.get("snippet", {}) or {}
        items.append({
            "thread_id": thread.get("id", ""),
            "comment_id": top.get("id", ""),
            "author": snippet.get("authorDisplayName") or "",
            "author_avatar": snippet.get("authorProfileImageUrl") or "",
            "text": snippet.get("textDisplay") or snippet.get("textOriginal") or "",
            "like_count": int(snippet.get("likeCount", 0) or 0),
            "published_at": snippet.get("publishedAt") or "",
            "updated_at": snippet.get("updatedAt") or "",
            "reply_count": int(thread.get("snippet", {}).get("totalReplyCount", 0) or 0),
        })
    return {"items": items, "next_page_token": response.get("nextPageToken") or ""}


def reply_to_comment(channel_id: int, video_id: str, parent_comment_id: str, text: str) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    _owned_video(service, expected_channel_id, video_id, part="snippet")
    body = {"snippet": {"parentId": parent_comment_id, "textOriginal": (text or "").strip()[:10000]}}
    if not body["snippet"]["textOriginal"]:
        raise ValueError("Reply is empty")
    return service.comments().insert(part="snippet", body=body).execute()


def moderate_comment(channel_id: int, video_id: str, comment_id: str, status: str, ban_author: bool = False) -> None:
    service, expected_channel_id, _ = _manager_service(channel_id)
    _owned_video(service, expected_channel_id, video_id, part="snippet")
    if status not in {"published", "rejected"}:
        raise ValueError("Invalid moderation status")
    service.comments().setModerationStatus(
        id=comment_id,
        moderationStatus=status,
        banAuthor=bool(ban_author and status == "rejected"),
    ).execute()


def delete_comment(channel_id: int, video_id: str, comment_id: str) -> None:
    service, expected_channel_id, _ = _manager_service(channel_id)
    _owned_video(service, expected_channel_id, video_id, part="snippet")
    service.comments().delete(id=comment_id).execute()


def list_video_captions(channel_id: int, video_id: str) -> list[dict]:
    service, expected_channel_id, _ = _manager_service(channel_id)
    _owned_video(service, expected_channel_id, video_id, part="snippet")
    response = service.captions().list(part="snippet", videoId=video_id).execute()
    result = []
    for item in response.get("items", []):
        snippet = item.get("snippet", {}) or {}
        result.append({
            "caption_id": item.get("id", ""),
            "language": snippet.get("language") or "",
            "name": snippet.get("name") or "",
            "track_kind": snippet.get("trackKind") or "",
            "is_draft": bool(snippet.get("isDraft", False)),
            "status": snippet.get("status") or "",
            "last_updated": snippet.get("lastUpdated") or "",
        })
    return result


def upload_caption(
    channel_id: int,
    video_id: str,
    *,
    content: bytes,
    language: str,
    name: str,
    is_draft: bool = False,
) -> dict:
    service, expected_channel_id, _ = _manager_service(channel_id)
    _owned_video(service, expected_channel_id, video_id, part="snippet")
    if not content:
        raise ValueError("Caption file is empty")
    if len(content) > 100 * 1024 * 1024:
        raise ValueError("Caption file is larger than 100MB")
    body = {
        "snippet": {
            "videoId": video_id,
            "language": (language or "").strip()[:20],
            "name": (name or "").strip()[:150],
            "isDraft": bool(is_draft),
        }
    }
    if not body["snippet"]["language"] or not body["snippet"]["name"]:
        raise ValueError("Caption language and name are required")
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/octet-stream", resumable=False)
    return service.captions().insert(part="snippet", body=body, media_body=media).execute()


def delete_caption(channel_id: int, video_id: str, caption_id: str) -> None:
    service, expected_channel_id, _ = _manager_service(channel_id)
    _owned_video(service, expected_channel_id, video_id, part="snippet")
    service.captions().delete(id=caption_id).execute()



def publishing_preflight_capability(channel_id: int) -> dict:
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")
        creds = credentials_for_channel(db, channel)
        scopes = granted_scopes(creds)

    upload_ok = YOUTUBE_UPLOAD_SCOPE in scopes or YOUTUBE_MANAGE_SCOPE in scopes or YOUTUBE_FORCE_SSL_SCOPE in scopes
    manage_ok = YOUTUBE_MANAGE_SCOPE in scopes or YOUTUBE_FORCE_SSL_SCOPE in scopes
    return {
        "ok": bool(upload_ok and manage_ok),
        "upload": bool(upload_ok),
        "manage": bool(manage_ok),
        "scopes": sorted(scopes),
    }


def wait_for_video_preflight(
    channel_id: int,
    video_id: str,
    *,
    timeout_seconds: int = 600,
    poll_seconds: int = 10,
) -> dict:
    """Wait for API-visible YouTube processing/rejection signals."""
    service, expected_channel_id, _ = _manager_service(channel_id)
    deadline = time.monotonic() + max(30, int(timeout_seconds))
    last_payload: dict = {}

    while time.monotonic() < deadline:
        response = service.videos().list(
            part="snippet,status,processingDetails",
            id=video_id,
            maxResults=1,
        ).execute()
        items = response.get("items", [])
        if not items:
            return {
                "ok": False,
                "blocked": True,
                "copyright_signal": False,
                "reason": "video_missing_after_upload",
                "detail": "Uploaded video is no longer available on YouTube.",
            }

        item = items[0]
        if item.get("snippet", {}).get("channelId") != expected_channel_id:
            raise PermissionError("Video does not belong to this channel")

        status = item.get("status", {}) or {}
        processing = item.get("processingDetails", {}) or {}
        processing_status = processing.get("processingStatus") or ""
        rejection_reason = status.get("rejectionReason") or ""
        upload_status = status.get("uploadStatus") or ""
        failure_reason = processing.get("processingFailureReason") or ""
        upload_failure_reason = status.get("failureReason") or ""

        last_payload = {
            "processing_status": processing_status,
            "upload_status": upload_status,
            "rejection_reason": rejection_reason,
            "failure_reason": failure_reason,
            "upload_failure_reason": upload_failure_reason,
        }
        reason_text = " ".join(
            str(x).lower()
            for x in [rejection_reason, failure_reason, upload_failure_reason, upload_status]
            if x
        )
        copyright_signal = any(
            marker in reason_text
            for marker in ("copyright", "claim")
        )

        terminal_upload_failure = upload_status in {"rejected", "failed", "deleted"}
        terminal_processing_failure = processing_status in {"failed", "terminated"}
        if rejection_reason or upload_failure_reason or terminal_upload_failure or terminal_processing_failure:
            return {
                "ok": False,
                "blocked": True,
                "copyright_signal": copyright_signal,
                "reason": (
                    rejection_reason
                    or upload_failure_reason
                    or failure_reason
                    or upload_status
                    or processing_status
                ),
                "detail": last_payload,
            }

        if processing_status == "succeeded":
            return {
                "ok": True,
                "blocked": False,
                "copyright_signal": False,
                "reason": "",
                "detail": last_payload,
            }

        time.sleep(max(2, int(poll_seconds)))

    return {
        "ok": False,
        "blocked": True,
        "copyright_signal": False,
        "reason": "copyright_check_timeout",
        "detail": last_payload,
    }


def publish_checked_video(
    channel_id: int,
    video_id: str,
    *,
    privacy: str,
    content_type: str = "short",
) -> dict:
    target_privacy = privacy if privacy in {"public", "unlisted", "private"} else "public"
    service, expected_channel_id, _ = _manager_service(channel_id)
    current = _owned_video(service, expected_channel_id, video_id, part="status")
    status = current.get("status", {}) or {}
    body = {
        "id": video_id,
        "status": {
            "privacyStatus": target_privacy,
            "selfDeclaredMadeForKids": bool(status.get("selfDeclaredMadeForKids", False)),
        },
    }
    for key in ("embeddable", "license", "publicStatsViewable"):
        if key in status:
            body["status"][key] = status[key]
    updated = service.videos().update(part="status", body=body).execute()
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if channel:
            channel.last_used_at = datetime.utcnow()
            channel.video_count = max(0, channel.video_count + 1)
            db.commit()
    return {
        "video_id": video_id,
        "video_url": (
            f"https://youtube.com/watch?v={video_id}"
            if content_type == "long"
            else f"https://youtube.com/shorts/{video_id}"
        ),
        "privacy": updated.get("status", {}).get("privacyStatus") or target_privacy,
    }


def discard_preflight_video(channel_id: int, video_id: str) -> None:
    delete_video(channel_id, video_id)


def upload_to_youtube(
    file_path: str,
    channel_id: int,
    title: str,
    hashtags: str = "",
    description: str = "",
    privacy: Optional[str] = None,
    *,
    force_private: bool = False,
    content_type: str = "short",
    tags: Optional[list[str]] = None,
    category_id: str = "22",
    made_for_kids: bool = False,
    embeddable: bool = True,
    license_name: str = "youtube",
    notify_subscribers: bool = True,
    default_language: str = "",
    audio_language: str = "",
) -> dict:
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")
        if not channel.is_active:
            raise RuntimeError("Channel is disabled")

        creds = credentials_for_channel(db, channel)
        service = youtube_data_service(creds)
        requested_privacy = privacy or channel.default_privacy
        if requested_privacy not in {"public", "unlisted", "private"}:
            requested_privacy = "public"
        effective_privacy = "private" if force_private else requested_privacy

        is_long = content_type == "long"
        final_title = (title or ("YouTube Video" if is_long else "YouTube Shorts")).strip()
        full_title = final_title[:100] if is_long else f"{final_title} {hashtags}".strip()[:100]
        description_parts = [description.strip(), hashtags.strip()]
        if not is_long:
            description_parts.append("#Shorts")
        full_description = "\n\n".join(x for x in description_parts if x).strip()[:5000]

        clean_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
        body = {
            "snippet": {
                "title": full_title,
                "description": full_description,
                "categoryId": str(category_id or "22"),
            },
            "status": {
                "privacyStatus": effective_privacy,
                "selfDeclaredMadeForKids": bool(made_for_kids),
                "embeddable": bool(embeddable),
                "license": license_name if license_name in {"youtube", "creativeCommon"} else "youtube",
                "publicStatsViewable": True,
            },
        }
        if clean_tags:
            body["snippet"]["tags"] = clean_tags[:500]
        if default_language:
            body["snippet"]["defaultLanguage"] = default_language.strip()[:20]
        if audio_language:
            body["snippet"]["defaultAudioLanguage"] = audio_language.strip()[:20]

        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type or not mime_type.startswith("video/"):
            raise RuntimeError(f"Downloaded file is not a supported video media type: {mime_type or 'unknown'}")
        media = MediaFileUpload(
            file_path,
            mimetype=mime_type,
            chunksize=8 * 1024 * 1024,
            resumable=True,
        )
        request = service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
            notifySubscribers=bool(notify_subscribers),
        )
        response = None
        while response is None:
            _, response = request.next_chunk()

        video_id = response.get("id")
        if not video_id:
            raise RuntimeError("YouTube API did not return a video ID")

        channel.last_used_at = datetime.utcnow()
        if not force_private:
            channel.video_count = max(0, channel.video_count + 1)
        db.commit()

        return {
            "success": True,
            "video_id": video_id,
            "video_url": (
                f"https://youtube.com/watch?v={video_id}"
                if is_long
                else f"https://youtube.com/shorts/{video_id}"
            ),
            "channel_id": channel.id,
            "channel_title": channel.title,
            "privacy": effective_privacy,
            "requested_privacy": requested_privacy,
        }