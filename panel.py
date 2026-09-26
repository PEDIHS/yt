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
from config import Config, DATA_DIR
from db import SessionLocal, init_db
from downloader import is_supported_external_url, is_supported_instagram_url, probe_external_video
from jobs import (
    configure_long_job,
    create_job,
    enqueue_job,
    mark_job_failed,
    resume_reauth_jobs_for_channel,
)
from publishing import (
    analyze_peak_slots,
    cancel_scheduled_job,
    local_datetime_to_utc,
    publishing_config_payload,
    queue_snapshot,
    release_job_now,
    reschedule_channel_queue,
    schedule_job_manual,
    schedule_job_smart,
    update_publishing_config,
    utc_to_channel_local,
)
from instagram_direct import (
    list_instagram_group_routes,
    set_instagram_group_route,
    sync_instagram_groups,
)
from integrations import (
    add_telegram_admin,
    build_instagram_cookie_blob,
    create_claim_code,
    get_secret,
    google_oauth_status,
    instagram_direct_health,
    instagram_status,
    normalize_instagram_cookie_blob,
    parse_instagram_cookie_blob,
    save_google_web_client,
    validate_instagram_session,
    set_secret,
    delete_secret,
    telegram_admin_count,
    validate_telegram_token,
)
from models import AuditLog, OAuthRequest, UploadJob, UploadJobOption, UploadSchedule, YouTubeChannel
from missions import build_channel_mission, set_mission_settings
from security import credentials_match, csrf_token, login_required, require_csrf
from youtube import (
    add_video_to_playlist,
    build_authorization_url,
    channel_authorization_state,
    connect_channel,
    connect_channels,
    discover_related_channels,
    create_playlist,
    create_channel_section,
    delete_caption,
    delete_channel_localization,
    delete_channel_section,
    delete_comment,
    delete_playlist,
    delete_video,
    get_channel_studio_data,
    get_video_manager_data,
    list_channel_playlists,
    list_channel_sections,
    list_channel_videos,
    list_video_captions,
    list_video_comments,
    moderate_comment,
    oauth_flow,
    refresh_channel,
    remove_channel_watermark,
    reply_to_comment,
    set_channel_watermark,
    set_video_thumbnail,
    update_channel_audience,
    update_channel_branding,
    update_channel_localization,
    update_channel_section,
    update_playlist,
    update_video_metadata,
    upload_caption,
    upload_channel_banner,
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
    MAX_CONTENT_LENGTH=105 * 1024 * 1024,
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
    audience_totals = {
        "countries": {},
        "traffic_sources": {},
        "devices": {},
        "subscribed_status": {},
        "content_types": {},
    }

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

        audience = payload.get("audience", {}) or {}
        audience_fields = {
            "countries": ("country", "views"),
            "traffic_sources": ("source", "views"),
            "devices": ("device", "views"),
            "subscribed_status": ("status", "views"),
            "content_types": ("type", "views"),
        }
        for group, (name_key, value_key) in audience_fields.items():
            for item in audience.get(group, []) or []:
                name = str(item.get(name_key) or "Unknown")
                audience_totals[group][name] = audience_totals[group].get(name, 0) + int(item.get(value_key, 0) or 0)

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

    audience_rollup = {}
    for group, values in audience_totals.items():
        ranked = sorted(values.items(), key=lambda item: item[1], reverse=True)
        audience_rollup[group] = [{"name": name, "value": value} for name, value in ranked[:10]]

    return analytics_map, coverage, summary, changes, {"dates": dates, "series": series}, distribution, audience_rollup


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "yt-panel"})


@app.get("/about")
def public_about():
    return render_template("public_info.html", page="about")


@app.get("/privacy")
def public_privacy():
    return render_template("public_info.html", page="privacy")


