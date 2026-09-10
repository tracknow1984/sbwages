# SB EMPIRE — Staff & Timesheets

A server-backed staff management and weekly timesheet app for SB EMPIRE, built for the `tracknow1984/sbwages` Render project.

## Included

- Admin tabs: Staff Details, Timesheets and Settings.
- Staff contact and emergency contact details; hourly rate in AUD; username; optional password; active/terminated status; fixed Monday–Sunday timesheets.
- Searchable staff table with edit and email invitation actions.
- Separate `/admin/login` and `/employee/login` pages.
- Employee contact updates, daily start/finish times and activity notes across all seven days, separate Save day and Commit day buttons, calculated hours, live totals and weekly submission.
- Committed days are locked on the server. Only administrators can correct or unlock them, with a required reason and a retained change history. Admins can see draft weeks as soon as a day is saved.
- All new timesheets run Monday–Sunday. Dates use Australia/Brisbane.
- Employees may submit at any time, including before Sunday when working a partial week. Entered days must be committed; days not worked can stay blank. Submitted records are locked and visible in the admin table and employee history.
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

The integration tests exercise staff creation/editing, employee field restrictions and isolation, both week schedules, calculations and rounding, locked submission snapshots, invalid inputs, CSRF, termination, invitation activation and replay protection, SMTP failure and login throttling. SMTP is mocked in tests; live email and Render deployment require configuration. The daily entry workflow is also checked in a local browser when browser dependencies are available.

All employees use Sunday week endings. Existing Friday-ending records keep their original dates and totals and remain accessible in timesheet history, including drafts. Dates already recorded there cannot be entered again in a Monday–Sunday sheet and are excluded from its totals. Payroll exports are not included.


## Daily commits and administrator corrections

- Each date has its own start time, finish time, activity notes and Save day / Commit day actions. Committing one date does not save other unsaved dates. The interface warns before discarding unsaved edits elsewhere.
- Hours are elapsed minutes converted to decimal hours, rounded to two places. No break time is deducted automatically. Finish must be later than start on the same date; split overnight shifts across dates. Blank times represent zero hours.
- Weekly submission requires every entered day with hours or notes to be committed. Blank days can remain empty. Committed days and submitted weeks reject employee edits even through direct requests.
- Admin: TIMESHEETS → View week → Edit / unlock. A correction stays locked and recalculates the total using the submitted rate if already submitted. Unlocking reopens the week as a draft and requires the employee to recommit and resubmit; draft estimates use the employee's current rate. Other committed days stay locked.
- Administrator changes store the reason, administrator, timestamp and previous day/week values in `entry_audit`. No records are deleted.
- Startup applies an additive SQLite migration for time fields and daily locks. Existing hours, submissions and accounts are preserved; historical submitted entries are marked locked without inventing start/finish times. Existing draft hours remain visible until the user supplies times and saves/commits the day.

## Staff documents and annual leave

- Staff DOCUMENTS accepts PDF, JPG and PNG files up to 5 MB. Files are stored privately in SQLite alongside existing data on the persistent volume; no public upload URLs are created. Backups must include the database and its WAL correctly.
- Admin lands on DASHBOARD with active document and pending leave queues. DOCUMENTS provides View, Archive and Email actions. Archived documents remain available to admin and their owner. Email asks admin for one recipient, attaches the file through the existing SMTP settings, and records successful sends. No automatic emails are sent on upload.
- ANNUAL LEAVE accepts inclusive first/last dates starting today or later. Pending/approved requests cannot overlap. Admin approves or disapproves once and can add comments; employees see status and comments in their tab. Review records admin and time. This is an approval workflow, not an accrual balance or payroll integration.
- Existing users, timesheets and licence details are retained through additive schema creation.

## EMPIRE WIRE and staff home

- Staff log in to HOME, showing their EMPIRE WIRE notice board, daily timesheet status, licence expiry and shortcuts. Licence alerts highlight expiry within 30 days, expiry today and overdue dates; missing dates link to My Details.
- Admin EMPIRE WIRE selects one active employee and sends a title, pasted job/information text and normal/urgent priority. The recipient alone sees the notice and can mark it as read. Admin sees read status and time; viewing the dashboard does not mark messages read.
- Notices are in-app, not SMS, email or device push. While staff have Home or EMPIRE WIRE open, the app checks every 30 seconds for new notices and shows a View updates prompt without discarding forms.
- Mobile navigation uses a compact grid; timesheets, document lists and leave requests become cards on narrow screens. Existing daily commit locks and early submission remain in place.

## Date format and timesheet grouping

