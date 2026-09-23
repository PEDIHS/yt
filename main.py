import asyncio
import logging
import time
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import Config
from db import SessionLocal, init_db
from downloader import is_supported_instagram_url
from jobs import create_job, process_job
from analytics import get_cached_analytics
from publishing import (
    analyze_peak_slots,
    cancel_scheduled_job,
    publishing_config_payload,
    queue_snapshot,
    release_job_now,
    schedule_job_smart,
    update_publishing_config,
    utc_to_channel_local,
)
from integrations import claim_telegram_admin, is_telegram_admin, resolve_telegram_token
from models import OAuthRequest, TelegramPreference, UploadJob, YouTubeChannel
from security import new_token
from youtube import (
    add_video_to_playlist,
    create_playlist,
    delete_comment,
    delete_video,
    get_video_manager_data,
    list_channel_playlists,
    list_channel_videos,
    list_video_comments,
    refresh_channel,
    reply_to_comment,
    set_video_thumbnail,
    update_video_metadata,
    upload_caption,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("telegram-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _authorized(user_id: int) -> bool:
    return is_telegram_admin(user_id)


async def _guard(update: Update) -> bool:
    user = update.effective_user
    if user and _authorized(user.id):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("⛔ دسترسی به این ربات محدود به مدیران است.")
    return False


async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except (NetworkError, TimedOut) as exc:
        logger.warning("Telegram send failed: %s", exc)


def _get_channels(active_only: bool = False):
    with SessionLocal() as db:
        query = db.query(YouTubeChannel)
        if active_only:
            query = query.filter(YouTubeChannel.is_active.is_(True))
        return query.order_by(YouTubeChannel.id.asc()).all()


def _selected_channel(user_id: int):
    with SessionLocal() as db:
        pref = db.get(TelegramPreference, user_id)
        if not pref or not pref.channel_id:
            return None
        channel = db.get(YouTubeChannel, pref.channel_id)
        if not channel or not channel.is_active:
            return None
        return channel


def _set_selected_channel(user_id: int, channel_id: int) -> None:
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel or not channel.is_active:
            raise RuntimeError("Channel unavailable")
        pref = db.get(TelegramPreference, user_id)
        if pref is None:
            pref = TelegramPreference(user_id=user_id, channel_id=channel_id)
            db.add(pref)
        else:
            pref.channel_id = channel_id
        db.commit()


def _channel_keyboard(active_only: bool = True) -> InlineKeyboardMarkup:
    channels = _get_channels(active_only=active_only)
    rows = []
    for channel in channels:
        icon = "✅" if channel.is_active else "⏸"
        rows.append([InlineKeyboardButton(f"{icon} {channel.label} · {channel.title}", callback_data=f"select:{channel.id}")])
    if not rows:
        rows = [[InlineKeyboardButton("➕ اتصال کانال جدید", callback_data="connect")]]
    return InlineKeyboardMarkup(rows)



async def claim_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_message:
        return
    if _authorized(update.effective_user.id):
        await update.effective_message.reply_text("✅ شما از قبل مدیر این ربات هستید.")
        return
    if not context.args:
        await update.effective_message.reply_text("کد یک‌بارمصرف پنل را به شکل /claim CODE ارسال کن.")
        return
    code = context.args[0].strip()
    if claim_telegram_admin(update.effective_user.id, code):
        await update.effective_message.reply_text("✅ دسترسی مدیریت فعال شد. حالا /start را بزن.")
    else:
        await update.effective_message.reply_text("❌ کد نامعتبر یا منقضی است.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    selected = _selected_channel(update.effective_user.id)
    selected_text = f"\n📺 کانال انتخاب‌شده: {selected.label}" if selected else "\n⚠️ هنوز کانالی انتخاب نشده."
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 کانال‌ها", callback_data="channels"), InlineKeyboardButton("📊 آمار", callback_data="stats")],
        [InlineKeyboardButton("🎬 ویدیوها", callback_data="videos"), InlineKeyboardButton("✨ انتشار هوشمند", callback_data="smart")],
        [InlineKeyboardButton("🕓 صف انتشار", callback_data="queue"), InlineKeyboardButton("📤 ارسال محتوا", callback_data="howto")],
        [InlineKeyboardButton("🧾 ارسال‌های اخیر", callback_data="uploads"), InlineKeyboardButton("➕ اتصال کانال", callback_data="connect")],
    ])
    await update.effective_message.reply_text(
        "🤖 مدیریت چندکاناله YouTube Shorts\n"
        "لینک Reel/Post اینستاگرام را بفرست، عنوان را وارد کن و محتوا به کانال انتخاب‌شده ارسال می‌شود."
        + selected_text,
        reply_markup=keyboard,
    )


async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channels = _get_channels(active_only=False)
    if not channels:
        await update.effective_message.reply_text("هنوز کانال YouTube متصل نشده. از /connect استفاده کن.")
        return
    lines = ["📺 کانال‌های YouTube:"]
    for ch in channels:
        state = "فعال" if ch.is_active else "غیرفعال"
        lines.append(f"#{ch.id} · {ch.label} · {ch.title} · {state} · {ch.video_count:,} ویدیو")
    await update.effective_message.reply_text("\n".join(lines), reply_markup=_channel_keyboard(active_only=True))


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    token = new_token(32)
    with SessionLocal() as db:
        db.add(OAuthRequest(
            token=token,
            telegram_user_id=update.effective_user.id,
            label=f"Telegram {update.effective_user.id}",
            expires_at=datetime.utcnow() + timedelta(minutes=Config.OAUTH_LINK_MINUTES),
        ))
        db.commit()
    url = f"{Config.PUBLIC_BASE_URL}/telegram/connect/{token}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 اتصال کانال YouTube", url=url)]])
    await update.effective_message.reply_text(
        f"این لینک فقط {Config.OAUTH_LINK_MINUTES} دقیقه معتبر است. حساب/کانال YouTube موردنظر را در Google انتخاب کن.",
        reply_markup=keyboard,
    )