@app.get("/terms")
def public_terms():
    return render_template("public_info.html", page="terms")


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
        operational = {
            "scheduled": db.query(func.count(UploadSchedule.id)).filter(
                UploadSchedule.status.in_(["waiting", "releasing"])
            ).scalar() or 0,
            "inflight": db.query(func.count(UploadJob.id)).filter(
                UploadJob.status.in_(["preparing", "downloading", "uploading", "checking"])
            ).scalar() or 0,
            "ready": db.query(func.count(UploadJob.id)).filter(
                UploadJob.status == "ready_scheduled"
            ).scalar() or 0,
            "copyright_blocked": db.query(func.count(UploadJob.id)).filter(
                UploadJob.status == "copyright_blocked"
            ).scalar() or 0,
            "reauth_required": db.query(func.count(UploadJob.id)).filter(
                UploadJob.status == "reauth_required"
            ).scalar() or 0,
        }

    analytics_map, coverage, period_summary, period_changes, global_chart, distribution, audience_rollup = _dashboard_analytics(channels, days)
    channel_ranking = []
    for channel in channels:
        payload = analytics_map.get(channel.id) or {}
        row = payload.get("summary", {}) or {}
        channel_ranking.append({
            "channel": channel,
            "views": int(row.get("views", 0) or 0),
            "likes": int(row.get("likes", 0) or 0),
            "comments": int(row.get("comments", 0) or 0),
            "subscribers_net": int(row.get("subscribers_net", 0) or 0),
            "watch_minutes": int(row.get("watch_minutes", 0) or 0),
        })
    channel_ranking.sort(
        key=lambda item: (item["views"], item["likes"], item["subscribers_net"]),
        reverse=True,
    )
    for index, item in enumerate(channel_ranking, start=1):
        item["rank"] = index

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
        audience_rollup=audience_rollup,
        lifetime=lifetime,
        operational=operational,
        channel_ranking=channel_ranking,
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
    instagram_direct = instagram_direct_health() if instagram.get("configured") else {"ok": False, "status": "not_configured"}
    bot_configured = bool(get_secret("telegram_bot_token") or Config.TELEGRAM_BOT_TOKEN)
    bot_username = get_secret("telegram_bot_username")
    primary_admin_id = get_secret("telegram_primary_admin_id")
    admin_count = telegram_admin_count() + len(Config.TELEGRAM_ADMIN_IDS)
    claim_code = session.pop("telegram_claim_code", None)
    with SessionLocal() as db:
        instagram_route_channels = (
            db.query(YouTubeChannel)
            .filter(YouTubeChannel.is_active.is_(True))
            .order_by(YouTubeChannel.label.asc())
            .all()
        )
    instagram_groups = list_instagram_group_routes() if instagram.get("configured") else []
    return render_template(
        "integrations.html",
        google_status=google_status,
        instagram=instagram,
        instagram_direct=instagram_direct,
        instagram_groups=instagram_groups,
        instagram_route_channels=instagram_route_channels,
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
        uploaded_cookie_blob = ""
        if uploaded and uploaded.filename:
            raw = uploaded.read(256 * 1024).decode("utf-8", errors="strict")
            cookies = parse_instagram_cookie_blob(raw)
            uploaded_cookie_blob = normalize_instagram_cookie_blob(raw)
            sessionid = cookies.get("sessionid", "")
            csrftoken = cookies.get("csrftoken", "")
            ds_user_id = cookies.get("ds_user_id", "")

        info = validate_instagram_session(sessionid, csrftoken, ds_user_id)
        cookie_blob = uploaded_cookie_blob or build_instagram_cookie_blob(sessionid, csrftoken, ds_user_id)

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




@app.post("/integrations/instagram/groups/refresh")
@login_required
def integrations_instagram_groups_refresh():
    require_csrf()
    try:
        result = sync_instagram_groups(50)
        _audit("panel", "instagram_groups_refreshed", f"count={result.get('count', 0)}")
        if result.get("count"):
            flash(f"{result['count']} گروه Instagram پیدا و بروزرسانی شد.", "success")
        else:
            flash("فعلاً گروه Instagram در Inbox پیدا نشد. بعد از ساخت گروه دوباره Update List را بزن.", "warning")
    except Exception as exc:
        logger.exception("Instagram group refresh failed")
        flash(f"بروزرسانی گروه‌های Instagram ناموفق بود: {exc}", "danger")
    return redirect(url_for("integrations_page") + "#instagram-group-routing")


@app.post("/integrations/instagram/groups/<thread_id>/route")
@login_required
def integrations_instagram_group_route(thread_id: str):
    require_csrf()
    raw_channel = request.form.get("channel_id", "").strip()
    try:
        channel_id = int(raw_channel) if raw_channel else None
        route = set_instagram_group_route(thread_id, channel_id)
        if route.enabled and route.channel_id:
            with SessionLocal() as db:
                channel = db.get(YouTubeChannel, route.channel_id)
                channel_name = (channel.label or channel.title) if channel else f"#{route.channel_id}"
            _audit(
                "panel",
                "instagram_group_routed",
                f"thread_id={thread_id}; channel_id={route.channel_id}",
            )
            flash(
                f"گروه «{route.thread_title}» به «{channel_name}» وصل شد. "
                "از این به بعد Shareهای جدید بدون تأیید ادمین مستقیم وارد Publishing Queue همین کانال می‌شوند.",
                "success",
            )
        else:
            _audit("panel", "instagram_group_unrouted", f"thread_id={thread_id}")
            flash(f"اتصال گروه «{route.thread_title}» غیرفعال شد.", "success")
    except Exception as exc:
        logger.exception("Instagram group routing update failed")
        flash(f"ذخیره اتصال گروه ناموفق بود: {exc}", "danger")
    return redirect(url_for("integrations_page") + "#instagram-group-routing")


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


@app.get("/missions")
@login_required
def missions_page():
    selected_channel_id = request.args.get("channel_id", type=int)
    with SessionLocal() as db:
        query = db.query(YouTubeChannel).order_by(YouTubeChannel.id.asc())
        if selected_channel_id:
            query = query.filter(YouTubeChannel.id == selected_channel_id)
        channels = query.all()
    workspace_channel = channels[0] if selected_channel_id and channels else None

    missions = {}
    errors = {}
    if channels:
        workers = min(3, len(channels))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="missions-panel") as executor:
            futures = {
                executor.submit(build_channel_mission, channel.id): channel.id
                for channel in channels
            }
            for future in as_completed(futures):
                channel_id = futures[future]
                try:
                    missions[channel_id] = future.result()
                except Exception as exc:
                    errors[channel_id] = str(exc)

    return render_template(
        "missions.html",
        channels=channels,
        missions=missions,
        mission_errors=errors,
        workspace_channel=workspace_channel,
    )


@app.post("/missions/<int:channel_id>/refresh")
@login_required
def missions_refresh(channel_id: int):
    require_csrf()
    try:
        build_channel_mission(channel_id, refresh=True)
        flash("Mission با تازه‌ترین Analytics بروزرسانی شد.", "success")
    except Exception as exc:
        flash(f"Mission بروزرسانی نشد: {exc}", "danger")
    return redirect(url_for("missions_page", channel_id=channel_id))


@app.post("/missions/<int:channel_id>/settings")
@login_required
def missions_settings(channel_id: int):
    require_csrf()
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            return "Channel not found", 404
    enabled = request.form.get("enabled") == "on"
    daily_report = request.form.get("daily_report") == "on"
    set_mission_settings(channel_id, enabled=enabled, daily_report=daily_report)
    flash("تنظیمات Mission ذخیره شد.", "success")
    return redirect(url_for("missions_page", channel_id=channel_id))


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
        mode = session.get("oauth_mode")

        if mode == "telegram":
            channel = connect_channel(flow.credentials, session.get("oauth_label", "YouTube Channel"))
            token = session.get("oauth_request_token")
            with SessionLocal() as db:
                req = db.query(OAuthRequest).filter_by(token=token).one_or_none()
                if req:
                    req.used_at = datetime.utcnow()
                    db.commit()
            resumed = resume_reauth_jobs_for_channel(channel.id)
            message = f"کانال {channel.title} با دسترسی جدید متصل شد."
            if resumed:
                message += f" ادامه خودکار Jobهای در انتظار شروع شد: {', '.join('#'+str(x) for x in resumed)}"
            else:
                message += " حالا به Telegram برگرد و /channels را بزن."
            session.pop("oauth_request_token", None)
            session.pop("oauth_state", None)
            return render_template("oauth_success.html", channel=channel, message=message)

        with SessionLocal() as db:
            before_channel_ids = {row.youtube_channel_id for row in db.query(YouTubeChannel).all()}

        connected_channels = connect_channels(
            flow.credentials,
            session.get("oauth_label", "YouTube Channel"),
        )
        newly_connected = [
            channel for channel in connected_channels
            if channel.youtube_channel_id not in before_channel_ids
        ]
        analytics_synced = 0
        analytics_failed = 0
        resumed_jobs: list[int] = []
        for channel in connected_channels:
            try:
                sync_channel_analytics(channel.id, 28)
                analytics_synced += 1
            except Exception as analytics_exc:
                analytics_failed += 1
                logger.warning(
                    "Initial analytics sync failed for channel %s: %s",
                    channel.id,
                    analytics_exc,
                )
            resumed_jobs.extend(resume_reauth_jobs_for_channel(channel.id))

        session.pop("oauth_state", None)
        session.pop("oauth_label", None)
        session.pop("oauth_mode", None)
        session.pop("oauth_parent_channel_id", None)

        channel_names = "، ".join(f"«{channel.title}»" for channel in connected_channels[:5])
        extra_count = max(0, len(connected_channels) - 5)
        if extra_count:
            channel_names += f" و {extra_count} کانال دیگر"
        if mode == "subchannel" and not newly_connected:
            flash(
                f"کانال {channel_names} از قبل داخل پنل بود. "
                "برای اضافه‌کردن زیرچنل بعدی، دوباره «افزودن زیرچنل» را بزن و در Google یک Channel / Brand Account متفاوت انتخاب کن.",
                "warning",
            )
        elif mode == "subchannel":
            flash(
                f"{len(newly_connected)} زیرچنل جدید اضافه شد: "
                + "، ".join(f"«{channel.title}»" for channel in newly_connected),
                "success",
            )
        elif len(connected_channels) == 1:
            flash(
                f"کانال {channel_names} متصل شد؛ پروفایل و اطلاعات اصلی همگام شدند.",
                "success",
            )
        else:
            flash(
                f"{len(connected_channels)} کانال زیرمجموعه این Google Account شناسایی و جداگانه ثبت شدند: {channel_names}",
                "success",
            )
        if analytics_failed:
            flash(
                f"Analytics برای {analytics_synced} کانال Sync شد و برای {analytics_failed} کانال بعداً قابل Sync است.",
                "warning",
            )

        _audit(
            "panel",
            "google_account_channels_connected",
            "channel_ids="
            + ",".join(str(channel.id) for channel in connected_channels)
            + f"; analytics_synced={analytics_synced}; analytics_failed={analytics_failed}; resumed_jobs={resumed_jobs}",
        )
        if resumed_jobs:
            flash(
                "Jobهای متوقف‌شده به‌خاطر OAuth به‌صورت خودکار ادامه داده شدند: "
                + ", ".join(f"#{job_id}" for job_id in resumed_jobs),
                "success",
            )
        return redirect(url_for("channels"))
    except Exception as exc:
        logger.exception("OAuth callback failed")
        return render_template("oauth_error.html", message=str(exc)), 400


