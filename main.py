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
from integrations import claim_telegram_admin, is_telegram_admin, resolve_telegram_token
from models import OAuthRequest, TelegramPreference, UploadJob, YouTubeChannel
from security import new_token
from youtube import refresh_channel

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("telegram-bot")


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
        [InlineKeyboardButton("📺 کانال‌ها", callback_data="channels"), InlineKeyboardButton("➕ اتصال کانال", callback_data="connect")],
        [InlineKeyboardButton("📤 ارسال محتوا", callback_data="howto"), InlineKeyboardButton("🧾 ارسال‌های اخیر", callback_data="uploads")],
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


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    context.user_data.clear()
    await update.effective_message.reply_text("لغو شد.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    await update.effective_message.reply_text(
        "/channels — لیست و انتخاب کانال\n"
        "/connect — اتصال کانال جدید با OAuth\n"
        "/channelinfo [id] — اطلاعات کانال\n"
        "/uploads — ارسال‌های اخیر\n"
        "/toggle id — فعال/غیرفعال کردن کانال\n"
        "/setprivacy id public|unlisted|private\n"
        "/sethashtags id #tag...\n"
        "/refresh [id] — بروزرسانی آمار YouTube\n"
        "/cancel — لغو عملیات جاری"
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
        await update.effective_message.reply_text(f"🕓 Job #{job.id} ثبت شد. دانلود و آپلود در حال انجام است.")
        context.application.create_task(_run_job_and_notify(context, update.effective_chat.id, job.id))
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
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    logger.info("Telegram bot started")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    run_bot()
