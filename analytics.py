from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from db import SessionLocal
from models import ChannelAnalyticsCache, YouTubeChannel
from youtube import (
    YOUTUBE_ANALYTICS_SCOPE,
    credentials_for_channel,
    granted_scopes,
    youtube_analytics_service,
    youtube_data_service,
)

logger = logging.getLogger("analytics")

ALLOWED_PERIODS = {7, 28, 90, 365}
DAILY_METRICS = (
    "views,likes,comments,shares,subscribersGained,subscribersLost,"
    "estimatedMinutesWatched,averageViewDuration"
)
TOP_VIDEO_METRICS = (
    "views,likes,comments,shares,subscribersGained,"
    "estimatedMinutesWatched,averageViewDuration"
)


def normalize_period(days: int | str | None) -> int:
    try:
        value = int(days or 28)
    except (TypeError, ValueError):
        value = 28
    return value if value in ALLOWED_PERIODS else 28


def _rows_as_dict(response: dict[str, Any]) -> list[dict[str, Any]]:
    headers = [item.get("name") for item in response.get("columnHeaders", [])]
    return [dict(zip(headers, row)) for row in response.get("rows", [])]


def _safe_int(value: Any) -> int:
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _duration_seconds(value: str | None) -> int:
    if not value:
        return 0
    match = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        return 0
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _thumbnail(snippet: dict[str, Any]) -> str:
    thumbs = snippet.get("thumbnails", {})
    return (
        (thumbs.get("maxres") or {}).get("url")
        or (thumbs.get("standard") or {}).get("url")
        or (thumbs.get("high") or {}).get("url")
        or (thumbs.get("medium") or {}).get("url")
        or (thumbs.get("default") or {}).get("url")
        or ""
    )


def _video_payload(item: dict[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    details = item.get("contentDetails", {})
    status = item.get("status", {})
    return {
        "video_id": item.get("id", ""),
        "title": snippet.get("title") or "Untitled",
        "thumbnail_url": _thumbnail(snippet),
        "published_at": snippet.get("publishedAt") or "",
        "views": _safe_int(stats.get("viewCount")),
        "likes": _safe_int(stats.get("likeCount")),
        "comments": _safe_int(stats.get("commentCount")),
        "duration_seconds": _duration_seconds(details.get("duration")),
        "privacy": status.get("privacyStatus") or "",
        "url": f"https://youtube.com/watch?v={item.get('id', '')}",
    }


def _load_video_details(service, video_ids: list[str]) -> dict[str, dict[str, Any]]:
    unique_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))[:50]
    if not unique_ids:
        return {}
    response = service.videos().list(
        part="snippet,statistics,contentDetails,status",
        id=",".join(unique_ids),
        maxResults=len(unique_ids),
    ).execute()
    return {item["id"]: _video_payload(item) for item in response.get("items", [])}


def _current_channel(service) -> tuple[dict[str, Any], str]:
    response = service.channels().list(
        part="snippet,statistics,status,contentDetails",
        mine=True,
    ).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError("No YouTube channel is available for this authorization")
    item = items[0]
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads") or ""
    current = {
        "youtube_channel_id": item["id"],
        "title": snippet.get("title") or "YouTube Channel",
        "custom_url": snippet.get("customUrl") or "",
        "description": snippet.get("description") or "",
        "thumbnail_url": _thumbnail(snippet),
        "subscriber_count": _safe_int(stats.get("subscriberCount")),
        "view_count": _safe_int(stats.get("viewCount")),
        "video_count": _safe_int(stats.get("videoCount")),
    }
    return current, uploads


def _latest_upload_ids(service, uploads_playlist_id: str, limit: int = 12) -> list[str]:
    if not uploads_playlist_id:
        return []
    response = service.playlistItems().list(
        part="contentDetails",
        playlistId=uploads_playlist_id,
        maxResults=min(50, limit),
    ).execute()
    return [
        item.get("contentDetails", {}).get("videoId")
        for item in response.get("items", [])
        if item.get("contentDetails", {}).get("videoId")
    ]


