from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class YouTubeChannel(Base):
    __tablename__ = "youtube_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    youtube_channel_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    custom_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_hashtags: Mapped[str] = mapped_column(Text, default="#Shorts #YouTubeShorts", nullable=False)
    default_privacy: Mapped[str] = mapped_column(String(20), default="public", nullable=False)

    subscriber_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    view_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    video_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    jobs: Mapped[list["UploadJob"]] = relationship(back_populates="channel")
    analytics_caches: Mapped[list["ChannelAnalyticsCache"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class ChannelAnalyticsCache(Base):
    __tablename__ = "channel_analytics_cache"
    __table_args__ = (UniqueConstraint("channel_id", "period_days", name="uq_channel_analytics_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"), index=True, nullable=False
    )
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    channel: Mapped[YouTubeChannel] = relationship(back_populates="analytics_caches")


class UploadJob(Base):
    __tablename__ = "upload_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("youtube_channels.id", ondelete="RESTRICT"), index=True)
    source: Mapped[str] = mapped_column(String(30), default="panel", nullable=False)
    telegram_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)

    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    hashtags: Mapped[str] = mapped_column(Text, default="", nullable=False)
    privacy: Mapped[str] = mapped_column(String(20), default="public", nullable=False)

    status: Mapped[str] = mapped_column(String(30), default="queued", index=True, nullable=False)
    video_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    video_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    channel: Mapped[YouTubeChannel] = relationship(back_populates="jobs")


class TelegramPreference(Base):
    __tablename__ = "telegram_preferences"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[Optional[int]] = mapped_column(ForeignKey("youtube_channels.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class OAuthRequest(Base):
    __tablename__ = "oauth_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="Telegram channel", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    details: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class SystemSecret(Base):
    __tablename__ = "system_secrets"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class TelegramAdmin(Base):
    __tablename__ = "telegram_admins"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ChannelPublishingConfig(Base):
    __tablename__ = "channel_publishing_configs"

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    smart_peak_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    videos_per_day: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Tehran", nullable=False)
    minimum_gap_minutes: Mapped[int] = mapped_column(Integer, default=180, nullable=False)
    allowed_start_hour: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    allowed_end_hour: Mapped[int] = mapped_column(Integer, default=23, nullable=False)
    manual_slots_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    peak_slots_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    peak_analysis_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    last_analyzed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class ChannelMissionState(Base):
    __tablename__ = "channel_mission_states"

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    daily_report: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_report_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class UploadSchedule(Base):
    __tablename__ = "upload_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("upload_jobs.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    schedule_mode: Mapped[str] = mapped_column(String(24), default="smart", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="waiting", index=True, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class InstagramDirectShare(Base):
    __tablename__ = "instagram_direct_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_key: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    item_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    sender_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    sender_username: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    media_url: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(30), default="post", nullable=False)
    title_hint: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    thumbnail_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    status: Mapped[str] = mapped_column(String(30), default="pending_channel", index=True, nullable=False)
    selected_channel_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="SET NULL"), nullable=True, index=True
    )
    telegram_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    upload_job_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("upload_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    selected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