@app.post("/channels/<int:channel_id>/discover-related")
@login_required
def channels_discover_related(channel_id: int):
    require_csrf()
    with SessionLocal() as db:
        source = db.get(YouTubeChannel, channel_id)
        if not source:
            return "Channel not found", 404
        before_ids = {row.youtube_channel_id for row in db.query(YouTubeChannel).all()}
        source_title = source.title

    try:
        discovered = discover_related_channels(channel_id)
        discovered_ids = {channel.youtube_channel_id for channel in discovered}
        new_count = len(discovered_ids - before_ids)
        if len(discovered) > 1 or new_count:
            flash(
                f"{len(discovered)} کانال با همین مجوز Google شناسایی شد؛ "
                f"{new_count} کانال جدید به پنل اضافه شد.",
                "success",
            )
            _audit(
                "panel",
                "related_channels_discovered",
                f"source_channel_id={channel_id}; discovered={len(discovered)}; new={new_count}",
            )
            return redirect(url_for("channels"))

        url, state = build_authorization_url(_oauth_redirect_uri(), select_account=True)
        session["oauth_state"] = state
        session["oauth_label"] = ""
        session["oauth_mode"] = "subchannel"
        session["oauth_parent_channel_id"] = channel_id
        flash(
            f"Google برای «{source_title}» فقط یک Channel identity برگرداند. "
            "الان از صفحه Google یک Channel / Brand Account دیگرِ همین حساب را انتخاب کن.",
            "warning",
        )
        return redirect(url)
    except Exception as exc:
        logger.exception("Related YouTube channel discovery failed for channel %s", channel_id)
        flash(f"شناسایی کانال‌های زیرمجموعه ناموفق بود: {exc}", "danger")
        return redirect(url_for("channels"))


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
        channel_jobs = {
            "completed": db.query(func.count(UploadJob.id)).filter(
                UploadJob.channel_id == channel_id,
                UploadJob.status == "completed",
            ).scalar() or 0,
            "scheduled": db.query(func.count(UploadSchedule.id)).filter(
                UploadSchedule.channel_id == channel_id,
                UploadSchedule.status.in_(["waiting", "releasing"]),
            ).scalar() or 0,
            "active": db.query(func.count(UploadJob.id)).filter(
                UploadJob.channel_id == channel_id,
                UploadJob.status.in_(["preparing", "downloading", "uploading", "checking"]),
            ).scalar() or 0,
            "blocked": db.query(func.count(UploadJob.id)).filter(
                UploadJob.channel_id == channel_id,
                UploadJob.status.in_(["copyright_blocked", "preflight_blocked"]),
            ).scalar() or 0,
        }
        recent_channel_jobs = (
            db.query(UploadJob)
            .filter(UploadJob.channel_id == channel_id)
            .order_by(UploadJob.id.desc())
            .limit(6)
            .all()
        )
    upcoming_queue = queue_snapshot(channel_id=channel_id, limit=6)
    for item in upcoming_queue:
        try:
            item["scheduled_local"] = utc_to_channel_local(channel_id, item["scheduled_for"])
        except Exception:
            item["scheduled_local"] = item["scheduled_for"]

    return render_template(
        "channel_detail.html",
        channel=channel,
        analytics=analytics,
        analytics_error=analytics_error,
        authorization=authorization,
        selected_days=days,
        channel_jobs=channel_jobs,
        recent_channel_jobs=recent_channel_jobs,
        upcoming_queue=upcoming_queue,
    )



def _studio_id_list(*values: str) -> list[str]:
    result = []
    seen = set()
    for value in values:
        for item in (value or "").replace("\n", ",").replace(";", ",").split(","):
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                result.append(item)
    return result


@app.get("/channels/<int:channel_id>/studio")
@login_required
def channel_studio(channel_id: int):
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            return "Channel not found", 404

    studio = {}
    sections = []
    playlists = []
    recent_videos = []
    page_errors = {}
    try:
        studio = get_channel_studio_data(channel_id)
    except Exception as exc:
        page_errors["studio"] = str(exc)
    try:
        sections = list_channel_sections(channel_id)
    except Exception as exc:
        page_errors["sections"] = str(exc)
    try:
        playlists = list_channel_playlists(channel_id)
    except Exception as exc:
        page_errors["playlists"] = str(exc)
    try:
        recent_videos = list_channel_videos(channel_id, max_results=25).get("items", [])
    except Exception as exc:
        page_errors["videos"] = str(exc)
    try:
        authorization = channel_authorization_state(channel_id)
    except Exception as exc:
        authorization = {"manage": False, "upload": False, "force_ssl": False, "error": str(exc)}

    section_types = [
        ("recentUploads", "آخرین ویدیوها"),
        ("popularUploads", "محبوب‌ترین ویدیوها"),
        ("singlePlaylist", "یک پلی‌لیست"),
        ("multiplePlaylists", "چند پلی‌لیست"),
        ("multipleChannels", "چنل‌های پیشنهادی"),
        ("allPlaylists", "همه پلی‌لیست‌ها"),
        ("subscriptions", "اشتراک‌های کانال"),
        ("liveEvents", "Liveها"),
        ("upcomingEvents", "Liveهای آینده"),
        ("completedEvents", "Liveهای تمام‌شده"),
    ]
    return render_template(
        "channel_studio.html",
        channel=channel,
        studio=studio,
        sections=sections,
        playlists=playlists,
        recent_videos=recent_videos,
        authorization=authorization,
        section_types=section_types,
        page_errors=page_errors,
        workspace_channel=channel,
    )


