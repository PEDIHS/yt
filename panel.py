from __future__ import annotations

import logging
from datetime import datetime

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func

from config import Config
from db import SessionLocal, init_db
from downloader import is_supported_instagram_url
from jobs import create_job, enqueue_job
from models import AuditLog, OAuthRequest, UploadJob, YouTubeChannel
from security import credentials_match, csrf_token, login_required, require_csrf
from youtube import connect_channel, oauth_flow, build_authorization_url, refresh_channel

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("panel")

Config.validate_panel()

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=Config.PUBLIC_BASE_URL.startswith("https://"),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)
init_db()


@app.context_processor
def inject_globals():
    return {"csrf_token": csrf_token, "public_base_url": Config.PUBLIC_BASE_URL}


def _audit(actor: str, action: str, details: str = "") -> None:
    with SessionLocal() as db:
        db.add(AuditLog(actor=actor[:120], action=action[:120], details=details[:4000]))
        db.commit()


def _oauth_redirect_uri() -> str:
    return f"{Config.PUBLIC_BASE_URL}/oauth/callback"


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "yt-panel"})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        require_csrf()
        if credentials_match(request.form.get("username", ""), request.form.get("password", "")):
            session.clear()
            session["panel_authenticated"] = True
            csrf_token()
            _audit("panel", "login", request.remote_addr or "")
            return redirect(url_for("dashboard"))
        flash("نام کاربری یا رمز عبور اشتباه است.", "danger")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    require_csrf()
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def dashboard():
    with SessionLocal() as db:
        channel_count = db.query(func.count(YouTubeChannel.id)).scalar() or 0
        active_channels = db.query(func.count(YouTubeChannel.id)).filter(YouTubeChannel.is_active.is_(True)).scalar() or 0
        completed = db.query(func.count(UploadJob.id)).filter(UploadJob.status == "completed").scalar() or 0
        failed = db.query(func.count(UploadJob.id)).filter(UploadJob.status == "failed").scalar() or 0
        channels = db.query(YouTubeChannel).order_by(YouTubeChannel.id.asc()).all()
        recent_jobs = db.query(UploadJob).order_by(UploadJob.id.desc()).limit(10).all()
    return render_template(
        "dashboard.html",
        channel_count=channel_count,
        active_channels=active_channels,
        completed=completed,
        failed=failed,
        channels=channels,
        recent_jobs=recent_jobs,
    )


@app.get("/channels")
@login_required
def channels():
    with SessionLocal() as db:
        rows = db.query(YouTubeChannel).order_by(YouTubeChannel.id.asc()).all()
    return render_template("channels.html", channels=rows)


@app.post("/channels/connect")
@login_required
def channels_connect():
    require_csrf()
    label = request.form.get("label", "").strip()[:120] or "YouTube Channel"
    url, state = build_authorization_url(_oauth_redirect_uri())
    session["oauth_state"] = state
    session["oauth_label"] = label
    session["oauth_mode"] = "panel"
    return redirect(url)


@app.get("/telegram/connect/<token>")
def telegram_connect(token: str):
    with SessionLocal() as db:
        req = db.query(OAuthRequest).filter_by(token=token).one_or_none()
        if not req or req.used_at is not None or req.expires_at < datetime.utcnow():
            return render_template("oauth_error.html", message="این لینک اتصال معتبر نیست یا منقضی شده است."), 400
        label = req.label
    url, state = build_authorization_url(_oauth_redirect_uri())
    session["oauth_state"] = state
    session["oauth_label"] = label
    session["oauth_mode"] = "telegram"
    session["oauth_request_token"] = token
    return redirect(url)


@app.get("/oauth/callback")
def oauth_callback():
    expected_state = session.get("oauth_state")
    if not expected_state or request.args.get("state") != expected_state:
        return render_template("oauth_error.html", message="OAuth state نامعتبر است."), 400
    try:
        flow = oauth_flow(_oauth_redirect_uri(), state=expected_state)
        flow.fetch_token(authorization_response=request.url)
        channel = connect_channel(flow.credentials, session.get("oauth_label", "YouTube Channel"))
        mode = session.get("oauth_mode")
        if mode == "telegram":
            token = session.get("oauth_request_token")
            with SessionLocal() as db:
                req = db.query(OAuthRequest).filter_by(token=token).one_or_none()
                if req:
                    req.used_at = datetime.utcnow()
                    db.commit()
            message = f"کانال {channel.title} با موفقیت متصل شد. حالا به Telegram برگرد و /channels را بزن."
            session.pop("oauth_request_token", None)
            session.pop("oauth_state", None)
            return render_template("oauth_success.html", channel=channel, message=message)
        flash(f"کانال «{channel.title}» متصل شد.", "success")
        _audit("panel", "channel_connected", f"channel_id={channel.id}")
        return redirect(url_for("channels"))
    except Exception as exc:
        logger.exception("OAuth callback failed")
        return render_template("oauth_error.html", message=str(exc)), 400