async def channel_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = None
    if context.args:
        try:
            channel_id = int(context.args[0])
            with SessionLocal() as db:
                channel = db.get(YouTubeChannel, channel_id)
        except ValueError:
            pass
    else:
        channel = _selected_channel(update.effective_user.id)
    if not channel:
        await update.effective_message.reply_text("کانال پیدا نشد. /channels")
        return
    await update.effective_message.reply_text(
        f"📺 {channel.label}\n"
        f"نام YouTube: {channel.title}\n"
        f"ID: {channel.youtube_channel_id}\n"
        f"وضعیت: {'فعال' if channel.is_active else 'غیرفعال'}\n"
        f"Privacy: {channel.default_privacy}\n"
        f"مشترک: {channel.subscriber_count:,}\n"
        f"بازدید: {channel.view_count:,}\n"
        f"ویدیو: {channel.video_count:,}\n"
        f"هشتگ‌ها: {channel.default_hashtags}"
    )


async def recent_uploads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    with SessionLocal() as db:
        jobs = db.query(UploadJob).order_by(UploadJob.id.desc()).limit(8).all()
        channel_names = {c.id: c.label for c in db.query(YouTubeChannel).all()}
    if not jobs:
        await update.effective_message.reply_text("هنوز ارسالی ثبت نشده.")
        return
    lines = ["🧾 آخرین ارسال‌ها:"]
    for job in jobs:
        icon = {"completed": "✅", "failed": "❌", "queued": "🕓", "downloading": "⬇️", "uploading": "⬆️"}.get(job.status, "•")
        suffix = f"\n{job.video_url}" if job.video_url else ""
        lines.append(f"{icon} #{job.id} · {channel_names.get(job.channel_id, job.channel_id)} · {job.title}{suffix}")
    await update.effective_message.reply_text("\n".join(lines))


async def toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("استفاده: /toggle CHANNEL_ID")
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("CHANNEL_ID نامعتبر است.")
        return
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            await update.effective_message.reply_text("کانال پیدا نشد.")
            return
        channel.is_active = not channel.is_active
        db.commit()
        label, active = channel.label, channel.is_active
    await update.effective_message.reply_text(f"{'✅ فعال شد' if active else '⏸ غیرفعال شد'}: {label}")


async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if len(context.args) < 2 or context.args[1] not in {"public", "unlisted", "private"}:
        await update.effective_message.reply_text("استفاده: /setprivacy CHANNEL_ID public|unlisted|private")
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("CHANNEL_ID نامعتبر است.")
        return
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            await update.effective_message.reply_text("کانال پیدا نشد.")
            return
        channel.default_privacy = context.args[1]
        db.commit()
    await update.effective_message.reply_text("✅ Privacy پیش‌فرض کانال تغییر کرد.")


async def hashtags_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("استفاده: /sethashtags CHANNEL_ID #tag1 #tag2")
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("CHANNEL_ID نامعتبر است.")
        return
    hashtags = " ".join(context.args[1:])[:2000]
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            await update.effective_message.reply_text("کانال پیدا نشد.")
            return
        channel.default_hashtags = hashtags
        db.commit()
    await update.effective_message.reply_text("✅ هشتگ‌های پیش‌فرض کانال ذخیره شد.")


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    channel_id = channel.id if channel else None
    if context.args:
        try:
            channel_id = int(context.args[0])
        except ValueError:
            channel_id = None
    if not channel_id:
        await update.effective_message.reply_text("استفاده: /refresh CHANNEL_ID")
        return
    await update.effective_message.reply_text("🔄 در حال همگام‌سازی اطلاعات کانال...")
    try:
        refreshed = await asyncio.to_thread(refresh_channel, channel_id)
        await update.effective_message.reply_text(f"✅ {refreshed.title} همگام شد · {refreshed.subscriber_count:,} مشترک")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ خطا: {exc}")