@app.post("/channels/<int:channel_id>/studio/branding")
@login_required
def channel_studio_branding(channel_id: int):
    require_csrf()
    try:
        update_channel_branding(
            channel_id,
            description=request.form.get("description", ""),
            keywords=request.form.get("keywords", ""),
            country=request.form.get("country", ""),
            default_language=request.form.get("default_language", ""),
            tracking_analytics_id=request.form.get("tracking_analytics_id", ""),
            unsubscribed_trailer=request.form.get("unsubscribed_trailer", ""),
        )
        _audit("panel", "channel_branding_updated", f"channel_id={channel_id}")
        flash("تنظیمات About و Branding روی YouTube ذخیره شد.", "success")
    except Exception as exc:
        logger.exception("Channel branding update failed")
        flash(f"ذخیره تنظیمات YouTube ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_studio", channel_id=channel_id))


@app.post("/channels/<int:channel_id>/studio/audience")
@login_required
def channel_studio_audience(channel_id: int):
    require_csrf()
    try:
        value = request.form.get("made_for_kids", "")
        if value not in {"yes", "no"}:
            raise ValueError("یک گزینه Audience انتخاب کن")
        update_channel_audience(channel_id, value == "yes")
        _audit("panel", "channel_audience_updated", f"channel_id={channel_id}; made_for_kids={value}")
        flash("تنظیم Audience کانال روی YouTube ذخیره شد.", "success")
    except Exception as exc:
        logger.exception("Channel audience update failed")
        flash(f"ذخیره Audience ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_studio", channel_id=channel_id) + "#channel-status")


@app.post("/channels/<int:channel_id>/studio/banner")
@login_required
def channel_studio_banner(channel_id: int):
    require_csrf()
    upload = request.files.get("banner")
    try:
        if not upload or not upload.filename:
            raise ValueError("فایل بنر انتخاب نشده است")
        content = upload.read(6 * 1024 * 1024 + 1)
        upload_channel_banner(channel_id, content, upload.mimetype or "application/octet-stream")
        _audit("panel", "channel_banner_updated", f"channel_id={channel_id}")
        flash("بنر کانال با موفقیت روی YouTube تغییر کرد.", "success")
    except Exception as exc:
        logger.exception("Channel banner upload failed")
        flash(f"آپلود بنر ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_studio", channel_id=channel_id))


@app.post("/channels/<int:channel_id>/studio/watermark")
@login_required
def channel_studio_watermark(channel_id: int):
    require_csrf()
    upload = request.files.get("watermark")
    try:
        if not upload or not upload.filename:
            raise ValueError("فایل Watermark انتخاب نشده است")
        content = upload.read(10 * 1024 * 1024 + 1)
        timing_type = request.form.get("timing_type", "offsetFromStart")
        try:
            offset_ms = max(0, int(float(request.form.get("offset_seconds", "0") or 0) * 1000))
        except ValueError:
            offset_ms = 0
        duration_raw = request.form.get("duration_seconds", "").strip()
        duration_ms = None
        if duration_raw:
            duration_ms = max(1000, int(float(duration_raw) * 1000))
        set_channel_watermark(
            channel_id,
            content,
            upload.mimetype or "application/octet-stream",
            timing_type=timing_type,
            offset_ms=offset_ms,
            duration_ms=duration_ms,
            target_channel_id=request.form.get("target_channel_id", ""),
        )
        _audit("panel", "channel_watermark_set", f"channel_id={channel_id}")
        flash("Watermark کانال روی YouTube تنظیم شد.", "success")
    except Exception as exc:
        logger.exception("Channel watermark update failed")
        flash(f"تنظیم Watermark ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_studio", channel_id=channel_id))


@app.post("/channels/<int:channel_id>/studio/watermark/remove")
@login_required
def channel_studio_watermark_remove(channel_id: int):
    require_csrf()
    try:
        remove_channel_watermark(channel_id)
        _audit("panel", "channel_watermark_removed", f"channel_id={channel_id}")
        flash("Watermark کانال حذف شد.", "success")
    except Exception as exc:
        flash(f"حذف Watermark ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_studio", channel_id=channel_id))


@app.post("/channels/<int:channel_id>/studio/localization")
@login_required
def channel_studio_localization(channel_id: int):
    require_csrf()
    try:
        language = request.form.get("language", "").strip()
        update_channel_localization(
            channel_id,
            language,
            request.form.get("localized_title", ""),
            request.form.get("localized_description", ""),
        )
        _audit("panel", "channel_localization_updated", f"channel_id={channel_id}; language={language}")
        flash(f"ترجمه {language} ذخیره شد.", "success")
    except Exception as exc:
        flash(f"ذخیره ترجمه ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_studio", channel_id=channel_id) + "#localizations")


@app.post("/channels/<int:channel_id>/studio/localization/delete")
@login_required
def channel_studio_localization_delete(channel_id: int):
    require_csrf()
    language = request.form.get("language", "").strip()
    try:
        delete_channel_localization(channel_id, language)
        _audit("panel", "channel_localization_deleted", f"channel_id={channel_id}; language={language}")
        flash(f"ترجمه {language} حذف شد.", "success")
    except Exception as exc:
        flash(f"حذف ترجمه ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_studio", channel_id=channel_id) + "#localizations")


@app.post("/channels/<int:channel_id>/studio/sections")
@login_required
def channel_studio_section_create(channel_id: int):
    require_csrf()
    try:
        playlist_ids = _studio_id_list(*request.form.getlist("playlist_ids"), request.form.get("playlist_ids_manual", ""))
        channel_ids = _studio_id_list(request.form.get("channel_ids", ""))
        create_channel_section(
            channel_id,
            request.form.get("section_type", ""),
            title=request.form.get("section_title", ""),
            position=int(request.form.get("position", "0") or 0),
            playlist_ids=playlist_ids,
            channel_ids=channel_ids,
        )
        _audit("panel", "channel_section_created", f"channel_id={channel_id}")
        flash("بخش جدید به Home کانال اضافه شد.", "success")
    except Exception as exc:
        logger.exception("Channel section create failed")
        flash(f"ساخت بخش Home ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_studio", channel_id=channel_id) + "#home-layout")


@app.post("/channels/<int:channel_id>/studio/sections/<section_id>/update")
@login_required
def channel_studio_section_update(channel_id: int, section_id: str):
    require_csrf()
    try:
        playlist_ids = _studio_id_list(request.form.get("playlist_ids", ""))
        channel_ids = _studio_id_list(request.form.get("channel_ids", ""))
        update_channel_section(
            channel_id,
            section_id,
            request.form.get("section_type", ""),
            title=request.form.get("section_title", ""),
            position=int(request.form.get("position", "0") or 0),
            playlist_ids=playlist_ids,
            channel_ids=channel_ids,
        )
        _audit("panel", "channel_section_updated", f"channel_id={channel_id}; section_id={section_id}")
        flash("بخش Home بروزرسانی شد.", "success")
    except Exception as exc:
        logger.exception("Channel section update failed")
        flash(f"بروزرسانی بخش ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_studio", channel_id=channel_id) + "#home-layout")


