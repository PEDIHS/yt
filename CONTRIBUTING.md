# Contributing

Development should be done in small, reviewable branches.

## Local setup

1. Create a virtual environment.
2. Install `requirements.txt`.
3. Copy `.env.example` to `.env` and provide local credentials.
4. Keep generated media, OAuth tokens, sessions, and credentials out of Git.

## Before committing

```bash
python -m compileall -q main.py downloader.py uploader.py
```

Prefer focused commits and document behavior changes in the pull request description.
