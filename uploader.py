import os
import time
import json
import logging
from typing import Optional
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRET_FILE = os.environ.get("CLIENT_SECRET_FILE", "client_secret.json")
TOKEN_FILE = os.environ.get("YOUTUBE_TOKEN_FILE", "token.json")

logger = logging.getLogger("uploader")

def _get_service_sync():
    """دریافت سرویس یوتیوب با احراز هویت - بدون نیاز به مرورگر"""
    creds = None
    
    # چک کردن توکن ذخیره شده
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            logger.info("✅ توکن از فایل بارگذاری شد")
        except Exception as e:
            logger.warning(f"خطا در بارگذاری توکن: {e}")
    
    # اگر توکن معتبر نیست یا وجود نداره
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("🔄 در حال رفرش توکن...")
                creds.refresh(Request())
                logger.info("✅ توکن رفرش شد")
            except Exception as e:
                logger.warning(f"خطا در رفرش توکن: {e}")
                creds = None
        
        # اگر هنوز توکن نداریم، از flow استفاده کن
        if not creds:
            logger.info("🔑 در حال احراز هویت با گوگل...")
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET_FILE, SCOPES
            )
            
            # اینجا مهمه - بدون مرورگر و با پورت ۰
            try:
                # روش اول: استفاده از local server با پورت ۰
                creds = flow.run_local_server(
                    port=0,  # پورت خالی
                    open_browser=False,  # مرورگر باز نشه
                    prompt='consent',
                    timeout_seconds=300  # ۵ دقیقه وقت داره
                )
                logger.info("✅ احراز هویت موفق")
            except Exception as e:
                logger.error(f"❌ خطا در احراز هویت: {e}")
                raise
        
        # ذخیره توکن برای دفعات بعد
        try:
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
            logger.info(f"✅ توکن در {TOKEN_FILE} ذخیره شد")
        except Exception as e:
            logger.warning(f"⚠️ خطا در ذخیره توکن: {e}")
    
    return build("youtube", "v3", credentials=creds)

def _get_channel_info(service):
    """دریافت اطلاعات کانال یوتیوب"""
    try:
        request = service.channels().list(
            part="snippet",
            mine=True
        )
        response = request.execute()
        if response.get('items'):
            channel = response['items'][0]
            return {
                'id': channel['id'],
                'title': channel['snippet']['title'],
                'custom_url': channel['snippet'].get('customUrl', '')
            }
        return None
    except Exception as e:
        logger.error(f"خطا در دریافت اطلاعات کانال: {e}")
        return None

def upload_to_youtube(file_path: str, hashtags: str, title: Optional[str] = None) -> dict:
    """آپلود ویدیو در یوتیوب شورت"""
    result = {
        'success': False,
        'video_id': None,
        'video_url': None,
        'channel_info': None,
        'error': None
    }
    
    try:
        service = _get_service_sync()
        
        channel_info = _get_channel_info(service)
        if channel_info:
            result['channel_info'] = channel_info
            logger.info(f"📺 آپلود در کانال: {channel_info['title']}")
        
        final_title = (title or "YouTube Shorts").strip()
        full_title = f"{final_title} {hashtags}".strip()[:100]
        
        body = {
            "snippet": {
                "title": full_title,
                "description": f"{hashtags}\n\n#Shorts #YouTubeShorts",
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }
        
        media = MediaFileUpload(file_path, chunksize=-1, resumable=True)
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)
        
        for attempt in range(3):
            try:
                response = request.execute()
                video_id = response.get("id")
                if video_id:
                    result['success'] = True
                    result['video_id'] = video_id
                    result['video_url'] = f"https://youtube.com/shorts/{video_id}"
                    logger.info(f"✅ آپلود شد: {result['video_url']}")
                    return result
                break
            except HttpError as e:
                logger.warning(f"خطای API (تلاش {attempt+1}/3): {e}")
                time.sleep(5)
            except Exception as e:
                logger.warning(f"خطای آپلود (تلاش {attempt+1}/3): {e}")
                time.sleep(5)
        
        result['error'] = "آپلود ناموفق بعد از ۳ بار تلاش"
        return result
        
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"❌ خطا در آپلود: {e}")
        return result
    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"🗑️ فایل موقت حذف شد: {file_path}")
        except Exception as e:
            logger.warning(f"نمی‌توان فایل موقت را حذف کرد: {e}")