def _summary_from_row(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    gained = _safe_int(row.get("subscribersGained"))
    lost = _safe_int(row.get("subscribersLost"))
    return {
        "views": _safe_int(row.get("views")),
        "likes": _safe_int(row.get("likes")),
        "comments": _safe_int(row.get("comments")),
        "shares": _safe_int(row.get("shares")),
        "subscribers_gained": gained,
        "subscribers_lost": lost,
        "subscribers_net": gained - lost,
        "watch_minutes": _safe_int(row.get("estimatedMinutesWatched")),
        "average_view_duration": _safe_float(row.get("averageViewDuration")),
    }


def sync_channel_analytics(channel_id: int, days: int = 28) -> dict[str, Any]:
    days = normalize_period(days)
    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)
    previous_end_date = start_date - timedelta(days=1)
    previous_start_date = previous_end_date - timedelta(days=days - 1)

    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            raise RuntimeError("Channel not found")

        credentials = credentials_for_channel(db, channel)
        if YOUTUBE_ANALYTICS_SCOPE not in granted_scopes(credentials):
            raise RuntimeError(
                "Analytics permission is missing. Reconnect this channel to grant YouTube Analytics access."
            )

        data_service = youtube_data_service(credentials)
        analytics_service = youtube_analytics_service(credentials)

        current, uploads_playlist_id = _current_channel(data_service)
        if current["youtube_channel_id"] != channel.youtube_channel_id:
            raise RuntimeError("Authorized YouTube channel does not match the saved channel")

        channel.title = current["title"]
        channel.custom_url = current["custom_url"]
        channel.description = current["description"]
        channel.thumbnail_url = current["thumbnail_url"]
        channel.subscriber_count = current["subscriber_count"]
        channel.view_count = current["view_count"]
        channel.video_count = current["video_count"]
        channel.last_synced_at = datetime.utcnow()

        common = {
            "ids": "channel==MINE",
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
        }

        daily_response = analytics_service.reports().query(
            **common,
            metrics=DAILY_METRICS,
            dimensions="day",
            sort="day",
        ).execute()
        daily_rows = _rows_as_dict(daily_response)

        summary_response = analytics_service.reports().query(
            **common,
            metrics=DAILY_METRICS,
        ).execute()
        summary_rows = _rows_as_dict(summary_response)
        summary = _summary_from_row(summary_rows[0] if summary_rows else None)

        previous_response = analytics_service.reports().query(
            ids="channel==MINE",
            startDate=previous_start_date.isoformat(),
            endDate=previous_end_date.isoformat(),
            metrics=DAILY_METRICS,
        ).execute()
        previous_rows = _rows_as_dict(previous_response)
        previous_summary = _summary_from_row(previous_rows[0] if previous_rows else None)

        def percent_change(current: int | float, previous: int | float):
            previous = float(previous or 0)
            if previous == 0:
                return None
            return round(((float(current or 0) - previous) / abs(previous)) * 100, 1)

        changes = {
            "views": percent_change(summary["views"], previous_summary["views"]),
            "likes": percent_change(summary["likes"], previous_summary["likes"]),
            "comments": percent_change(summary["comments"], previous_summary["comments"]),
            "shares": percent_change(summary["shares"], previous_summary["shares"]),
            "watch_minutes": percent_change(summary["watch_minutes"], previous_summary["watch_minutes"]),
            "subscribers_net_delta": summary["subscribers_net"] - previous_summary["subscribers_net"],
        }

        top_response = analytics_service.reports().query(
            **common,
            metrics=TOP_VIDEO_METRICS,
            dimensions="video",
            sort="-views",
            maxResults=10,
        ).execute()
        top_rows = _rows_as_dict(top_response)

        latest_ids = _latest_upload_ids(data_service, uploads_playlist_id, 12)
        top_ids = [str(row.get("video") or "") for row in top_rows]
        video_details = _load_video_details(data_service, top_ids + latest_ids)

        rows_by_day = {str(row.get("day") or ""): row for row in daily_rows}
        daily = []
        cursor = start_date
        while cursor <= end_date:
            day_key = cursor.isoformat()
            row = rows_by_day.get(day_key, {})
            gained = _safe_int(row.get("subscribersGained"))
            lost = _safe_int(row.get("subscribersLost"))
            daily.append({
                "date": day_key,
                "views": _safe_int(row.get("views")),
                "likes": _safe_int(row.get("likes")),
                "comments": _safe_int(row.get("comments")),
                "shares": _safe_int(row.get("shares")),
                "subscribers_gained": gained,
                "subscribers_lost": lost,
                "subscribers_net": gained - lost,
                "watch_minutes": _safe_int(row.get("estimatedMinutesWatched")),
                "average_view_duration": round(_safe_float(row.get("averageViewDuration")), 2),
            })
            cursor += timedelta(days=1)

        top_videos = []
        for row in top_rows:
            video_id = str(row.get("video") or "")
            detail = video_details.get(video_id, {"video_id": video_id, "title": video_id, "url": f"https://youtube.com/watch?v={video_id}"})
            top_videos.append({
                **detail,
                "period_views": _safe_int(row.get("views")),
                "period_likes": _safe_int(row.get("likes")),
                "period_comments": _safe_int(row.get("comments")),
                "period_shares": _safe_int(row.get("shares")),
                "period_subscribers_gained": _safe_int(row.get("subscribersGained")),
                "period_watch_minutes": _safe_int(row.get("estimatedMinutesWatched")),
                "period_average_view_duration": round(_safe_float(row.get("averageViewDuration")), 2),
            })

        latest_videos = [video_details[video_id] for video_id in latest_ids if video_id in video_details]

        payload = {
            "channel_id": channel.id,
            "youtube_channel_id": channel.youtube_channel_id,
            "period_days": days,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "fetched_at": datetime.utcnow().isoformat(),
            "summary": summary,
            "previous_summary": previous_summary,
            "changes": changes,
            "daily": daily,
            "top_videos": top_videos,
            "latest_videos": latest_videos,
            "current": current,
        }

        cache = db.query(ChannelAnalyticsCache).filter_by(channel_id=channel.id, period_days=days).one_or_none()
        if cache is None:
            cache = ChannelAnalyticsCache(
                channel_id=channel.id,
                period_days=days,
                payload_json=json.dumps(payload, ensure_ascii=False),
                fetched_at=datetime.utcnow(),
            )
            db.add(cache)
        else:
            cache.payload_json = json.dumps(payload, ensure_ascii=False)
            cache.fetched_at = datetime.utcnow()

        db.commit()
        return payload


