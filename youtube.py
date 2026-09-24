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


def build_authorization_url(redirect_uri: str) -> tuple[str, str]:
    state = secrets.token_urlsafe(32)
    flow = oauth_flow(redirect_uri, state=state)
    url, returned_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url, returned_state


def youtube_data_service(credentials: Credentials):
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def youtube_analytics_service(credentials: Credentials):
    return build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)


def _channel_payload(service) -> dict:
    response = service.channels().list(part="snippet,statistics,status,contentDetails", mine=True).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError("No YouTube channel is available for this Google account")
    item = items[0]
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    thumbnails = snippet.get("thumbnails", {})
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


def connect_channel(credentials: Credentials, label: str) -> YouTubeChannel:
    service = youtube_data_service(credentials)
    payload = _channel_payload(service)
    token_encrypted = encrypt_secret(credentials.to_json())
    channel_values = {k: v for k, v in payload.items() if k != "uploads_playlist_id"}

    with SessionLocal() as db:
        channel = db.query(YouTubeChannel).filter_by(youtube_channel_id=payload["youtube_channel_id"]).one_or_none()
        if channel is None:
            channel = YouTubeChannel(
                label=(label or payload["title"]).strip()[:120],
                token_encrypted=token_encrypted,
                default_hashtags=Config.DEFAULT_HASHTAGS,
                default_privacy="public",
                **channel_values,
            )
            db.add(channel)
        else:
            channel.label = (label or channel.label).strip()[:120]
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
        db.commit()
        db.refresh(channel)
        return channel


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
        payload = _channel_payload(youtube_data_service(creds))
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
