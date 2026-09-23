from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func
from werkzeug.middleware.proxy_fix import ProxyFix

from analytics import (
    ALLOWED_PERIODS,
    get_cached_analytics,
    get_or_sync_channel_analytics,
    normalize_period,
    sync_channel_analytics,
)
from config import Config
from db import SessionLocal, init_db
from downloader import is_supported_instagram_url
from jobs import create_job, enqueue_job
from integrations import (
    add_telegram_admin,
    build_instagram_cookie_blob,
    create_claim_code,
    get_secret,
    google_oauth_status,
    instagram_status,
    parse_instagram_cookie_blob,
    save_google_web_client,
    validate_instagram_session,
    set_secret,
    delete_secret,
    telegram_admin_count,
    validate_telegram_token,
)
from models import AuditLog, OAuthRequest, UploadJob, YouTubeChannel
from security import credentials_match, csrf_token, login_required, require_csrf
from youtube import (
    build_authorization_url,
    channel_authorization_state,
    connect_channel,
    oauth_flow,
    refresh_channel,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("panel")

Config.validate_panel()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = Config.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=Config.PUBLIC_BASE_URL.startswith("https://"),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)
init_db()


def _compact_number(value) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    absolute = abs(number)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if absolute >= divisor:
            result = number / divisor
            return f"{result:.1f}{suffix}".replace(".0", "")
    return f"{int(number):,}"


def _duration(value) -> str:
    try:
        seconds = int(round(float(value or 0)))
    except (TypeError, ValueError):
        seconds = 0
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


@app.context_processor
def inject_globals():
    return {
        "csrf_token": csrf_token,
        "public_base_url": Config.PUBLIC_BASE_URL,
        "compact_number": _compact_number,
        "format_duration": _duration,
        "analytics_periods": sorted(ALLOWED_PERIODS),
    }


def _audit(actor: str, action: str, details: str = "") -> None:
    with SessionLocal() as db:
        db.add(AuditLog(actor=actor[:120], action=action[:120], details=details[:4000]))
        db.commit()


def _oauth_redirect_uri() -> str:
    return f"{Config.PUBLIC_BASE_URL}/oauth/callback"


def _dashboard_analytics(channels: list[YouTubeChannel], days: int):
    analytics_map = {}
    coverage = 0
    summary = {
        "views": 0,
        "likes": 0,
        "comments": 0,
        "shares": 0,
        "subscribers_gained": 0,
        "subscribers_lost": 0,
        "subscribers_net": 0,
        "watch_minutes": 0,
    }
    previous_summary = {key: 0 for key in summary}
    all_dates: set[str] = set()
    channel_daily_maps: dict[int, dict[str, dict]] = {}

    for channel in channels:
        payload = get_cached_analytics(channel.id, days)
        analytics_map[channel.id] = payload
        if not payload:
            continue
        coverage += 1
        row = payload.get("summary", {})
        previous_row = payload.get("previous_summary", {})
        for key in summary:
            summary[key] += int(row.get(key, 0) or 0)
            previous_summary[key] += int(previous_row.get(key, 0) or 0)
        daily_map = {item.get("date"): item for item in payload.get("daily", []) if item.get("date")}
        channel_daily_maps[channel.id] = daily_map
        all_dates.update(daily_map.keys())

    dates = sorted(all_dates)
    series = []
    for channel in channels:
        daily_map = channel_daily_maps.get(channel.id, {})
        if not daily_map:
            continue
        series.append({
            "name": channel.label,
            "data": [int(daily_map.get(day, {}).get("views", 0) or 0) for day in dates],
        })

    distribution = [
        {
            "name": channel.label,
            "value": int((analytics_map.get(channel.id) or {}).get("summary", {}).get("views", 0) or 0),
        }
        for channel in channels
        if analytics_map.get(channel.id)
    ]
    distribution.sort(key=lambda item: item["value"], reverse=True)

    def pct(current, previous):
        previous = float(previous or 0)
        if previous == 0:
            return None
        return round(((float(current or 0) - previous) / abs(previous)) * 100, 1)

    changes = {
        "views": pct(summary["views"], previous_summary["views"]),
        "likes": pct(summary["likes"], previous_summary["likes"]),
        "comments": pct(summary["comments"], previous_summary["comments"]),
        "shares": pct(summary["shares"], previous_summary["shares"]),
        "watch_minutes": pct(summary["watch_minutes"], previous_summary["watch_minutes"]),
        "subscribers_net_delta": summary["subscribers_net"] - previous_summary["subscribers_net"],
    }

    return analytics_map, coverage, summary, changes, {"dates": dates, "series": series}, distribution


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
    days = normalize_period(request.args.get("days"))
    with SessionLocal() as db:
        channel_count = db.query(func.count(YouTubeChannel.id)).scalar() or 0
        active_channels = db.query(func.count(YouTubeChannel.id)).filter(YouTubeChannel.is_active.is_(True)).scalar() or 0
        completed = db.query(func.count(UploadJob.id)).filter(UploadJob.status == "completed").scalar() or 0
        failed = db.query(func.count(UploadJob.id)).filter(UploadJob.status == "failed").scalar() or 0
        channels = db.query(YouTubeChannel).order_by(YouTubeChannel.id.asc()).all()
        channel_map = {channel.id: channel for channel in channels}
        recent_jobs = db.query(UploadJob).order_by(UploadJob.id.desc()).limit(8).all()

    analytics_map, coverage, period_summary, period_changes, global_chart, distribution = _dashboard_analytics(channels, days)
    lifetime = {
        "views": sum(int(channel.view_count or 0) for channel in channels),
        "subscribers": sum(int(channel.subscriber_count or 0) for channel in channels),
        "videos": sum(int(channel.video_count or 0) for channel in channels),
    }

    return render_template(
        "dashboard.html",
        channel_count=channel_count,
        active_channels=active_channels,
        completed=completed,
        failed=failed,
        channels=channels,
        channel_map=channel_map,
        recent_jobs=recent_jobs,
        selected_days=days,
        analytics_map=analytics_map,
        analytics_coverage=coverage,
        period_summary=period_summary,
        period_changes=period_changes,
        global_chart=global_chart,
        distribution=distribution,
        lifetime=lifetime,
    )


