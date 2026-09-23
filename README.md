# YT Multi-Channel Shorts Manager

A production-oriented Telegram + web control plane for publishing Instagram Reels/Posts to multiple independently connected YouTube channels, with a professional multi-channel analytics dashboard.

## Features

- Multiple YouTube / Brand Channels with separate Google OAuth credentials.
- OAuth tokens encrypted before storage.
- YouTube Data API + YouTube Analytics API integration.
- Per-channel analytics for 7, 28, 90 and 365 day windows.
- Current-period vs previous-period comparison.
- Views, likes, comments, shares, subscriber gained/lost/net, watch time and average view duration.
- Top-performing videos from YouTube Analytics plus current video statistics from YouTube Data API.
- Latest uploads fetched through the channel uploads playlist instead of expensive search calls.
- Glassmorphism Persian RTL management dashboard with responsive layouts and interactive Apache ECharts charts.
- Per-channel label, hashtags, privacy, enabled state and publishing history.
- Admin-only Telegram bot for channel selection, connection and publishing.
- Persistent upload jobs with `queued`, `downloading`, `uploading`, `completed` and `failed` states.
- Docker/Compose deployment with FFmpeg included.
- SQLite + WAL by default, with SQLAlchemy for future PostgreSQL migration.

## Analytics dashboard

The main dashboard provides a cross-channel command center:

- Lifetime views, subscribers and video counts.
- Period views, likes, comments, shares and watch time.
- Net subscriber growth.
- Previous-period percentage comparisons.
- Multi-channel daily views chart.
- View distribution between channels.
- Per-channel analytics cards.
- Publishing pipeline status.

Each channel also has a dedicated **Analytics Studio** with:

- Lifetime channel counters.
- Daily views + subscriber growth.
- Likes / comments / shares chart.
- Watch time + average view duration chart.
- Top videos for the selected period.
- Latest YouTube uploads with live views / likes / comments.
- OAuth scope status and reconnect workflow.
- Independent publishing settings.

Analytics responses are cached in `channel_analytics_cache`, so navigating the panel does not repeatedly spend API calls. Manual Sync is available per channel and for all active channels.

## Architecture

```text
Telegram Admin ───────┐
                      ├──► SQLAlchemy DB ◄── Web Panel
                      │       │
                      │       ├── YouTube channels + encrypted OAuth
                      │       ├── analytics cache
                      │       ├── upload jobs
                      │       └── Telegram preferences
                      │
                      ├──► YouTube Data API v3
                      │       ├── channel lifetime statistics
                      │       └── current video statistics
                      │
                      └──► YouTube Analytics API v2
                              ├── daily performance
                              ├── engagement
                              ├── subscriber growth
                              ├── watch time
                              └── top content
```

## UI stack

The panel keeps the Flask/Jinja architecture instead of introducing a second React application.

- **Apache ECharts** for interactive data visualization.
- **Lucide** for interface icons.
- Glass / bento dashboard styling inspired by modern open-source admin patterns such as Flowbite Admin and shadcn-admin.
- No Flowbite/shadcn runtime is required and no full external template is copied into the project.

See `THIRD_PARTY.md` for details.

## Telegram bot

Access is limited to IDs in `TELEGRAM_ADMIN_IDS`.

```text
/start
/channels
/connect
/channelinfo [CHANNEL_ID]
/uploads
/toggle CHANNEL_ID
/setprivacy CHANNEL_ID public|unlisted|private
/sethashtags CHANNEL_ID #tag1 #tag2
/refresh [CHANNEL_ID]
/cancel
/help
```

To publish:

1. Select the destination using `/channels`.
2. Send an Instagram Reel/Post URL.
3. Send the title.
4. The job is persisted and processed.
5. The bot sends the final YouTube URL.

## Google Cloud setup

1. Create a Google Cloud project.
2. Enable **YouTube Data API v3**.
3. Enable **YouTube Analytics API**.
4. Configure the OAuth consent screen.
5. Create a **Web application** OAuth Client ID.
6. Add the exact redirect URI:

```text
https://YOUR-DOMAIN.example/oauth/callback
```

7. Save the OAuth client JSON as `client_secret.json`.
8. Set `PUBLIC_BASE_URL=https://YOUR-DOMAIN.example`.

The application requests:

```text
https://www.googleapis.com/auth/youtube.upload
https://www.googleapis.com/auth/youtube.readonly
https://www.googleapis.com/auth/yt-analytics.readonly
```

### Existing connected channels

Channels connected before Analytics support was added do not have the new Analytics scope. In the channel page, click **Reconnect OAuth** and choose the same Google / Brand Channel. The existing channel record is updated instead of duplicated when the same YouTube channel ID is selected.

For public production OAuth apps, Google may require OAuth app verification depending on the scopes and user audience.

## Installation

```bash
git clone https://github.com/PEDIHS/yt.git
cd yt
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Install FFmpeg through the OS package manager, or use Docker.

Minimum environment configuration:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_IDS=123456789
PANEL_USERNAME=admin
PANEL_PASSWORD=...
SECRET_KEY=...
TOKEN_ENCRYPTION_KEY=...
PUBLIC_BASE_URL=https://your-domain.example
CLIENT_SECRET_FILE=client_secret.json
```

Generate secure secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Do not change `SECRET_KEY` / `TOKEN_ENCRYPTION_KEY` after channels have been connected unless encrypted credentials are intentionally migrated or channels are reconnected.

## Run

Panel:

```bash
gunicorn --workers=1 --threads=8 --bind=0.0.0.0:8080 --timeout=120 panel:app
```

Telegram bot:

```bash
python main.py
```

## Docker Compose

```bash
cp .env.example .env
# edit .env and add client_secret.json
docker compose up -d --build
```

Put port `8080` behind Nginx/Caddy and HTTPS. Google OAuth must use the same public HTTPS origin configured in `PUBLIC_BASE_URL`.

## Data model

- `youtube_channels`: encrypted credential set and independent settings per channel.
- `channel_analytics_cache`: cached API analytics by channel and period.
- `upload_jobs`: durable publishing history.
- `telegram_preferences`: selected destination channel per Telegram admin.
- `oauth_requests`: short-lived Telegram-to-web OAuth links.
- `audit_logs`: security and panel events.

SQLite is stored at `data/app.db` by default. WAL and foreign keys are enabled. The analytics cache table is additive, so existing databases are upgraded automatically by SQLAlchemy `create_all`.

## Security

Never commit:

- `.env`
- `client_secret.json`
- Telegram bot tokens
- Instagram session IDs
- OAuth refresh/access tokens
- `data/app.db`

## Project layout

```text
.
├── analytics.py           # YouTube Analytics sync/cache
├── config.py
├── db.py
├── downloader.py
├── jobs.py
├── main.py                # Telegram bot
├── models.py
├── panel.py               # Flask admin + analytics routes
├── security.py
├── youtube.py             # OAuth, Data API and uploader
├── templates/
├── static/
│   ├── panel.css
│   └── panel.js
├── tests/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Next upgrades

The architecture is ready for PostgreSQL, Redis/Celery/RQ, scheduled publishing, role-based panel users, revenue analytics (with the monetary Analytics scope), geographic/device reports, thumbnail CTR reports, quotas and additional content sources.

## License

MIT. See `LICENSE`.
