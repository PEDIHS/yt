from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from config import Config
from db import SessionLocal
from models import SystemSecret, TelegramAdmin
from security import decrypt_secret, encrypt_secret


def get_secret(key: str) -> str:
    with SessionLocal() as db:
        row = db.get(SystemSecret, key)
        if not row:
            return ""
        return decrypt_secret(row.value_encrypted)


def set_secret(key: str, value: str) -> None:
    with SessionLocal() as db:
        row = db.get(SystemSecret, key)
        encrypted = encrypt_secret(value)
        if row is None:
            db.add(SystemSecret(key=key, value_encrypted=encrypted))
        else:
            row.value_encrypted = encrypted
            row.updated_at = datetime.utcnow()
        db.commit()


def delete_secret(key: str) -> None:
    with SessionLocal() as db:
        row = db.get(SystemSecret, key)
        if row:
            db.delete(row)
            db.commit()


def resolve_telegram_token() -> str:
    return Config.TELEGRAM_BOT_TOKEN or get_secret("telegram_bot_token")


def telegram_admin_count() -> int:
    with SessionLocal() as db:
        return db.query(TelegramAdmin).count()


def is_telegram_admin(user_id: int) -> bool:
    if user_id in Config.TELEGRAM_ADMIN_IDS:
        return True
    with SessionLocal() as db:
        return db.get(TelegramAdmin, user_id) is not None


def add_telegram_admin(user_id: int) -> None:
    with SessionLocal() as db:
        if db.get(TelegramAdmin, user_id) is None:
            db.add(TelegramAdmin(user_id=user_id))
            db.commit()


def create_claim_code(minutes: int = 15) -> str:
    code = secrets.token_hex(4).upper()
    set_secret("telegram_claim_hash", hashlib.sha256(code.encode()).hexdigest())
    set_secret("telegram_claim_expires", (datetime.utcnow() + timedelta(minutes=minutes)).isoformat())
    return code


def claim_telegram_admin(user_id: int, code: str) -> bool:
    expected = get_secret("telegram_claim_hash")
    expires = get_secret("telegram_claim_expires")
    if not expected or not expires:
        return False
    try:
        if datetime.utcnow() > datetime.fromisoformat(expires):
            return False
    except ValueError:
        return False
    supplied = hashlib.sha256(code.strip().upper().encode()).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        return False
    add_telegram_admin(user_id)
    delete_secret("telegram_claim_hash")
    delete_secret("telegram_claim_expires")
    return True


def validate_telegram_token(token: str) -> dict:
    token = token.strip()
    if not token:
        raise ValueError("Bot token is empty")
    response = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    payload = response.json()
    if response.status_code != 200 or not payload.get("ok"):
        raise ValueError("Telegram rejected this bot token")
    result = payload.get("result", {})
    return {"id": result.get("id"), "username": result.get("username") or ""}


def google_oauth_status() -> dict:
    path = Path(Config.CLIENT_SECRET_FILE)
    if not path.exists():
        return {"configured": False, "type": "missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"configured": False, "type": "invalid"}
    if "web" in payload:
        redirects = payload.get("web", {}).get("redirect_uris", [])
        expected = f"{Config.PUBLIC_BASE_URL}/oauth/callback"
        return {
            "configured": True,
            "type": "web",
            "redirect_ok": expected in redirects,
            "expected_redirect": expected,
        }
    if "installed" in payload:
        return {"configured": False, "type": "installed"}
    return {"configured": False, "type": "invalid"}


def save_google_web_client(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid Google OAuth JSON") from exc
    if "web" not in payload:
        raise ValueError("Google OAuth client must be a Web application client, not Desktop/Installed")
    web = payload["web"]
    if not web.get("client_id") or not web.get("client_secret"):
        raise ValueError("OAuth JSON is missing client_id/client_secret")
    expected = f"{Config.PUBLIC_BASE_URL}/oauth/callback"
    redirects = web.get("redirect_uris", [])
    if expected not in redirects:
        raise ValueError(f"Add this Authorized redirect URI in Google Cloud first: {expected}")
    Path(Config.CLIENT_SECRET_FILE).write_bytes(raw)
    return {"configured": True, "type": "web", "redirect_ok": True}