- Dates are displayed as DD.MM.YYYY throughout the app; timestamps show DD.MM.YYYY · HH:MM in Brisbane time. Licence and leave date entry uses DD.MM.YYYY with an optional native calendar picker. Storage and internal URLs retain ISO dates.
- Admin timesheets and staff history are grouped by their recorded week ending, newest first. Each expandable week shows date range, total hours, total amount and draft/submitted counts; the newest week starts open. Existing Friday-ending records keep their original dates.

## Payment records and staff pay slips

- Admin opens a submitted individual timesheet → Process Payment → enters cash and transfer amounts → Process. The two amounts must exactly equal the submitted total; either can be zero. This records payment only and initiates no transfer.
- Each timesheet can have only one payment. A database uniqueness constraint and write transaction prevent duplicate processing. Paid timesheets cannot be corrected or unlocked, preserving their recorded total.
- Payment records snapshot staff name, week, hours, hourly rate, cash/transfer amounts, processing date and administrator. Later staff changes do not rewrite slips. Staff see their own Pay Slips, payment status in history and the latest payment on Home. Admin can view the same slip.
- Print / Save PDF uses the browser print dialog. Slips record the cash/transfer breakdown only; they do not calculate tax, superannuation or deductions.

## SB Empire branding

The header uses the SB EMPIRE .CO brand mark from the sbempire.co website icon asset (https://img1.wsimg.com/isteam/ip/9b8113f0-1359-42dd-8bb9-a95e43af7815/blob.png/:/rs=w:180,h:180,m), saved locally as static/sb-empire-logo.jpg. The visual palette is blue, black and grey; existing page layout and workflows are retained. The header and footer identify Staff, Timesheets & Operations.

The approved blue SB EMPIRE logo with “Building Opportunities and Delivering Solutions” is now used in the top header as static/sb-empire-header.webp. A clean white background replaces the supplied checkerboard, with responsive sizing and the Staff, Timesheets & Operations label retained.


## Staff prestarts

Staff use PRESTART to choose T595 BOBCAT, SUMITOMO EXCAVATOR, DEMAG ROLLER, MITSUBISHI RIGID or ACCO TIPPER. Machines record hour-meter readings; vehicles record odometer kilometres. Versioned checklist labels, answers, defect notes, employee name, drawn signature, declaration and Brisbane submission timestamp are saved together. Each check requires an answer, Fail/No requires a note, and only applicable optional checks accept N/A. Fit-for-duty Yes or No and an explicit signed acknowledgement are required; No can be submitted with notes so admin receives the concern.

Admin PRESTART lists completed signed records and has a failed-items/fitness-concerns filter with direct links. Staff can view only their own records. Submissions are immutable and duplicate submissions are deduplicated. No email is sent. These are basic operational checks to use alongside the equipment manual and site procedures; a submitted form is not a maintenance clearance or certification. This feature does not reset any existing data.

Prestart checks use compact Pass/Fail buttons (Yes/No for greasing), with N/A only for optional equipment. Notes are collapsed until needed. Each line supports one optional JPG/PNG/WebP photo, up to 5 MB and 20 megapixels (30 MB combined per form). Photos are decoded, orientation-corrected, resized and re-encoded without source metadata, then stored privately with the record. Signatures are drawn with touch, stylus or mouse; the server validates the stroke data and renders a PNG. Blank signatures are rejected. Original typed sign-offs remain readable on historical records. Photo and signature routes allow only admin or the submitting employee. On a validation error, staff must reselect their files; answers and drawn signature remain in the form.

Equipment also includes CAT D6 DOZER, HYSTER FORKLIFT and FORKFORCE 2.5T, with dozer/forklift-specific checks and hour-meter readings. One prestart per asset per Brisbane calendar day is accepted across all employees. Completed equipment is marked in the selector and shows the submitter/time when selected. A transaction locks the daily check and insert to prevent concurrent duplicate submissions. Existing history is retained. A completed equipment check does not transfer the signer's fitness-for-duty declaration to another operator.

The equipment selector groups Machines and Vehicles and immediately loads the appropriate checklist on selection, including the daily duplicate check. Machine choices are T595 BOBCAT, SUMITOMO EXCAVATOR, DEMAG ROLLER, CAT D6 DOZER, HYSTER FORKLIFT and FORKFORCE 2.5T; MITSUBISHI RIGID and ACCO TIPPER are vehicles.


## Timesheet archive and deletion

Admin timesheet rows include Archive (or Restore in the Archived view) and Delete beside View week. Archive only organises the admin list; staff history, committed locks, payment records and pay slips remain available. Delete requires a dedicated confirmation page and CSRF-protected POST. Deletion removes only the selected sheet, its daily entries and matching correction history, plus any linked payment/pay slip after explicit confirmation. If a payment is processed after the confirmation page was opened, deletion stops for a fresh review. Employee accounts and unrelated modules are retained.