@app.route("/channels/<int:channel_id>", methods=["GET", "POST"])
@login_required
def channel_detail(channel_id: int):
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            return "Channel not found", 404
        if request.method == "POST":
            require_csrf()
            privacy = request.form.get("default_privacy", "public")
            if privacy not in {"public", "unlisted", "private"}:
                privacy = "public"
            channel.label = (request.form.get("label", channel.label).strip() or channel.title)[:120]
            channel.default_hashtags = request.form.get("default_hashtags", "").strip()[:2000]
            channel.default_privacy = privacy
            channel.is_active = request.form.get("is_active") == "on"
            db.commit()
            _audit("panel", "channel_updated", f"channel_id={channel_id}")
            flash("تنظیمات کانال ذخیره شد.", "success")
            return redirect(url_for("channel_detail", channel_id=channel_id))
    return render_template("channel_detail.html", channel=channel)


@app.post("/channels/<int:channel_id>/refresh")
@login_required
def channel_refresh(channel_id: int):
    require_csrf()
    try:
        refresh_channel(channel_id)
        flash("اطلاعات کانال از YouTube همگام شد.", "success")
    except Exception as exc:
        flash(f"خطا در همگام‌سازی: {exc}", "danger")
    return redirect(url_for("channel_detail", channel_id=channel_id))


@app.post("/channels/<int:channel_id>/delete")
@login_required
def channel_delete(channel_id: int):
    require_csrf()
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            return "Channel not found", 404
        job_count = db.query(func.count(UploadJob.id)).filter(UploadJob.channel_id == channel_id).scalar() or 0
        if job_count:
            channel.is_active = False
            db.commit()
            flash("به‌دلیل وجود تاریخچه، کانال حذف نشد و فقط غیرفعال شد.", "warning")
        else:
            db.delete(channel)
            db.commit()
            flash("کانال حذف شد.", "success")
    _audit("panel", "channel_delete_or_disable", f"channel_id={channel_id}")
    return redirect(url_for("channels"))


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    with SessionLocal() as db:
        channels_list = db.query(YouTubeChannel).filter(YouTubeChannel.is_active.is_(True)).order_by(YouTubeChannel.label).all()
    if request.method == "POST":
        require_csrf()
        source_url = request.form.get("source_url", "").strip()
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        try:
            channel_id = int(request.form.get("channel_id", "0"))
        except ValueError:
            channel_id = 0
        if not is_supported_instagram_url(source_url):
            flash("لینک Instagram معتبر نیست.", "danger")
        elif not title:
            flash("عنوان الزامی است.", "danger")
        else:
            try:
                job = create_job(
                    channel_id=channel_id,
                    source_url=source_url,
                    title=title,
                    description=description,
                    hashtags=request.form.get("hashtags") or None,
                    privacy=request.form.get("privacy") or None,
                    source="panel",
                )
                enqueue_job(job.id)
                _audit("panel", "upload_queued", f"job_id={job.id}; channel_id={channel_id}")
                flash(f"Job #{job.id} وارد صف شد.", "success")
                return redirect(url_for("jobs"))
            except Exception as exc:
                flash(f"ثبت ارسال ناموفق بود: {exc}", "danger")
    return render_template("upload.html", channels=channels_list)


@app.get("/jobs")
@login_required
def jobs():
    status = request.args.get("status", "").strip()
    with SessionLocal() as db:
        query = db.query(UploadJob)
        if status in {"queued", "downloading", "uploading", "completed", "failed"}:
            query = query.filter(UploadJob.status == status)
        rows = query.order_by(UploadJob.id.desc()).limit(Config.MAX_UPLOAD_HISTORY).all()
        channel_map = {c.id: c for c in db.query(YouTubeChannel).all()}
    return render_template("jobs.html", jobs=rows, channel_map=channel_map, selected_status=status)


@app.get("/api/jobs/<int:job_id>")
@login_required
def api_job(job_id: int):
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "id": job.id,
            "status": job.status,
            "video_url": job.video_url,
            "error": job.error,
            "created_at": job.created_at.isoformat(),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        })


if __name__ == "__main__":
    app.run(host=Config.PANEL_HOST, port=Config.PANEL_PORT, debug=False)