@app.post("/analytics/sync-all")
@login_required
def sync_all_analytics():
    require_csrf()
    days = normalize_period(request.form.get("days"))
    with SessionLocal() as db:
        channel_ids = [
            channel.id
            for channel in db.query(YouTubeChannel).filter(YouTubeChannel.is_active.is_(True)).all()
        ]

    if not channel_ids:
        flash("کانال فعالی برای همگام‌سازی وجود ندارد.", "warning")
        return redirect(url_for("dashboard", days=days))

    success = 0
    failures = []
    workers = min(4, len(channel_ids))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="analytics-sync") as executor:
        futures = {executor.submit(sync_channel_analytics, channel_id, days): channel_id for channel_id in channel_ids}
        for future in as_completed(futures):
            channel_id = futures[future]
            try:
                future.result()
                success += 1
            except Exception as exc:
                failures.append(f"#{channel_id}: {exc}")

    _audit("panel", "analytics_sync_all", f"days={days}; success={success}; failed={len(failures)}")
    if success:
        flash(f"آمار {success} کانال برای {days} روز بروزرسانی شد.", "success")
    if failures:
        flash(
            "بعضی کانال‌ها همگام نشدند؛ احتمالاً باید OAuth آن‌ها با دسترسی Analytics دوباره متصل شود. "
            + " | ".join(failures[:3]),
            "warning",
        )
    return redirect(url_for("dashboard", days=days))



@app.get("/integrations")
@login_required
def integrations_page():
    google_status = google_oauth_status()
    instagram = instagram_status()
    bot_configured = bool(get_secret("telegram_bot_token") or Config.TELEGRAM_BOT_TOKEN)
    bot_username = get_secret("telegram_bot_username")
    primary_admin_id = get_secret("telegram_primary_admin_id")
    admin_count = telegram_admin_count() + len(Config.TELEGRAM_ADMIN_IDS)
    claim_code = session.pop("telegram_claim_code", None)
    return render_template(
        "integrations.html",
        google_status=google_status,
        instagram=instagram,
        bot_configured=bot_configured,
        bot_username=bot_username,
        primary_admin_id=primary_admin_id,
        admin_count=admin_count,
        claim_code=claim_code,
    )