def _resolve_channel_for_command(user_id: int, raw_id: str | None = None):
    if raw_id:
        try:
            channel_id = int(raw_id)
        except ValueError:
            return None
        with SessionLocal() as db:
            return db.get(YouTubeChannel, channel_id)
    return _selected_channel(user_id)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _resolve_channel_for_command(update.effective_user.id, context.args[0] if context.args else None)
    if not channel:
        await update.effective_message.reply_text("کانال پیدا نشد. از /channels انتخاب کن.")
        return
    payload = get_cached_analytics(channel.id, 28) or {}
    summary = payload.get("summary", {})
    cfg = publishing_config_payload(channel.id)
    peaks = cfg.get("peak_slots", [])
    peak_text = "، ".join(f"{int(item.get('hour', 0)):02d}:00" for item in peaks[:4]) or "هنوز تحلیل نشده"
    await update.effective_message.reply_text(
        f"📊 {channel.title}\n"
        f"👥 Subscribers: {channel.subscriber_count:,}\n"
        f"👁 Lifetime Views: {channel.view_count:,}\n"
        f"🎬 Videos: {channel.video_count:,}\n\n"
        f"📈 ۲۸ روز اخیر\n"
        f"Views: {int(summary.get('views', 0) or 0):,}\n"
        f"Likes: {int(summary.get('likes', 0) or 0):,}\n"
        f"Comments: {int(summary.get('comments', 0) or 0):,}\n"
        f"Watch Time: {int(summary.get('watch_minutes', 0) or 0):,} min\n"
        f"Net Subs: {int(summary.get('subscribers_net', 0) or 0):+,}\n\n"
        f"✨ Auto Publisher: {'ON' if cfg.get('enabled') else 'OFF'}\n"
        f"📅 روزانه: {cfg.get('videos_per_day', 2)} ویدیو\n"
        f"🕒 Peak: {peak_text}\n"
        f"🌍 Timezone: {cfg.get('timezone', 'Asia/Tehran')}"
    )


async def smart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel:
        await update.effective_message.reply_text("ابتدا از /channels کانال پیش‌فرض را انتخاب کن.")
        return
    cfg = publishing_config_payload(channel.id)
    peaks = cfg.get("peak_slots", [])
    lines = [
        f"✨ Smart Publisher · {channel.title}",
        f"وضعیت: {'✅ فعال' if cfg.get('enabled') else '⏸ غیرفعال'}",
        f"تعداد روزانه: {cfg.get('videos_per_day', 2)}",
        f"Timezone: {cfg.get('timezone')}",
        f"حداقل فاصله: {cfg.get('minimum_gap_minutes')} دقیقه",
        "",
        "Peak Slots:",
    ]
    if peaks:
        lines += [f"• {int(x.get('hour',0)):02d}:00 · score {float(x.get('score',0)):.1f} · {int(x.get('samples',0))} sample" for x in peaks[:6]]
    else:
        lines.append("• هنوز تحلیل نشده؛ /peaks را بزن.")
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ روشن" if not cfg.get("enabled") else "⏸ خاموش", callback_data=f"autopost:{channel.id}:{0 if cfg.get('enabled') else 1}"),
            InlineKeyboardButton("🧠 تحلیل Peak", callback_data=f"peaks:{channel.id}"),
        ],
        [InlineKeyboardButton("🕓 صف این کانال", callback_data=f"queuech:{channel.id}")],
    ])
    await update.effective_message.reply_text("\n".join(lines), reply_markup=keyboard)


async def autopost_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if len(context.args) < 2 or context.args[1].lower() not in {"on", "off"}:
        await update.effective_message.reply_text("استفاده: /autopost CHANNEL_ID on|off")
        return
    channel = _resolve_channel_for_command(update.effective_user.id, context.args[0])
    if not channel:
        await update.effective_message.reply_text("کانال پیدا نشد.")
        return
    cfg = publishing_config_payload(channel.id)
    update_publishing_config(
        channel.id,
        enabled=context.args[1].lower() == "on",
        smart_peak_enabled=cfg.get("smart_peak_enabled", True),
        videos_per_day=cfg.get("videos_per_day", 2),
        timezone_name=cfg.get("timezone", "Asia/Tehran"),
        minimum_gap_minutes=cfg.get("minimum_gap_minutes", 180),
        allowed_start_hour=cfg.get("allowed_start_hour", 9),
        allowed_end_hour=cfg.get("allowed_end_hour", 23),
        manual_slots=cfg.get("manual_slots", []),
    )
    await update.effective_message.reply_text(f"✅ Auto Publisher برای {channel.title} {'روشن' if context.args[1].lower() == 'on' else 'خاموش'} شد.")


async def perday_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("استفاده: /perday CHANNEL_ID 1..12")
        return
    channel = _resolve_channel_for_command(update.effective_user.id, context.args[0])
    try:
        count = max(1, min(12, int(context.args[1])))
    except ValueError:
        count = 0
    if not channel or not count:
        await update.effective_message.reply_text("کانال یا تعداد نامعتبر است.")
        return
    cfg = publishing_config_payload(channel.id)
    update_publishing_config(
        channel.id,
        enabled=cfg.get("enabled", False),
        smart_peak_enabled=cfg.get("smart_peak_enabled", True),
        videos_per_day=count,
        timezone_name=cfg.get("timezone", "Asia/Tehran"),
        minimum_gap_minutes=cfg.get("minimum_gap_minutes", 180),
        allowed_start_hour=cfg.get("allowed_start_hour", 9),
        allowed_end_hour=cfg.get("allowed_end_hour", 23),
        manual_slots=cfg.get("manual_slots", []),
    )
    await update.effective_message.reply_text(f"✅ ظرفیت روزانه {channel.title}: {count} ویدیو.")


