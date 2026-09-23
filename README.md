# Reels to Shorts Bot

A Telegram bot that accepts an Instagram Reel/post URL, downloads the media with `yt-dlp`, asks for a title, and uploads the result to YouTube as a public Short using the YouTube Data API.

> Repository: `PEDIHS/yt`

## Features

- Telegram-based workflow for submitting Instagram URLs.
- Instagram Reel/post downloads through `yt-dlp`.
- Optional Instagram session support.
- YouTube OAuth authentication and token refresh.
- YouTube Shorts upload with configurable hashtags.
- Automatic cleanup of successfully processed local media.
- Environment-based configuration; secrets are not stored in Git.

## Architecture

```text
Telegram user
    |
    v
main.py
    |-- downloader.py  -> Instagram / yt-dlp -> downloads/
    |
    `-- uploader.py    -> YouTube Data API -> YouTube Shorts
```

## Requirements

- Python 3.10+
- FFmpeg available on `PATH` (recommended), or an explicit `FFMPEG_PATH`
- Telegram bot token
- Google OAuth Desktop App credentials with YouTube Data API enabled
- Instagram session ID when Instagram requires authenticated access

## Installation

```bash
git clone https://github.com/PEDIHS/yt.git
cd yt
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Install FFmpeg through your operating system package manager. Do not commit FFmpeg archives or binaries to this repository.

## Configuration

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=replace_me
INSTAGRAM_SESSIONID=replace_me
CLIENT_SECRET_FILE=client_secret.json
YOUTUBE_TOKEN_FILE=token.json
DEFAULT_HASHTAGS=#Shorts #YouTubeShorts #reels
FFMPEG_PATH=
```

Download your Google OAuth Desktop App credentials and save them locally as `client_secret.json`. The first successful OAuth flow will create `token.json` locally. Both files are ignored by Git.

## Run

```bash
python main.py
```

Then open the Telegram bot, send `/start`, submit an Instagram Reel/post URL, and send the desired title when prompted.

## Repository safety

Never commit or share:

- `.env`
- `TELEGRAM_BOT_TOKEN`
- `INSTAGRAM_SESSIONID`
- `client_secret.json`
- `token.json` / refresh tokens

If any of these values have been shared outside a trusted environment, rotate/revoke them before production use.

## Current development notes

This repository is an initial cleaned import of the existing bot. The current implementation is intentionally kept close to the original behavior so future improvements can be developed incrementally. Planned areas include access control, non-blocking job processing, queueing, stronger URL validation, progress reporting, better download isolation, tests, and deployment tooling.

## Project files

```text
.
├── .env.example
├── .github/workflows/ci.yml
├── .gitignore
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── SECURITY.md
├── downloader.py
├── main.py
├── requirements.txt
└── uploader.py
```

## License

MIT. See [LICENSE](LICENSE). The original license notice is preserved.