@app.post("/channels/<int:channel_id>/studio/sections/<section_id>/delete")
@login_required
def channel_studio_section_delete(channel_id: int, section_id: str):
    require_csrf()
    try:
        delete_channel_section(channel_id, section_id)
        _audit("panel", "channel_section_deleted", f"channel_id={channel_id}; section_id={section_id}")
        flash("بخش از Home کانال حذف شد.", "success")
    except Exception as exc:
        flash(f"حذف بخش ناموفق بود: {exc}", "danger")
    return redirect(url_for("channel_studio", channel_id=channel_id) + "#home-layout")



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



@app.get("/videos")
@login_required
def videos_manager():
    with SessionLocal() as db:
        channels_list = db.query(YouTubeChannel).order_by(YouTubeChannel.label.asc()).all()

    selected_channel = None
    if channels_list:
        try:
            selected_id = int(request.args.get("channel_id") or channels_list[0].id)
        except (TypeError, ValueError):
            selected_id = channels_list[0].id
        selected_channel = next((item for item in channels_list if item.id == selected_id), channels_list[0])

    videos_payload = {"items": [], "next_page_token": ""}
    playlists = []
    authorization = {}
    manager_error = None
    if selected_channel:
        try:
            videos_payload = list_channel_videos(
                selected_channel.id,
                page_token=request.args.get("page_token", "").strip(),
                max_results=24,
            )
            playlists = list_channel_playlists(selected_channel.id)
            authorization = channel_authorization_state(selected_channel.id)
        except Exception as exc:
            manager_error = str(exc)
            logger.warning("Video manager load failed for channel %s: %s", selected_channel.id, exc)

    return render_template(
        "videos.html",
        channels=channels_list,
        selected_channel=selected_channel,
        videos=videos_payload.get("items", []),
        next_page_token=videos_payload.get("next_page_token", ""),
        playlists=playlists,
        authorization=authorization,
        manager_error=manager_error,
        workspace_channel=selected_channel,
    )


@app.get("/videos/<int:channel_id>/<video_id>")
@login_required
def video_detail(channel_id: int, video_id: str):
    with SessionLocal() as db:
        channel = db.get(YouTubeChannel, channel_id)
        if not channel:
            return "Channel not found", 404

    video = None
    comments = []
    held_comments = []
    captions = []
    playlists = []
    page_errors = {}
    try:
        video = get_video_manager_data(channel_id, video_id)
    except Exception as exc:
        flash(f"دریافت ویدیو ناموفق بود: {exc}", "danger")
        return redirect(url_for("videos_manager", channel_id=channel_id))

    for key, loader in (
        ("comments", lambda: list_video_comments(channel_id, video_id, "published").get("items", [])),
        ("held_comments", lambda: list_video_comments(channel_id, video_id, "heldForReview").get("items", [])),
        ("captions", lambda: list_video_captions(channel_id, video_id)),
        ("playlists", lambda: list_channel_playlists(channel_id)),
    ):
        try:
            value = loader()
            if key == "comments":
                comments = value
            elif key == "held_comments":
                held_comments = value
            elif key == "captions":
                captions = value
            else:
                playlists = value
        except Exception as exc:
            page_errors[key] = str(exc)

    try:
        authorization = channel_authorization_state(channel_id)
    except Exception as exc:
        authorization = {"manage": False, "force_ssl": False, "error": str(exc)}

    return render_template(
        "video_detail.html",
        channel=channel,
        video=video,
        comments=comments,
        held_comments=held_comments,
        captions=captions,
        playlists=playlists,
        authorization=authorization,
        page_errors=page_errors,
        workspace_channel=channel,
    )


@app.post("/videos/<int:channel_id>/<video_id>/edit")
@login_required
def video_edit(channel_id: int, video_id: str):
    require_csrf()
    tags_raw = request.form.get("tags", "")
    tags = [part.strip() for part in tags_raw.replace("\n", ",").split(",") if part.strip()]
    try:
        update_video_metadata(
            channel_id,
            video_id,
            title=request.form.get("title", ""),
            description=request.form.get("description", ""),
            tags=tags,
            privacy=request.form.get("privacy", "private"),
            category_id=request.form.get("category_id", "22"),
            made_for_kids=request.form.get("made_for_kids") == "on",
            embeddable=request.form.get("embeddable") == "on",
        )
        _audit("panel", "youtube_video_updated", f"channel_id={channel_id}; video_id={video_id}")
        flash("اطلاعات ویدیو روی YouTube بروزرسانی شد.", "success")
    except Exception as exc:
        flash(f"ویرایش ویدیو ناموفق بود: {exc}", "danger")
    return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))


@app.post("/videos/<int:channel_id>/<video_id>/thumbnail")
@login_required
def video_thumbnail(channel_id: int, video_id: str):
    require_csrf()
    uploaded = request.files.get("thumbnail")
    if not uploaded or not uploaded.filename:
        flash("فایل Thumbnail انتخاب نشده است.", "warning")
        return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))
    try:
        content = uploaded.read(50 * 1024 * 1024 + 1)
        set_video_thumbnail(channel_id, video_id, content, uploaded.mimetype or "application/octet-stream")
        _audit("panel", "youtube_thumbnail_updated", f"channel_id={channel_id}; video_id={video_id}")
        flash("Thumbnail جدید روی YouTube تنظیم شد.", "success")
    except Exception as exc:
        flash(f"تغییر Thumbnail ناموفق بود: {exc}", "danger")
    return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))


@app.post("/videos/<int:channel_id>/<video_id>/delete")
@login_required
def video_delete(channel_id: int, video_id: str):
    require_csrf()
    confirmation = request.form.get("confirmation", "").strip()
    if confirmation != "DELETE":
        flash("برای حذف دائمی، عبارت DELETE را وارد کن.", "warning")
        return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))
    try:
        delete_video(channel_id, video_id)
        try:
            refresh_channel(channel_id)
        except Exception:
            pass
        _audit("panel", "youtube_video_deleted", f"channel_id={channel_id}; video_id={video_id}")
        flash("ویدیو برای همیشه از YouTube حذف شد.", "success")
        return redirect(url_for("videos_manager", channel_id=channel_id))
    except Exception as exc:
        flash(f"حذف ویدیو ناموفق بود: {exc}", "danger")
        return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))


