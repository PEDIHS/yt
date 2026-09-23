from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
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
