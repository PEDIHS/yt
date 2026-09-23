# Security

## Secrets

This project requires credentials for Telegram, Instagram, and Google/YouTube. Keep all secrets outside Git and load them through environment variables or local credential files.

The following files are intentionally ignored:

- `.env`
- `client_secret.json`
- `token.json`
- `token.pickle`
- Telegram session files

If a credential is accidentally committed or shared, removing the file from Git is not enough. Revoke or rotate the credential at its provider.

## Reporting

For security-sensitive findings, avoid publishing live credentials, tokens, session values, or private user data in a public issue. Reproduce the problem with redacted values.