@app.post("/videos/<int:channel_id>/<video_id>/playlists/add")
@login_required
def video_playlist_add(channel_id: int, video_id: str):
    require_csrf()
    playlist_id = request.form.get("playlist_id", "").strip()
    try:
        add_video_to_playlist(channel_id, playlist_id, video_id)
        _audit("panel", "youtube_playlist_item_added", f"channel_id={channel_id}; video_id={video_id}; playlist_id={playlist_id}")
        flash("ویدیو به Playlist اضافه شد.", "success")
    except Exception as exc:
        flash(f"افزودن به Playlist ناموفق بود: {exc}", "danger")
    return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))


@app.post("/playlists/<int:channel_id>/create")
@login_required
def playlist_create(channel_id: int):
    require_csrf()
    try:
        create_playlist(
            channel_id,
            request.form.get("title", ""),
            request.form.get("description", ""),
            request.form.get("privacy", "private"),
        )
        _audit("panel", "youtube_playlist_created", f"channel_id={channel_id}")
        flash("Playlist جدید ساخته شد.", "success")
    except Exception as exc:
        flash(f"ساخت Playlist ناموفق بود: {exc}", "danger")
    return redirect(url_for("videos_manager", channel_id=channel_id))


@app.post("/playlists/<int:channel_id>/<playlist_id>/edit")
@login_required
def playlist_edit(channel_id: int, playlist_id: str):
    require_csrf()
    try:
        update_playlist(
            channel_id,
            playlist_id,
            request.form.get("title", ""),
            request.form.get("description", ""),
            request.form.get("privacy", "private"),
        )
        _audit("panel", "youtube_playlist_updated", f"channel_id={channel_id}; playlist_id={playlist_id}")
        flash("Playlist بروزرسانی شد.", "success")
    except Exception as exc:
        flash(f"ویرایش Playlist ناموفق بود: {exc}", "danger")
    return redirect(url_for("videos_manager", channel_id=channel_id))


@app.post("/playlists/<int:channel_id>/<playlist_id>/delete")
@login_required
def playlist_delete(channel_id: int, playlist_id: str):
    require_csrf()
    try:
        delete_playlist(channel_id, playlist_id)
        _audit("panel", "youtube_playlist_deleted", f"channel_id={channel_id}; playlist_id={playlist_id}")
        flash("Playlist حذف شد.", "success")
    except Exception as exc:
        flash(f"حذف Playlist ناموفق بود: {exc}", "danger")
    return redirect(url_for("videos_manager", channel_id=channel_id))


@app.post("/videos/<int:channel_id>/<video_id>/comments/<comment_id>/reply")
@login_required
def video_comment_reply(channel_id: int, video_id: str, comment_id: str):
    require_csrf()
    try:
        reply_to_comment(channel_id, video_id, comment_id, request.form.get("text", ""))
        _audit("panel", "youtube_comment_replied", f"channel_id={channel_id}; video_id={video_id}")
        flash("پاسخ روی YouTube ارسال شد.", "success")
    except Exception as exc:
        flash(f"ارسال پاسخ ناموفق بود: {exc}", "danger")
    return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))


@app.post("/videos/<int:channel_id>/<video_id>/comments/<comment_id>/moderate")
@login_required
def video_comment_moderate(channel_id: int, video_id: str, comment_id: str):
    require_csrf()
    try:
        moderate_comment(
            channel_id,
            video_id,
            comment_id,
            request.form.get("status", "published"),
            request.form.get("ban_author") == "on",
        )
        _audit("panel", "youtube_comment_moderated", f"channel_id={channel_id}; video_id={video_id}; status={request.form.get('status')}")
        flash("وضعیت Comment بروزرسانی شد.", "success")
    except Exception as exc:
        flash(f"مدیریت Comment ناموفق بود: {exc}", "danger")
    return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))


@app.post("/videos/<int:channel_id>/<video_id>/comments/<comment_id>/delete")
@login_required
def video_comment_delete(channel_id: int, video_id: str, comment_id: str):
    require_csrf()
    try:
        delete_comment(channel_id, video_id, comment_id)
        _audit("panel", "youtube_comment_deleted", f"channel_id={channel_id}; video_id={video_id}")
        flash("Comment حذف شد.", "success")
    except Exception as exc:
        flash(f"حذف Comment ناموفق بود: {exc}", "danger")
    return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))


@app.post("/videos/<int:channel_id>/<video_id>/captions/upload")
@login_required
def video_caption_upload(channel_id: int, video_id: str):
    require_csrf()
    uploaded = request.files.get("caption_file")
    if not uploaded or not uploaded.filename:
        flash("فایل Caption انتخاب نشده است.", "warning")
        return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))
    try:
        content = uploaded.read(100 * 1024 * 1024 + 1)
        upload_caption(
            channel_id,
            video_id,
            content=content,
            language=request.form.get("language", ""),
            name=request.form.get("name", ""),
            is_draft=request.form.get("is_draft") == "on",
        )
        _audit("panel", "youtube_caption_uploaded", f"channel_id={channel_id}; video_id={video_id}")
        flash("Caption روی YouTube آپلود شد.", "success")
    except Exception as exc:
        flash(f"آپلود Caption ناموفق بود: {exc}", "danger")
    return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))


@app.post("/videos/<int:channel_id>/<video_id>/captions/<caption_id>/delete")
@login_required
def video_caption_delete(channel_id: int, video_id: str, caption_id: str):
    require_csrf()
    try:
        delete_caption(channel_id, video_id, caption_id)
        _audit("panel", "youtube_caption_deleted", f"channel_id={channel_id}; video_id={video_id}")
        flash("Caption حذف شد.", "success")
    except Exception as exc:
        flash(f"حذف Caption ناموفق بود: {exc}", "danger")
    return redirect(url_for("video_detail", channel_id=channel_id, video_id=video_id))


@app.get("/publishing")
@login_required
def publishing_page():
    selected_channel_id = request.args.get("channel_id", type=int)
    with SessionLocal() as db:
        query = db.query(YouTubeChannel).order_by(YouTubeChannel.label.asc())
        if selected_channel_id:
            query = query.filter(YouTubeChannel.id == selected_channel_id)
        channels_list = query.all()
    configs = {}
    for channel in channels_list:
        try:
            configs[channel.id] = publishing_config_payload(channel.id)
        except Exception as exc:
            configs[channel.id] = {"error": str(exc)}
    queue = queue_snapshot(channel_id=selected_channel_id, limit=150)
    for item in queue:
        try:
            item["scheduled_local"] = utc_to_channel_local(item["channel_id"], item["scheduled_for"])
        except Exception:
            item["scheduled_local"] = item["scheduled_for"]
    return render_template(
        "publishing.html",
        channels=channels_list,
        configs=configs,
        queue=queue,
        workspace_channel=(channels_list[0] if selected_channel_id and channels_list else None),
    )


