# SB EMPIRE — Staff & Timesheets

A server-backed staff management and weekly timesheet app for SB EMPIRE, built for the `tracknow1984/sbwages` Render project.

## Included

- Admin tabs: Staff Details, Timesheets and Settings.
- Staff contact and emergency contact details; hourly rate in AUD; username; optional password; active/terminated status; Friday/Sunday week ending.
- Searchable staff table with edit and email invitation actions.
- Separate `/admin/login` and `/employee/login` pages.
- Employee contact updates, daily hours and activity notes across all seven days, draft saving, live totals and weekly submission.
- Friday weeks run Saturday–Friday; Sunday weeks run Monday–Sunday. Dates use Australia/Brisbane.
- Employees may submit on or after the week-ending day. Submitted records are locked and visible in the admin table and employee history.
- Submission snapshots the rate, hours and weekly amount. Later rate changes do not rewrite submitted records.
- Hashed passwords, server-side role and ownership checks, CSRF protection, login throttling and single-use 48-hour password setup invitations. Termination revokes existing employee sessions.

The weekly amount is base hourly rate × hours, rounded to cents. This is a timesheet tool, not a complete Australian payroll engine; it does not calculate overtime, awards, allowances, tax, leave or superannuation.

## Deploy to Render

The repository contains `render.yaml` for a Python web service with a persistent disk. This configuration requests a paid Starter service and disk; review Render's displayed charges before creating it. No Render deployment is created by committing this repository.

1. In Render, create a **Blueprint** and connect `tracknow1984/sbwages`.
2. Set `ADMIN_EMAIL` and a unique `ADMIN_PASSWORD` of at least 12 characters. The initial username is `admin` unless changed via `ADMIN_USERNAME`.
3. Set `APP_URL` to the actual HTTPS Render URL. If it is not known yet, set it after the service is created, before inviting staff.
4. Configure the SMTP variables below when ready to send email. The app works without SMTP; invitation attempts explain that setup is required. No staff invites are sent automatically.
5. Open `/admin/login`, add a staff member, then use **Email invite**. Employees use `/employee/login`.

For a manually configured Render web service:

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn 'app:create_app()' --bind 0.0.0.0:$PORT --workers 1 --threads 4`
- Health check: `/health`
- Persistent disk mount: `/var/data`
- `DATABASE_PATH=/var/data/sbwages.db`
- Generate a random `SECRET_KEY` of at least 32 characters and set the remaining environment variables from `.env.example`.

Do not use an ephemeral filesystem for live staff records. SQLite is configured for one service instance with a persistent disk. Back up the database using SQLite's backup API, and verify restoration before relying on it for business records. SQLite WAL files must not be copied independently as a backup. Multiple service replicas would require a shared database architecture.

The first admin is created only if no admin exists. Changing `ADMIN_PASSWORD` later does not reset the saved account. Use a controlled database maintenance procedure for admin password recovery; do not delete the live database. Keep the signing key stable across deployments unless intentionally revoking sessions.

## Email configuration

Set these in Render's environment, never in GitHub source:

| Variable | Purpose |
|---|---|
| `SMTP_HOST` | Email provider's SMTP server |
| `SMTP_PORT` | Usually 587 for STARTTLS, or 465 for implicit TLS |
| `SMTP_USERNAME` | SMTP account username |
| `SMTP_PASSWORD` | Provider SMTP credential or app password |
| `SMTP_FROM` | Provider-authorised sender address |
| `SMTP_SSL` | `true` for implicit TLS; otherwise STARTTLS is required |
| `APP_URL` | Actual HTTPS app origin, without a trailing slash |

The SMTP integration uses password/app-password authentication over TLS. Providers requiring OAuth-only SMTP need a provider-supported SMTP relay or a future OAuth integration. Settings show configuration presence, not a verified connection. An email is reported sent only when SMTP accepts it; inbox delivery depends on the provider. Failed sends invalidate the new link and show an error. Re-inviting staff sends a new password setup link; raw passwords are never emailed or shown in the staff table.

## Local development

Requires Python 3.12. Install `requirements.txt`, export environment variables from `.env.example` with your own values, and set `COOKIE_SECURE=false` only for local HTTP. `.env` is not loaded automatically.

```sh
python -m flask --app 'app:create_app()' run
```

No demo accounts or staff data are seeded. Only the configured admin is created. Do not commit database files, passwords or real staff records.

## Validation

```sh
python -m unittest discover -s tests -v
node --check static/app.js
```

The integration tests exercise staff creation/editing, employee field restrictions and isolation, both week schedules, calculations and rounding, locked submission snapshots, invalid inputs, CSRF, termination, invitation activation and replay protection, SMTP failure and login throttling. SMTP is mocked in tests; live email and Render deployment require configuration. Browser visual testing is not included.

Changing a staff member's week ending is blocked while drafts exist, and overlapping weeks are rejected to prevent duplicate hours. Choose the correct week ending before entering hours. Submitted corrections/reopening and payroll exports are not part of this first version.