@app.post("/integrations/telegram")
@login_required
def integrations_telegram():
    require_csrf()
    token = request.form.get("bot_token", "").strip()
    admin_raw = request.form.get("primary_admin_id", "").strip()
    try:
        current_token = get_secret("telegram_bot_token") or Config.TELEGRAM_BOT_TOKEN
        effective_token = token or current_token
        if not effective_token:
            raise ValueError("Bot Token وارد نشده است")
        info = validate_telegram_token(effective_token)
        if token:
            set_secret("telegram_bot_token", token)
        set_secret("telegram_bot_username", info.get("username") or "")
        if not admin_raw.isdigit():
            raise ValueError("آیدی ادمین باید فقط عدد باشد")
        admin_id = int(admin_raw)
        if admin_id <= 0:
            raise ValueError("آیدی ادمین معتبر نیست")
        add_telegram_admin(admin_id)
        set_secret("telegram_primary_admin_id", str(admin_id))
        _audit("panel", "telegram_bot_configured", f"bot_id={info.get('id')}; primary_admin_id={admin_id}")
        flash(f"Telegram Bot @{info.get('username') or 'configured'} متصل شد و ادمین اصلی ثبت شد.", "success")
    except Exception as exc:
        flash(f"تنظیم Telegram ذخیره نشد: {exc}", "danger")
    return redirect(url_for("integrations_page"))


@app.post("/integrations/telegram/claim-code")
@login_required
def integrations_claim_code():
    require_csrf()
    if not (get_secret("telegram_bot_token") or Config.TELEGRAM_BOT_TOKEN):
        flash("ابتدا Bot Token را ذخیره کن.", "warning")
        return redirect(url_for("integrations_page"))
    code = create_claim_code(15)
    session["telegram_claim_code"] = code
    _audit("panel", "telegram_claim_code_created")
    return redirect(url_for("integrations_page"))


@app.post("/integrations/instagram")
@login_required
def integrations_instagram():
    require_csrf()
    sessionid = request.form.get("sessionid", "").strip()
    csrftoken = request.form.get("csrftoken", "").strip()
    ds_user_id = request.form.get("ds_user_id", "").strip()
    uploaded = request.files.get("cookies_file")

    try:
        if uploaded and uploaded.filename:
            raw = uploaded.read(256 * 1024).decode("utf-8", errors="strict")
            cookies = parse_instagram_cookie_blob(raw)
            sessionid = cookies.get("sessionid", "")
            csrftoken = cookies.get("csrftoken", "")
            ds_user_id = cookies.get("ds_user_id", "")

        info = validate_instagram_session(sessionid, csrftoken, ds_user_id)
        cookie_blob = build_instagram_cookie_blob(sessionid, csrftoken, ds_user_id)

        set_secret("instagram_sessionid", sessionid)
        set_secret("instagram_cookie_blob", cookie_blob)
        set_secret("instagram_username", info.get("username") or "")
        set_secret("instagram_user_id", info.get("user_id") or "")

        _audit("panel", "instagram_connected", f"username={info.get('username') or ''}; verified={info.get('verified')}")
        if info.get("username"):
            flash(f"Instagram @{info.get('username')} با موفقیت متصل شد.", "success")
        else:
            flash("کوکی‌های Instagram ذخیره شدند. تأیید نهایی هنگام اولین دانلود Reel/Post انجام می‌شود.", "success")
    except Exception as exc:
        flash(f"اتصال Instagram ناموفق بود: {exc}", "danger")
    return redirect(url_for("integrations_page"))


@app.post("/integrations/instagram/disconnect")
@login_required
def integrations_instagram_disconnect():
    require_csrf()
    delete_secret("instagram_sessionid")
    delete_secret("instagram_cookie_blob")
    delete_secret("instagram_username")
    delete_secret("instagram_user_id")
    _audit("panel", "instagram_disconnected")
    flash("اتصال Instagram حذف شد.", "success")
    return redirect(url_for("integrations_page"))


@app.post("/integrations/google")
@login_required
def integrations_google():
    require_csrf()
    uploaded = request.files.get("client_secret")
    if not uploaded or not uploaded.filename:
        flash("فایل JSON انتخاب نشده است.", "warning")
        return redirect(url_for("integrations_page"))
    raw = uploaded.read(256 * 1024)
    try:
        save_google_web_client(raw)
        _audit("panel", "google_oauth_client_updated")
        flash("Google Web OAuth Client ذخیره شد و Redirect URI معتبر است.", "success")
    except Exception as exc:
        flash(f"Google OAuth ذخیره نشد: {exc}", "danger")
    return redirect(url_for("integrations_page"))


