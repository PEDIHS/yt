# YT Multi-Channel Shorts Manager

A production-oriented Telegram + web control plane for downloading Instagram Reels/Posts and publishing them to multiple independently connected YouTube channels.

## Features

- Multiple YouTube channels connected with separate Google OAuth credentials.
- OAuth tokens encrypted before storage in the database.
- Per-channel label, hashtags, privacy, enabled state, statistics and upload history.
- Persian RTL web panel for channel management and manual publishing.
- Admin-only Telegram bot for selecting, connecting and managing channels.
- Persistent upload jobs with `queued`, `downloading`, `uploading`, `completed` and `failed` states.
- Isolated download directories per job.
- Docker/Compose deployment with FFmpeg included.
- SQLite + WAL by default, with SQLAlchemy configuration for future database migration.

## Architecture

```text
Telegram Admin ───────┐
                      ├──► SQLAlchemy DB ◄── Web Panel
                      │       │
                      │       ├── YouTube channels + encrypted OAuth tokens
                      │       ├── upload jobs
                      │       └── Telegram preferences
                      │
                      └──► Job Processor
                              │
                        Instagram / yt-dlp
                              │
                         YouTube Data API
                              │
                    selected destination channel
```

## Web panel

The panel includes:

- Secure admin login and CSRF protection.
- Dashboard with connected channels and upload metrics.
- Google OAuth connection flow for multiple YouTube or Brand Channels.
- Per-channel configuration for:
  - internal label
  - enabled/disabled state
  - default hashtags
  - default privacy: `public`, `unlisted`, `private`
  - YouTube statistics sync
- Manual Instagram URL submission to a selected channel.
- Upload job history, status, errors and final Shorts URL.
- Responsive dark RTL interface.

## Telegram bot

Access is limited to IDs in `TELEGRAM_ADMIN_IDS`.

Commands:

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

`/connect` creates a cryptographically random one-time link. The link expires after `OAUTH_LINK_MINUTES` and starts the same Google OAuth connection flow used by the web panel.

To publish from Telegram:

1. Select the destination using `/channels`.
2. Send an Instagram Reel/Post URL.
3. Send the title.
4. The bot creates a persistent job and sends the YouTube Shorts URL when the upload completes.

## Google Cloud setup

1. Create a Google Cloud project.
2. Enable **YouTube Data API v3**.
3. Configure the OAuth consent screen.
4. Create a **Web application** OAuth Client ID.
5. Add this exact redirect URI:

```text
https://YOUR-DOMAIN.example/oauth/callback
```

6. Save the downloaded OAuth JSON as `client_secret.json`.
7. Set `PUBLIC_BASE_URL=https://YOUR-DOMAIN.example`.

To connect multiple Brand Channels, repeat **Connect channel** and select the intended Google/YouTube identity during OAuth.

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

Set at minimum:

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

Generate strong values:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Do not change `SECRET_KEY` / `TOKEN_ENCRYPTION_KEY` after channels have been connected unless you intentionally reconnect or migrate their encrypted credentials.

## Run

Panel:

```bash
gunicorn --workers=1 --threads=8 --bind=0.0.0.0:8080 --timeout=120 panel:app
```

Telegram bot in a second process:

```bash
python main.py
```

## Docker Compose

```bash
cp .env.example .env
# edit .env and add client_secret.json
docker compose up -d --build
```

The panel listens on port `8080`. Put it behind Nginx or Caddy with HTTPS in production. Google OAuth must use the same public HTTPS origin configured in `PUBLIC_BASE_URL`.

## Data model

- `youtube_channels`: encrypted credential set and settings per channel.
- `upload_jobs`: durable upload history and processing state.
- `telegram_preferences`: selected destination channel per Telegram admin.
- `oauth_requests`: short-lived Telegram-to-web OAuth links.
- `audit_logs`: panel management/security events.

SQLite is stored at `data/app.db` by default. WAL and foreign keys are enabled.

## Security

Never commit:

- `.env`
- `client_secret.json`
- Telegram bot tokens
- Instagram session IDs
- OAuth refresh/access tokens
- `data/app.db`

The repository intentionally contains only `.env.example`.

## Project layout

```text
.
├── config.py
├── db.py
├── downloader.py
├── jobs.py
├── main.py               # Telegram bot
├── models.py
├── panel.py              # Flask web panel
├── security.py
├── youtube.py
├── templates/
├── static/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Next upgrades

The architecture is ready for scheduled publishing, PostgreSQL, Redis/Celery/RQ, role-based panel users, analytics charts, upload retry policies, per-channel quotas and additional content sources.

## License

MIT. See `LICENSE`.