async def peaks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _resolve_channel_for_command(update.effective_user.id, context.args[0] if context.args else None)
    if not channel:
        await update.effective_message.reply_text("کانال پیدا نشد.")
        return
    await update.effective_message.reply_text("🧠 در حال تحلیل تاریخچه کانال و ساخت Peak Slots...")
    try:
        result = await asyncio.to_thread(analyze_peak_slots, channel.id, 90)
        slots = result.get("best_hours", [])
        lines = [
            f"✅ تحلیل {channel.title}",
            f"Confidence: {result.get('confidence')}",
            f"Samples: {result.get('video_samples', 0)}",
            f"Top country: {result.get('top_country') or '—'}",
            f"Top device: {result.get('top_device') or '—'}",
            "",
            "بهترین ساعت‌ها:",
        ]
        lines += [f"• {int(x.get('hour',0)):02d}:00 · score {float(x.get('score',0)):.1f}" for x in slots]
        await update.effective_message.reply_text("\n".join(lines))
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ تحلیل ناموفق بود: {exc}")


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _resolve_channel_for_command(update.effective_user.id, context.args[0] if context.args else None)
    channel_id = channel.id if channel else None
    rows = queue_snapshot(channel_id=channel_id, limit=15)
    if not rows:
        await update.effective_message.reply_text("🕓 صف انتشار خالی است.")
        return
    lines = ["🕓 صف انتشار:"]
    buttons = []
    for item in rows:
        local = utc_to_channel_local(item["channel_id"], item["scheduled_for"])
        lines.append(f"#{item['job_id']} · {item['channel_label']} · {local:%m-%d %H:%M}\n{item['title']}")
        buttons.append([
            InlineKeyboardButton(f"🚀 #{item['job_id']} الان", callback_data=f"pubnow:{item['job_id']}"),
            InlineKeyboardButton("❌ لغو", callback_data=f"canceljob:{item['job_id']}"),
        ])
    await update.effective_message.reply_text("\n\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons[:8]))


async def publishnow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("استفاده: /publishnow JOB_ID")
        return
    job_id = int(context.args[0])
    try:
        release_job_now(job_id)
        await update.effective_message.reply_text(f"🚀 Job #{job_id} برای انتشار فوری آزاد شد.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def canceljob_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("استفاده: /canceljob JOB_ID")
        return
    try:
        cancel_scheduled_job(int(context.args[0]))
        await update.effective_message.reply_text("✅ از صف حذف شد.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def videos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _resolve_channel_for_command(update.effective_user.id, context.args[0] if context.args else None)
    if not channel:
        await update.effective_message.reply_text("کانال پیدا نشد.")
        return
    try:
        payload = await asyncio.to_thread(list_channel_videos, channel.id, "", 10)
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ دریافت ویدیوها ناموفق بود: {exc}")
        return
    rows = []
    text_lines = [f"🎬 آخرین ویدیوهای {channel.title}:"]
    for video in payload.get("items", []):
        text_lines.append(f"• {video['title']}\n👁 {video['views']:,} · 👍 {video['likes']:,} · 🔐 {video['privacy']}\nID: {video['video_id']}")
        rows.append([InlineKeyboardButton(f"⚙️ {video['title'][:28]}", callback_data=f"video:{channel.id}:{video['video_id']}")])
    await update.effective_message.reply_text("\n\n".join(text_lines), reply_markup=InlineKeyboardMarkup(rows[:10]))


async def video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or not context.args:
        await update.effective_message.reply_text("استفاده: /video VIDEO_ID بعد از انتخاب کانال")
        return
    video_id = context.args[0].strip()
    try:
        video = await asyncio.to_thread(get_video_manager_data, channel.id, video_id)
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Public", callback_data=f"vpriv:{channel.id}:{video_id}:public"), InlineKeyboardButton("🔗 Unlisted", callback_data=f"vpriv:{channel.id}:{video_id}:unlisted"), InlineKeyboardButton("🔒 Private", callback_data=f"vpriv:{channel.id}:{video_id}:private")],
        [InlineKeyboardButton("💬 Comments", callback_data=f"comments:{channel.id}:{video_id}"), InlineKeyboardButton("🗑 حذف", callback_data=f"delask:{channel.id}:{video_id}")],
        [InlineKeyboardButton("🛠 Video Studio", url=f"{Config.PUBLIC_BASE_URL}/videos/{channel.id}/{video_id}")],
    ])
    await update.effective_message.reply_text(
        f"🎬 {video['title']}\n"
        f"👁 {video['views']:,} · 👍 {video['likes']:,} · 💬 {video['comments']:,}\n"
        f"🔐 {video['privacy']} · ⏱ {video['duration_seconds']} sec\n"
        f"ID: {video_id}",
        reply_markup=keyboard,
    )


async def videoprivacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or len(context.args) < 2 or context.args[1] not in {"public", "unlisted", "private"}:
        await update.effective_message.reply_text("استفاده: /videoprivacy VIDEO_ID public|unlisted|private")
        return
    video_id, privacy = context.args[0], context.args[1]
    try:
        video = await asyncio.to_thread(get_video_manager_data, channel.id, video_id)
        await asyncio.to_thread(
            update_video_metadata,
            channel.id,
            video_id,
            title=video["title"],
            description=video["description"],
            tags=video["tags"],
            privacy=privacy,
            category_id=video["category_id"],
            made_for_kids=video["made_for_kids"],
            embeddable=video["embeddable"],
        )
        await update.effective_message.reply_text(f"✅ Privacy ویدیو روی {privacy} تنظیم شد.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def videotitle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or len(context.args) < 2:
        await update.effective_message.reply_text("استفاده: /videotitle VIDEO_ID عنوان جدید")
        return
    video_id, title = context.args[0], " ".join(context.args[1:])
    try:
        video = await asyncio.to_thread(get_video_manager_data, channel.id, video_id)
        await asyncio.to_thread(update_video_metadata, channel.id, video_id, title=title, description=video["description"], tags=video["tags"], privacy=video["privacy"], category_id=video["category_id"], made_for_kids=video["made_for_kids"], embeddable=video["embeddable"])
        await update.effective_message.reply_text("✅ عنوان ویدیو بروزرسانی شد.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def videodesc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or len(context.args) < 2:
        await update.effective_message.reply_text("استفاده: /videodesc VIDEO_ID توضیحات جدید")
        return
    video_id, description = context.args[0], " ".join(context.args[1:])
    try:
        video = await asyncio.to_thread(get_video_manager_data, channel.id, video_id)
        await asyncio.to_thread(update_video_metadata, channel.id, video_id, title=video["title"], description=description, tags=video["tags"], privacy=video["privacy"], category_id=video["category_id"], made_for_kids=video["made_for_kids"], embeddable=video["embeddable"])
        await update.effective_message.reply_text("✅ توضیحات ویدیو بروزرسانی شد.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def videotags_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or len(context.args) < 2:
        await update.effective_message.reply_text("استفاده: /videotags VIDEO_ID tag1,tag2")
        return
    video_id = context.args[0]
    tags = [tag.strip() for tag in " ".join(context.args[1:]).split(",") if tag.strip()]
    try:
        video = await asyncio.to_thread(get_video_manager_data, channel.id, video_id)
        await asyncio.to_thread(update_video_metadata, channel.id, video_id, title=video["title"], description=video["description"], tags=tags, privacy=video["privacy"], category_id=video["category_id"], made_for_kids=video["made_for_kids"], embeddable=video["embeddable"])
        await update.effective_message.reply_text("✅ Tagها بروزرسانی شدند.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def deletevideo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or not context.args:
        await update.effective_message.reply_text("استفاده: /deletevideo VIDEO_ID")
        return
    video_id = context.args[0]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 بله، حذف دائمی", callback_data=f"delvid:{channel.id}:{video_id}"),
        InlineKeyboardButton("لغو", callback_data="noop"),
    ]])
    await update.effective_message.reply_text("⚠️ حذف ویدیو از YouTube قابل بازگشت نیست. تأیید می‌کنی؟", reply_markup=keyboard)


async def comments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or not context.args:
        await update.effective_message.reply_text("استفاده: /comments VIDEO_ID")
        return
    video_id = context.args[0]
    try:
        payload = await asyncio.to_thread(list_video_comments, channel.id, video_id, "published")
        comments = payload.get("items", [])[:8]
        if not comments:
            await update.effective_message.reply_text("Commentی پیدا نشد.")
            return
        lines = ["💬 Commentها:"]
        for item in comments:
            lines.append(f"• {item['author']}: {item['text'][:180]}\nID: {item['comment_id']}")
        await update.effective_message.reply_text("\n\n".join(lines))
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or len(context.args) < 3:
        await update.effective_message.reply_text("استفاده: /reply VIDEO_ID COMMENT_ID متن پاسخ")
        return
    video_id, comment_id = context.args[0], context.args[1]
    text = " ".join(context.args[2:])
    try:
        await asyncio.to_thread(reply_to_comment, channel.id, video_id, comment_id, text)
        await update.effective_message.reply_text("✅ پاسخ ارسال شد.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def deletecomment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or len(context.args) < 2:
        await update.effective_message.reply_text("استفاده: /deletecomment VIDEO_ID COMMENT_ID")
        return
    try:
        await asyncio.to_thread(delete_comment, channel.id, context.args[0], context.args[1])
        await update.effective_message.reply_text("✅ Comment حذف شد.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def playlists_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel:
        await update.effective_message.reply_text("ابتدا کانال را انتخاب کن.")
        return
    try:
        rows = await asyncio.to_thread(list_channel_playlists, channel.id)
        if not rows:
            await update.effective_message.reply_text("Playlistی وجود ندارد.")
            return
        await update.effective_message.reply_text("\n".join(["📚 Playlistها:"] + [f"• {p['title']} · {p['item_count']} ویدیو\nID: {p['playlist_id']}" for p in rows[:20]]))
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def newplaylist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or len(context.args) < 2 or context.args[0] not in {"public","unlisted","private"}:
        await update.effective_message.reply_text("استفاده: /newplaylist private عنوان Playlist")
        return
    try:
        await asyncio.to_thread(create_playlist, channel.id, " ".join(context.args[1:]), "", context.args[0])
        await update.effective_message.reply_text("✅ Playlist ساخته شد.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def addplaylist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel or len(context.args) < 2:
        await update.effective_message.reply_text("استفاده: /addplaylist VIDEO_ID PLAYLIST_ID")
        return
    try:
        await asyncio.to_thread(add_video_to_playlist, channel.id, context.args[1], context.args[0])
        await update.effective_message.reply_text("✅ ویدیو به Playlist اضافه شد.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")


async def thumbnail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("استفاده: /thumbnail VIDEO_ID سپس عکس را ارسال کن.")
        return
    context.user_data["media_action"] = "thumbnail"
    context.user_data["media_video_id"] = context.args[0]
    await update.effective_message.reply_text("🖼 حالا فایل JPEG/PNG یا عکس Thumbnail را بفرست.")


async def caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if len(context.args) < 3:
        await update.effective_message.reply_text("استفاده: /caption VIDEO_ID fa نام_زیرنویس سپس فایل Caption را بفرست.")
        return
    context.user_data["media_action"] = "caption"
    context.user_data["media_video_id"] = context.args[0]
    context.user_data["caption_language"] = context.args[1]
    context.user_data["caption_name"] = " ".join(context.args[2:])
    await update.effective_message.reply_text("📄 حالا فایل Caption را به‌صورت Document بفرست.")


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    action = context.user_data.get("media_action")
    if not action:
        await update.effective_message.reply_text("برای Thumbnail اول /thumbnail VIDEO_ID و برای Caption اول /caption ... را بزن.")
        return
    channel = _selected_channel(update.effective_user.id)
    if not channel:
        await update.effective_message.reply_text("کانال انتخاب نشده.")
        return
    video_id = context.user_data.get("media_video_id")
    try:
        if action == "thumbnail":
            if update.effective_message.photo:
                item = update.effective_message.photo[-1]
                tg_file = await context.bot.get_file(item.file_id)
                data = bytes(await tg_file.download_as_bytearray())
                mime = "image/jpeg"
            elif update.effective_message.document:
                doc = update.effective_message.document
                tg_file = await context.bot.get_file(doc.file_id)
                data = bytes(await tg_file.download_as_bytearray())
                mime = doc.mime_type or "application/octet-stream"
            else:
                raise RuntimeError("عکس یا فایل تصویری ارسال کن")
            await asyncio.to_thread(set_video_thumbnail, channel.id, video_id, data, mime)
            await update.effective_message.reply_text("✅ Thumbnail روی YouTube بروزرسانی شد.")
        elif action == "caption":
            doc = update.effective_message.document
            if not doc:
                raise RuntimeError("Caption را به‌صورت Document ارسال کن")
            tg_file = await context.bot.get_file(doc.file_id)
            data = bytes(await tg_file.download_as_bytearray())
            await asyncio.to_thread(
                upload_caption,
                channel.id,
                video_id,
                content=data,
                language=context.user_data.get("caption_language", "fa"),
                name=context.user_data.get("caption_name", "Caption"),
                is_draft=False,
            )
            await update.effective_message.reply_text("✅ Caption روی YouTube آپلود شد.")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
    finally:
        for key in ["media_action","media_video_id","caption_language","caption_name"]:
            context.user_data.pop(key, None)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    context.user_data.clear()
    await update.effective_message.reply_text("لغو شد.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    await update.effective_message.reply_text(
        "🤖 دستورات مدیریت YT Studio\n\n"
        "📺 کانال و آمار\n"
        "/channels · /channelinfo [id] · /stats [id] · /refresh [id]\n"
        "/connect · /toggle id · /setprivacy id ... · /sethashtags id ...\n\n"
        "✨ انتشار هوشمند\n"
        "/smart · /peaks [id] · /autopost id on|off · /perday id N\n"
        "/queue [id] · /publishnow JOB_ID · /canceljob JOB_ID\n\n"
        "🎬 مدیریت ویدیو\n"
        "/videos [id] · /video VIDEO_ID · /videoprivacy VIDEO_ID public|unlisted|private\n"
        "/videotitle VIDEO_ID title · /videodesc VIDEO_ID description · /videotags VIDEO_ID tag1,tag2\n"
        "/deletevideo VIDEO_ID · /thumbnail VIDEO_ID\n\n"
        "💬 Comments / Captions / Playlist\n"
        "/comments VIDEO_ID · /reply VIDEO_ID COMMENT_ID text · /deletecomment VIDEO_ID COMMENT_ID\n"
        "/caption VIDEO_ID fa Name · /playlists · /newplaylist private Title · /addplaylist VIDEO_ID PLAYLIST_ID\n\n"
        "/uploads · /cancel"
    )


async def _run_job_and_notify(context: ContextTypes.DEFAULT_TYPE, chat_id: int, job_id: int):
    result = await asyncio.to_thread(process_job, job_id)
    if result.get("success"):
        await safe_send(
            context,
            chat_id,
            f"✅ آپلود کامل شد.\n📺 {result.get('channel_title')}\n🔗 {result.get('video_url')}\n🔐 {result.get('privacy')}",
        )
    else:
        await safe_send(context, chat_id, f"❌ Job #{job_id} ناموفق بود:\n{result.get('error')}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    text = (update.effective_message.text or "").strip()
    user_id = update.effective_user.id

    if is_supported_instagram_url(text):
        context.user_data.clear()
        context.user_data["pending_url"] = text
        selected = _selected_channel(user_id)
        if selected:
            context.user_data["channel_id"] = selected.id
            await update.effective_message.reply_text(f"📺 مقصد: {selected.label}\n✏️ حالا عنوان ویدیو را بفرست.")
        else:
            await update.effective_message.reply_text("ابتدا کانال مقصد را انتخاب کن:", reply_markup=_channel_keyboard(active_only=True))
        return

    pending_url = context.user_data.get("pending_url")
    channel_id = context.user_data.get("channel_id")
    if pending_url and channel_id:
        try:
            job = create_job(
                channel_id=channel_id,
                source_url=pending_url,
                title=text,
                source="telegram",
                telegram_user_id=user_id,
            )
        except Exception as exc:
            await update.effective_message.reply_text(f"❌ ایجاد Job ناموفق بود: {exc}")
            context.user_data.clear()
            return
        context.user_data.clear()
        try:
            cfg = publishing_config_payload(channel_id)
            if cfg.get("enabled"):
                schedule = schedule_job_smart(job.id)
                local = utc_to_channel_local(channel_id, schedule.scheduled_for)
                await update.effective_message.reply_text(
                    f"✨ Job #{job.id} وارد صف هوشمند شد.\n🕒 زمان پیشنهادی: {local:%Y-%m-%d %H:%M} · {cfg.get('timezone')}"
                )
            else:
                await update.effective_message.reply_text(f"🕓 Job #{job.id} ثبت شد. دانلود و آپلود در حال انجام است.")
                context.application.create_task(_run_job_and_notify(context, update.effective_chat.id, job.id))
        except Exception as exc:
            await update.effective_message.reply_text(f"❌ زمان‌بندی ناموفق بود: {exc}")
        return

    await update.effective_message.reply_text("لینک معتبر Instagram Reel/Post بفرست یا /help را ببین.")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not _authorized(query.from_user.id):
        await query.answer("دسترسی ندارید", show_alert=True)
        return
    await query.answer()
    data = query.data or ""

    if data == "channels":
        await query.message.reply_text("کانال مقصد را انتخاب کن:", reply_markup=_channel_keyboard(active_only=True))
    elif data == "connect":
        await connect_command(update, context)
    elif data == "uploads":
        await recent_uploads_command(update, context)
    elif data == "howto":
        await query.message.reply_text("یک لینک Instagram Reel/Post بفرست؛ سپس عنوان را ارسال کن. مقصد همان کانال انتخاب‌شده در /channels است.")
    elif data == "stats":
        await stats_command(update, context)
    elif data == "videos":
        await videos_command(update, context)
    elif data == "smart":
        await smart_command(update, context)
    elif data == "queue":
        await queue_command(update, context)
    elif data == "noop":
        await query.message.reply_text("لغو شد.")
    elif data.startswith("autopost:"):
        _, raw_channel, raw_enabled = data.split(":", 2)
        channel_id = int(raw_channel)
        cfg = publishing_config_payload(channel_id)
        update_publishing_config(
            channel_id,
            enabled=raw_enabled == "1",
            smart_peak_enabled=cfg.get("smart_peak_enabled", True),
            videos_per_day=cfg.get("videos_per_day", 2),
            timezone_name=cfg.get("timezone", "Asia/Tehran"),
            minimum_gap_minutes=cfg.get("minimum_gap_minutes", 180),
            allowed_start_hour=cfg.get("allowed_start_hour", 9),
            allowed_end_hour=cfg.get("allowed_end_hour", 23),
            manual_slots=cfg.get("manual_slots", []),
        )
        await query.message.reply_text("✅ تنظیم Auto Publisher تغییر کرد.")
    elif data.startswith("peaks:"):
        channel_id = int(data.split(":", 1)[1])
        await query.message.reply_text("🧠 در حال تحلیل Peak...")
        try:
            result = await asyncio.to_thread(analyze_peak_slots, channel_id, 90)
            slots = "، ".join(f"{int(x.get('hour',0)):02d}:00" for x in result.get("best_hours", []))
            await query.message.reply_text(f"✅ Peak Slots: {slots or 'داده کافی نیست'}")
        except Exception as exc:
            await query.message.reply_text(f"❌ {exc}")
    elif data.startswith("queuech:"):
        channel_id = int(data.split(":", 1)[1])
        rows = queue_snapshot(channel_id=channel_id, limit=15)
        if not rows:
            await query.message.reply_text("صف این کانال خالی است.")
        else:
            lines = []
            for item in rows:
                local = utc_to_channel_local(item["channel_id"], item["scheduled_for"])
                lines.append(f"#{item['job_id']} · {local:%m-%d %H:%M} · {item['title']}")
            await query.message.reply_text("🕓 صف:\n" + "\n".join(lines))
    elif data.startswith("pubnow:"):
        job_id = int(data.split(":", 1)[1])
        try:
            release_job_now(job_id)
            await query.message.reply_text(f"🚀 Job #{job_id} آزاد شد.")
        except Exception as exc:
            await query.message.reply_text(f"❌ {exc}")
    elif data.startswith("canceljob:"):
        job_id = int(data.split(":", 1)[1])
        try:
            cancel_scheduled_job(job_id)
            await query.message.reply_text(f"✅ Job #{job_id} لغو شد.")
        except Exception as exc:
            await query.message.reply_text(f"❌ {exc}")
    elif data.startswith("video:"):
        _, raw_channel, video_id = data.split(":", 2)
        channel_id = int(raw_channel)
        try:
            video = await asyncio.to_thread(get_video_manager_data, channel_id, video_id)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Public", callback_data=f"vpriv:{channel_id}:{video_id}:public"), InlineKeyboardButton("🔒 Private", callback_data=f"vpriv:{channel_id}:{video_id}:private")],
                [InlineKeyboardButton("💬 Comments", callback_data=f"comments:{channel_id}:{video_id}"), InlineKeyboardButton("🗑 حذف", callback_data=f"delask:{channel_id}:{video_id}")],
                [InlineKeyboardButton("🛠 پنل کامل", url=f"{Config.PUBLIC_BASE_URL}/videos/{channel_id}/{video_id}")],
            ])
            await query.message.reply_text(f"🎬 {video['title']}\n👁 {video['views']:,} · 👍 {video['likes']:,}\n🔐 {video['privacy']}", reply_markup=kb)
        except Exception as exc:
            await query.message.reply_text(f"❌ {exc}")
    elif data.startswith("vpriv:"):
        _, raw_channel, video_id, privacy = data.split(":", 3)
        channel_id = int(raw_channel)
        try:
            video = await asyncio.to_thread(get_video_manager_data, channel_id, video_id)
            await asyncio.to_thread(update_video_metadata, channel_id, video_id, title=video["title"], description=video["description"], tags=video["tags"], privacy=privacy, category_id=video["category_id"], made_for_kids=video["made_for_kids"], embeddable=video["embeddable"])
            await query.message.reply_text(f"✅ Privacy → {privacy}")
        except Exception as exc:
            await query.message.reply_text(f"❌ {exc}")
    elif data.startswith("comments:"):
        _, raw_channel, video_id = data.split(":", 2)
        try:
            payload = await asyncio.to_thread(list_video_comments, int(raw_channel), video_id, "published")
            items = payload.get("items", [])[:6]
            text = "\n\n".join(f"• {x['author']}: {x['text'][:160]}\nID: {x['comment_id']}" for x in items) or "Commentی نیست."
            await query.message.reply_text("💬 " + text)
        except Exception as exc:
            await query.message.reply_text(f"❌ {exc}")
    elif data.startswith("delask:"):
        _, raw_channel, video_id = data.split(":", 2)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 حذف دائمی", callback_data=f"delvid:{raw_channel}:{video_id}"),
            InlineKeyboardButton("لغو", callback_data="noop"),
        ]])
        await query.message.reply_text("⚠️ حذف دائمی ویدیو را تأیید می‌کنی؟", reply_markup=kb)
    elif data.startswith("delvid:"):
        _, raw_channel, video_id = data.split(":", 2)
        try:
            await asyncio.to_thread(delete_video, int(raw_channel), video_id)
            await query.message.reply_text("✅ ویدیو از YouTube حذف شد.")
        except Exception as exc:
            await query.message.reply_text(f"❌ {exc}")
    elif data.startswith("select:"):
        try:
            channel_id = int(data.split(":", 1)[1])
            _set_selected_channel(query.from_user.id, channel_id)
            with SessionLocal() as db:
                channel = db.get(YouTubeChannel, channel_id)
                label = channel.label
            if context.user_data.get("pending_url"):
                context.user_data["channel_id"] = channel_id
                await query.message.reply_text(f"✅ مقصد: {label}\n✏️ حالا عنوان ویدیو را بفرست.")
            else:
                await query.message.reply_text(f"✅ کانال پیش‌فرض ربات: {label}")
        except Exception as exc:
            await query.message.reply_text(f"❌ {exc}")


async def error_handler(_update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Telegram handler error", exc_info=context.error)


def run_bot() -> None:
    init_db()
    token = resolve_telegram_token()
    while not token:
        logger.warning("Telegram bot token is not configured yet; waiting for panel setup")
        time.sleep(10)
        token = resolve_telegram_token()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("claim", claim_command))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("channels", channels_command))
    app.add_handler(CommandHandler("connect", connect_command))
    app.add_handler(CommandHandler("channelinfo", channel_info_command))
    app.add_handler(CommandHandler("uploads", recent_uploads_command))
    app.add_handler(CommandHandler("toggle", toggle_command))
    app.add_handler(CommandHandler("setprivacy", privacy_command))
    app.add_handler(CommandHandler("sethashtags", hashtags_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("smart", smart_command))
    app.add_handler(CommandHandler("autopost", autopost_command))
    app.add_handler(CommandHandler("perday", perday_command))
    app.add_handler(CommandHandler("peaks", peaks_command))
    app.add_handler(CommandHandler("queue", queue_command))
    app.add_handler(CommandHandler("publishnow", publishnow_command))
    app.add_handler(CommandHandler("canceljob", canceljob_command))
    app.add_handler(CommandHandler("videos", videos_command))
    app.add_handler(CommandHandler("video", video_command))
    app.add_handler(CommandHandler("videoprivacy", videoprivacy_command))
    app.add_handler(CommandHandler("videotitle", videotitle_command))
    app.add_handler(CommandHandler("videodesc", videodesc_command))
    app.add_handler(CommandHandler("videotags", videotags_command))
    app.add_handler(CommandHandler("deletevideo", deletevideo_command))
    app.add_handler(CommandHandler("comments", comments_command))
    app.add_handler(CommandHandler("reply", reply_command))
    app.add_handler(CommandHandler("deletecomment", deletecomment_command))
    app.add_handler(CommandHandler("playlists", playlists_command))
    app.add_handler(CommandHandler("newplaylist", newplaylist_command))
    app.add_handler(CommandHandler("addplaylist", addplaylist_command))
    app.add_handler(CommandHandler("thumbnail", thumbnail_command))
    app.add_handler(CommandHandler("caption", caption_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL), handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    logger.info("Telegram bot started")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    run_bot()
