import base64
import hashlib
import hmac
import secrets
from functools import wraps
from typing import Callable

from cryptography.fernet import Fernet
from flask import abort, redirect, request, session, url_for

from config import Config


def _fernet_key() -> bytes:
    if Config.TOKEN_ENCRYPTION_KEY:
        return Config.TOKEN_ENCRYPTION_KEY.encode("utf-8")
    digest = hashlib.sha256(Config.SECRET_KEY.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_fernet_key())


def encrypt_secret(value: str) -> str:
    return _fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    return _fernet.decrypt(value.encode("utf-8")).decode("utf-8")


def new_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def login_required(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("panel_authenticated"):
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def credentials_match(username: str, password: str) -> bool:
    return hmac.compare_digest(username, Config.PANEL_USERNAME) and hmac.compare_digest(password, Config.PANEL_PASSWORD)


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def require_csrf() -> None:
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, "Invalid CSRF token")