@app.get("/channels")
@login_required
def channels():
    with SessionLocal() as db:
        rows = db.query(YouTubeChannel).order_by(YouTubeChannel.id.asc()).all()

    analytics_map = {channel.id: get_cached_analytics(channel.id, 28) for channel in rows}
    authorization = {}
    for channel in rows:
        try:
            authorization[channel.id] = channel_authorization_state(channel.id)
        except Exception as exc:
            authorization[channel.id] = {"analytics": False, "error": str(exc)}

    return render_template(
        "channels.html",
        channels=rows,
        analytics_map=analytics_map,
        authorization=authorization,
        google_status=google_oauth_status(),
    )


@app.post("/channels/connect")
@login_required
def channels_connect():
    require_csrf()
    label = request.form.get("label", "").strip()[:120]
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
        try:
            sync_channel_analytics(channel.id, 28)
            flash(f"کانال «{channel.title}» متصل شد؛ پروفایل، Subscribers، Views، Videos و Analytics همگام شدند.", "success")
            analytics_synced = True
        except Exception as analytics_exc:
            logger.warning("Initial analytics sync failed for channel %s: %s", channel.id, analytics_exc)
            flash(f"کانال «{channel.title}» متصل شد و اطلاعات پروفایل دریافت شد؛ Analytics را می‌توانی بعداً Sync کنی.", "warning")
            analytics_synced = False
        session.pop("oauth_state", None)
        session.pop("oauth_label", None)
        session.pop("oauth_mode", None)
        _audit("panel", "channel_connected", f"channel_id={channel.id}; analytics_synced={analytics_synced}")
        return redirect(url_for("channels"))
    except Exception as exc:
        logger.exception("OAuth callback failed")
        return render_template("oauth_error.html", message=str(exc)), 400


@app.route("/channels/<int:channel_id>", methods=["GET", "POST"])
@login_required
def channel_detail(channel_id: int):
    days = normalize_period(request.args.get("days"))
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
            return redirect(url_for("channel_detail", channel_id=channel_id, days=days))

    analytics, analytics_error = get_or_sync_channel_analytics(channel_id, days, max_age_minutes=30)
    try:
        authorization = channel_authorization_state(channel_id)
    except Exception as exc:
        authorization = {"analytics": False, "error": str(exc)}

    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)

    return render_template(
        "channel_detail.html",
        channel=channel,
        analytics=analytics,
        analytics_error=analytics_error,
        authorization=authorization,
        selected_days=days,
    )


@app.post("/channels/<int:channel_id>/analytics/sync")
@login_required
def channel_analytics_sync(channel_id: int):
    require_csrf()
    days = normalize_period(request.form.get("days"))
    try:
        sync_channel_analytics(channel_id, days)
        _audit("panel", "channel_analytics_sync", f"channel_id={channel_id}; days={days}")
        flash(f"Analytics کانال برای {days} روز بروزرسانی شد.", "success")
    except Exception as exc:
        flash(f"همگام‌سازی Analytics ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_detail", channel_id=channel_id, days=days))


@app.post("/channels/<int:channel_id>/refresh")
@login_required
def channel_refresh(channel_id: int):
    require_csrf()
    days = normalize_period(request.form.get("days"))
    try:
        refresh_channel(channel_id)
        flash("اطلاعات پایه کانال از YouTube بروزرسانی شد.", "success")
    except Exception as exc:
        flash(f"خطا در همگام‌سازی: {exc}", "danger")
    return redirect(url_for("channel_detail", channel_id=channel_id, days=days))


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
        channels_list = (
            db.query(YouTubeChannel)
            .filter(YouTubeChannel.is_active.is_(True))
            .order_by(YouTubeChannel.label)
            .all()
        )
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


@app.get("/api/channels/<int:channel_id>/analytics")
@login_required
def api_channel_analytics(channel_id: int):
    days = normalize_period(request.args.get("days"))
    payload = get_cached_analytics(channel_id, days)
    if not payload:
        return jsonify({"error": "analytics_not_synced", "days": days}), 404
    return jsonify(payload)


if __name__ == "__main__":
    app.run(host=Config.PANEL_HOST, port=Config.PANEL_PORT, debug=False)