@app.post("/publishing/<int:channel_id>/settings")
@login_required
def publishing_settings(channel_id: int):
    require_csrf()
    slots_raw = request.form.get("manual_slots", "")
    try:
        slots = [int(part.strip()) for part in slots_raw.replace(";", ",").split(",") if part.strip()]
        update_publishing_config(
            channel_id,
            enabled=request.form.get("enabled") == "on",
            smart_peak_enabled=request.form.get("smart_peak_enabled") == "on",
            videos_per_day=int(request.form.get("videos_per_day", "2")),
            timezone_name=request.form.get("timezone", "Asia/Tehran").strip() or "Asia/Tehran",
            minimum_gap_minutes=int(request.form.get("minimum_gap_minutes", "180")),
            allowed_start_hour=int(request.form.get("allowed_start_hour", "9")),
            allowed_end_hour=int(request.form.get("allowed_end_hour", "23")),
            manual_slots=slots,
        )
        if request.form.get("reschedule_waiting") == "on":
            count = reschedule_channel_queue(channel_id)
            flash(f"تنظیمات انتشار ذخیره شد و {count} آیتم صف دوباره زمان‌بندی شد.", "success")
        else:
            flash("تنظیمات انتشار هوشمند ذخیره شد.", "success")
        _audit("panel", "publishing_settings_updated", f"channel_id={channel_id}")
    except Exception as exc:
        flash(f"ذخیره تنظیمات انتشار ناموفق بود: {exc}", "danger")
    return redirect(url_for("publishing_page", channel_id=channel_id))


@app.post("/publishing/<int:channel_id>/analyze")
@login_required
def publishing_analyze(channel_id: int):
    require_csrf()
    try:
        result = analyze_peak_slots(channel_id, 90)
        _audit("panel", "publishing_peak_analyzed", f"channel_id={channel_id}; samples={result.get('video_samples')}")
        flash(f"تحلیل پیک بروزرسانی شد؛ {result.get('video_samples', 0)} ویدیو بررسی شد.", "success")
    except Exception as exc:
        flash(f"تحلیل زمان پیک ناموفق بود: {exc}", "danger")
    return redirect(url_for("publishing_page", channel_id=channel_id))


@app.post("/publishing/<int:channel_id>/bulk")
@login_required
def publishing_bulk_queue(channel_id: int):
    require_csrf()
    raw = request.form.get("items", "")
    created = 0
    errors = []
    for index, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if "|" not in line:
            errors.append(f"خط {index}: فرمت باید URL | Title باشد")
            continue
        source_url, title = [part.strip() for part in line.split("|", 1)]
        if not is_supported_instagram_url(source_url):
            errors.append(f"خط {index}: لینک Instagram معتبر نیست")
            continue
        if not title:
            errors.append(f"خط {index}: عنوان خالی است")
            continue
        job = None
        try:
            job = create_job(
                channel_id=channel_id,
                source_url=source_url,
                title=title,
                source="panel-smart",
            )
            schedule_job_smart(job.id)
            created += 1
        except Exception as exc:
            if job is not None:
                mark_job_failed(job.id, f"Scheduling failed: {exc}")
            errors.append(f"خط {index}: {exc}")
    if created:
        flash(f"{created} ویدیو وارد صف انتشار هوشمند شد.", "success")
    if errors:
        flash(" | ".join(errors[:5]), "warning")
    _audit("panel", "publishing_bulk_queued", f"channel_id={channel_id}; created={created}; errors={len(errors)}")
    return redirect(url_for("publishing_page", channel_id=channel_id))


@app.post("/queue/<int:job_id>/publish-now")
@login_required
def queue_publish_now(job_id: int):
    require_csrf()
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        channel_id = job.channel_id if job else None
    try:
        release_job_now(job_id)
        flash(f"Job #{job_id} برای انتشار فوری آزاد شد.", "success")
        _audit("panel", "scheduled_job_released", f"job_id={job_id}")
    except Exception as exc:
        flash(f"انتشار فوری ناموفق بود: {exc}", "danger")
    return redirect(
        url_for("publishing_page", channel_id=channel_id)
        if channel_id else url_for("publishing_page")
    )


@app.post("/queue/<int:job_id>/cancel")
@login_required
def queue_cancel(job_id: int):
    require_csrf()
    with SessionLocal() as db:
        job = db.get(UploadJob, job_id)
        channel_id = job.channel_id if job else None
    try:
        cancel_scheduled_job(job_id)
        flash(f"Job #{job_id} از صف حذف شد.", "success")
        _audit("panel", "scheduled_job_cancelled", f"job_id={job_id}")
    except Exception as exc:
        flash(f"لغو Job ناموفق بود: {exc}", "danger")
    return redirect(
        url_for("publishing_page", channel_id=channel_id)
        if channel_id else url_for("publishing_page")
    )


