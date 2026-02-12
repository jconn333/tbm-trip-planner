# TBM App

Flask app for TBM trip planning and owner/admin reservation management.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Run app:

```bash
python app.py
```

Run tests:

```bash
PYTHONPATH=. pytest -q
```

## Production hardening notes

- Set `APP_ENV=production`.
- Set a strong `FLASK_SECRET_KEY` (app will fail fast in production if unset/default).
- Set `TBM_DB_PATH` to a persistent disk location.
- Keep `FLASK_DEBUG=false` in production.

Session and API protections:
- Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` in production.
- Authenticated mutating API routes require `X-CSRF-Token`.
- `X-Request-Id` is returned on every response for tracing.
- Basic rate limiting is enabled on login/admin write endpoints.

## UI regression test (date/time click-anywhere)

This project includes Playwright tests that guard date/time wrapper behavior.

One-command run:

```bash
./scripts/run_ui_regression.sh
```

Manual run:

```bash
source .venv/bin/activate
python -m playwright install chromium
PYTHONPATH=. pytest -q -k datetime_box_playwright
```

## Deploy smoke checks

```bash
curl -sSf https://<your-host>/health
curl -sS https://<your-host>/api/admin/system-metrics -H "Cookie: <admin-session-cookie>"
```

Manual smoke flow:
1. Login as owner
2. Create pending reservation
3. Login as admin
4. Approve reservation
5. Owner submits change request
6. Admin approve/deny change request

## SQLite backup/restore runbook

Backup:
```bash
sqlite3 "$TBM_DB_PATH" ".backup '${TBM_DB_PATH}.bak'"
```

Restore:
```bash
cp "${TBM_DB_PATH}.bak" "$TBM_DB_PATH"
```