def get_cached_analytics(channel_id: int, days: int = 28) -> dict[str, Any] | None:
    days = normalize_period(days)
    with SessionLocal() as db:
        cache = db.query(ChannelAnalyticsCache).filter_by(channel_id=channel_id, period_days=days).one_or_none()
        if not cache:
            return None
        try:
            payload = json.loads(cache.payload_json)
        except json.JSONDecodeError:
            return None
        payload["_cache_fetched_at"] = cache.fetched_at.isoformat()
        return payload


def get_or_sync_channel_analytics(
    channel_id: int,
    days: int = 28,
    *,
    max_age_minutes: int = 30,
) -> tuple[dict[str, Any] | None, str | None]:
    days = normalize_period(days)
    cached = get_cached_analytics(channel_id, days)
    if cached:
        fetched = cached.get("_cache_fetched_at") or cached.get("fetched_at")
        try:
            fetched_at = datetime.fromisoformat(fetched)
            if datetime.utcnow() - fetched_at <= timedelta(minutes=max_age_minutes):
                return cached, None
        except (TypeError, ValueError):
            pass

    try:
        return sync_channel_analytics(channel_id, days), None
    except Exception as exc:
        logger.warning("Analytics sync failed for channel %s: %s", channel_id, exc)
        return cached, str(exc)