@app.post("/api/long-videos/probe")
@login_required
def api_long_video_probe():
    require_csrf()
    source_url = request.form.get("source_url", "").strip()
    if not is_supported_external_url(source_url):
        return jsonify({"ok": False, "error": "لینک عمومی معتبر http/https وارد کن."}), 400
    try:
        payload = probe_external_video(source_url)
        return jsonify({"ok": True, **payload})
    except Exception as exc:
        logger.warning("Long video probe failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)[:500]}), 400


@app.route("/long-videos", methods=["GET", "POST"])
@login_required
def long_video_upload():
    with SessionLocal() as db:
        channels_list = (
            db.query(YouTubeChannel)
            .filter(YouTubeChannel.is_active.is_(True))
            .order_by(YouTubeChannel.label)
            .all()
        )

    channel_configs = {}
    authorization = {}
    for channel in channels_list:
        try:
            channel_configs[channel.id] = publishing_config_payload(channel.id)
        except Exception:
            channel_configs[channel.id] = {"timezone": "Asia/Tehran"}
        try:
            authorization[channel.id] = channel_authorization_state(channel.id)
        except Exception as exc:
            authorization[channel.id] = {"manage": False, "force_ssl": False, "error": str(exc)}

    if request.method == "POST":
        require_csrf()
        source_url = request.form.get("source_url", "").strip()
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        hashtags = request.form.get("hashtags", "").strip()
        tags = request.form.get("tags", "").strip()
        category_id = request.form.get("category_id", "24").strip() or "24"
        quality = request.form.get("quality", "max").strip()
        privacy = request.form.get("privacy", "public").strip()
        license_name = request.form.get("license", "youtube").strip()
        default_language = request.form.get("default_language", "").strip()
        audio_language = request.form.get("audio_language", "").strip()
        made_for_kids = request.form.get("made_for_kids") == "on"
        embeddable = request.form.get("embeddable") == "on"
        notify_subscribers = request.form.get("notify_subscribers") == "on"
        publish_mode = request.form.get("publish_mode", "manual").strip()

        try:
            channel_id = int(request.form.get("channel_id", "0"))
        except ValueError:
            channel_id = 0

        if not is_supported_external_url(source_url):
            flash("لینک دانلود معتبر نیست یا به آدرس خصوصی/داخلی اشاره می‌کند.", "danger")
        elif not title:
            flash("عنوان ویدیو الزامی است.", "danger")
        elif len(title) > 100:
            flash("عنوان YouTube حداکثر 100 کاراکتر است.", "danger")
        elif privacy not in {"public", "unlisted", "private"}:
            flash("Privacy نامعتبر است.", "danger")
        else:
            job = None
            thumbnail_path = None
            try:
                job = create_job(
                    channel_id=channel_id,
                    source_url=source_url,
                    title=title,
                    description=description,
                    hashtags=hashtags,
                    privacy=privacy,
                    source="panel-long",
                )

                thumbnail = request.files.get("thumbnail")
                if thumbnail and thumbnail.filename:
                    raw = thumbnail.read(2 * 1024 * 1024 + 1)
                    if len(raw) > 2 * 1024 * 1024:
                        raise ValueError("Thumbnail باید حداکثر 2MB باشد.")
                    mime = (thumbnail.mimetype or "").lower()
                    if mime not in {"image/jpeg", "image/png"}:
                        raise ValueError("Thumbnail فقط JPEG یا PNG باشد.")
                    ext = ".png" if mime == "image/png" else ".jpg"
                    asset_dir = DATA_DIR / "long_assets" / f"job-{job.id}"
                    asset_dir.mkdir(parents=True, exist_ok=True)
                    path = asset_dir / f"thumbnail{ext}"
                    path.write_bytes(raw)
                    path.chmod(0o600)
                    thumbnail_path = str(path)

                configure_long_job(
                    job.id,
                    quality=quality,
                    tags=tags,
                    category_id=category_id,
                    made_for_kids=made_for_kids,
                    embeddable=embeddable,
                    license_name=license_name,
                    notify_subscribers=notify_subscribers,
                    default_language=default_language,
                    audio_language=audio_language,
                    thumbnail_path=thumbnail_path,
                )

                if publish_mode == "manual":
                    scheduled_utc = local_datetime_to_utc(
                        channel_id,
                        request.form.get("scheduled_at", ""),
                    )
                    schedule = schedule_job_manual(job.id, scheduled_utc)
                    local_time = utc_to_channel_local(channel_id, schedule.scheduled_for)
                    _audit(
                        "panel",
                        "long_video_prepare_scheduled",
                        f"job_id={job.id}; channel_id={channel_id}; scheduled_for={schedule.scheduled_for.isoformat()}",
                    )
                    flash(
                        f"Long Job #{job.id} ثبت شد؛ دانلود، Private Upload و Copyright Check از همین الان شروع می‌شود و انتشار برای {local_time:%Y-%m-%d %H:%M} است.",
                        "success",
                    )
                    return redirect(url_for("jobs"))

                enqueue_job(job.id)
                _audit("panel", "long_video_immediate", f"job_id={job.id}; channel_id={channel_id}")
                flash(
                    f"Long Job #{job.id} وارد Pipeline شد: دانلود Max Quality → Private → Check → انتشار.",
                    "success",
                )
                return redirect(url_for("jobs"))
            except Exception as exc:
                if job is not None:
                    mark_job_failed(job.id, f"Long video setup failed: {exc}")
                flash(f"ثبت Long Video ناموفق بود: {exc}", "danger")

    selected_channel_id = request.args.get("channel_id", type=int)
    workspace_channel = next(
        (channel for channel in channels_list if channel.id == selected_channel_id),
        None,
    )
    return render_template(
        "long_videos.html",
        channels=channels_list,
        channel_configs=channel_configs,
        authorization=authorization,
        selected_channel_id=selected_channel_id,
        workspace_channel=workspace_channel,
    )


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
            job = None
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
                publish_mode = request.form.get("publish_mode", "auto")
                if publish_mode == "auto":
                    cfg = publishing_config_payload(channel_id)
                    publish_mode = "smart" if cfg.get("enabled") else "immediate"

                if publish_mode == "smart":
                    schedule = schedule_job_smart(job.id)
                    local_time = utc_to_channel_local(channel_id, schedule.scheduled_for)
                    flash(f"Job #{job.id} برای {local_time.strftime('%Y-%m-%d %H:%M')} وارد صف هوشمند شد.", "success")
                    _audit("panel", "upload_smart_scheduled", f"job_id={job.id}; channel_id={channel_id}")
                    return redirect(url_for("publishing_page", channel_id=channel_id))
                if publish_mode == "manual":
                    scheduled_utc = local_datetime_to_utc(channel_id, request.form.get("scheduled_at", ""))
                    schedule = schedule_job_manual(job.id, scheduled_utc)
                    local_time = utc_to_channel_local(channel_id, schedule.scheduled_for)
                    flash(f"Job #{job.id} برای {local_time.strftime('%Y-%m-%d %H:%M')} زمان‌بندی شد.", "success")
                    _audit("panel", "upload_manual_scheduled", f"job_id={job.id}; channel_id={channel_id}")
                    return redirect(url_for("publishing_page", channel_id=channel_id))

                enqueue_job(job.id)
                _audit("panel", "upload_queued", f"job_id={job.id}; channel_id={channel_id}")
                flash(f"Job #{job.id} وارد صف انتشار فوری شد.", "success")
                return redirect(url_for("jobs"))
            except Exception as exc:
                if job is not None:
                    mark_job_failed(job.id, f"Scheduling failed: {exc}")
                flash(f"ثبت ارسال ناموفق بود: {exc}", "danger")
    selected_channel_id = request.args.get("channel_id", type=int)
    workspace_channel = next(
        (channel for channel in channels_list if channel.id == selected_channel_id),
        None,
    )
    return render_template(
        "upload.html",
        channels=channels_list,
        selected_channel_id=selected_channel_id,
        workspace_channel=workspace_channel,
    )


@app.get("/jobs")
@login_required
def jobs():
    status = request.args.get("status", "").strip()
    selected_channel_id = request.args.get("channel_id", type=int)
    with SessionLocal() as db:
        query = db.query(UploadJob)
        if selected_channel_id:
            query = query.filter(UploadJob.channel_id == selected_channel_id)
        if status in {
            "queued", "scheduled", "preparing", "ready_scheduled", "cancelled", "downloading",
            "uploading", "checking", "completed", "failed", "reauth_required",
            "copyright_blocked", "preflight_blocked",
        }:
            query = query.filter(UploadJob.status == status)
        rows = query.order_by(UploadJob.id.desc()).limit(Config.MAX_UPLOAD_HISTORY).all()
        all_channels = db.query(YouTubeChannel).order_by(YouTubeChannel.label.asc()).all()
        channel_map = {c.id: c for c in all_channels}
        selected_channel = channel_map.get(selected_channel_id) if selected_channel_id else None
    return render_template(
        "jobs.html",
        jobs=rows,
        channel_map=channel_map,
        selected_status=status,
        selected_channel=selected_channel,
        selected_channel_id=selected_channel_id,
    )


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