from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import Config
from db import SessionLocal
from models import YouTubeChannel
from security import decrypt_secret, encrypt_secret

logger = logging.getLogger("youtube")

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"

SCOPES = [
    YOUTUBE_UPLOAD_SCOPE,
    YOUTUBE_READ_SCOPE,
    YOUTUBE_ANALYTICS_SCOPE,
]


def oauth_flow(redirect_uri: str, state: Optional[str] = None) -> Flow:
    flow = Flow.from_client_secrets_file(Config.CLIENT_SECRET_FILE, scopes=SCOPES, state=state)
    flow.redirect_uri = redirect_uri
    return flow


def build_authorization_url(redirect_uri: str) -> tuple[str, str]:
    flow = oauth_flow(redirect_uri)
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url, state


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
            "upload": YOUTUBE_UPLOAD_SCOPE in scopes,
            "read": YOUTUBE_READ_SCOPE in scopes,
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


def upload_to_youtube(
    file_path: str,
    channel_id: int,
    title: str,
    hashtags: str = "",
    description: str = "",
    privacy: Optional[str] = None,
) -> dict:
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")
        if not channel.is_active:
            raise RuntimeError("Channel is disabled")

        creds = credentials_for_channel(db, channel)
        service = youtube_data_service(creds)
        effective_privacy = privacy or channel.default_privacy
        if effective_privacy not in {"public", "unlisted", "private"}:
            effective_privacy = "public"

        final_title = (title or "YouTube Shorts").strip()
        full_title = f"{final_title} {hashtags}".strip()[:100]
        full_description = "\n\n".join(
            x for x in [description.strip(), hashtags.strip(), "#Shorts"] if x
        ).strip()

        body = {
            "snippet": {
                "title": full_title,
                "description": full_description,
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": effective_privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(file_path, chunksize=8 * 1024 * 1024, resumable=True)
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = request.next_chunk()

        video_id = response.get("id")
        if not video_id:
            raise RuntimeError("YouTube API did not return a video ID")

        channel.last_used_at = datetime.utcnow()
        channel.video_count = max(0, channel.video_count + 1)
        db.commit()

        return {
            "success": True,
            "video_id": video_id,
            "video_url": f"https://youtube.com/shorts/{video_id}",
            "channel_id": channel.id,
            "channel_title": channel.title,
            "privacy": effective_privacy,
        }
