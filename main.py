import os
import logging
import time
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import NetworkError, TimedOut

from downloader import download_video
from uploader import upload_to_youtube

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_HASHTAGS = os.getenv("DEFAULT_HASHTAGS", "#Shorts #YouTubeShorts #reels")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

user_states = {}

async def safe_reply(update: Update, text: str, parse_mode=None):
    try:
        await update.message.reply_text(text, parse_mode=parse_mode)
    except (NetworkError, TimedOut) as e:
        logger.warning(f"Reply failed: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, "👋 سلام! لینک ریل اینستاگرام رو برام بفرست.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user_id = update.message.from_user.id
    message = update.message.text.strip()

    if "instagram.com/reel/" in message or "instagram.com/p/" in message:
        user_states[user_id] = {"url": message}
        preview = "\n".join(DEFAULT_HASHTAGS.split()[:20])
        await safe_reply(
            update,
            f"✅ لینک دریافت شد!\n\n📌 هشتگ‌ها:\n{preview}\n\n✏️ حالا *عنوان* ویدیو رو برام بفرست.",
            parse_mode="Markdown",
        )
        return

    if user_id in user_states and "url" in user_states[user_id] and "title" not in user_states[user_id]:
        user_states[user_id]["title"] = message
        url = user_states[user_id]["url"]
        title = user_states[user_id]["title"]
        hashtags = DEFAULT_HASHTAGS

        await safe_reply(update, "📥 در حال دانلود و آپلود... لطفاً صبر کن.")

        filename = download_video(url)
        if not filename:
            await safe_reply(update, "❌ دانلود ویدیو ناموفق بود.")
            user_states.pop(user_id, None)
            return

        result = upload_to_youtube(filename, hashtags, title)
        
        if result.get('success'):
            video_url = result.get('video_url')
            channel_info = result.get('channel_info')
            
            reply = "✅ **ویدیو با موفقیت آپلود شد!**\n\n"
            reply += f"🔗 **لینک شورت:** {video_url}\n\n"
            
            if channel_info:
                reply += f"📺 **کانال:** {channel_info.get('title', 'نامشخص')}\n"
                if channel_info.get('custom_url'):
                    reply += f"🔗 **آدرس کانال:** https://youtube.com/{channel_info.get('custom_url')}\n"
            
            reply += f"🎬 **عنوان:** {title}\n"
            reply += f"🏷️ **هشتگ‌ها:** {hashtags}"
            
            await safe_reply(update, reply, parse_mode="Markdown")
        else:
            error = result.get('error', 'خطای ناشناخته')
            await safe_reply(update, f"❌ آپلود ناموفق بود!\nخطا: {error}")

        user_states.pop(user_id, None)
        return

    await safe_reply(update, "❓ لطفاً یک لینک اینستاگرام (ریل یا پست) ارسال کن.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception in handler", exc_info=context.error)

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in environment.")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    print("🤖 ربات روشن شد!")
    application.run_polling()

if __name__ == "__main__":
    main()
