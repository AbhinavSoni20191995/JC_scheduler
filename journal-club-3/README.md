# Journal Club Scheduler

A small self-hosted platform to run a weekly (Friday) lab journal club with an
automatic replacement-finding email workflow.

**License: non-commercial use only** — see `LICENSE`.

## What it does

- Generates a Friday schedule, rotating through all active participants and
  skipping holidays you enter.
- Each session has a **paper column** (title + link) and a short (≤ 280 chars)
  **speaker note** ("please read Fig. 3 first"), editable from the session page.
- **Automatic swap workflow** when a speaker marks themselves unavailable
  (with a short comment):
  1. An email goes to the whole-lab address asking for a volunteer, with a link.
  2. Anyone can click **"I'll take over"** — if the volunteer has a future slot,
     the two speakers swap dates; otherwise the slot is simply reassigned.
     A confirmation email goes to the lab.
  3. If nobody volunteers after *N* days (default 2), a **reminder** email is sent.
  4. If still unresolved *M* days before the session (default 1), the session is
     **cancelled automatically** and a cancellation email is sent.
- **Admin panel** (password protected): manage participants and their emails,
  the whole-lab notification address, holidays, timing settings, regenerate the
  schedule, reassign/cancel/restore sessions, and **compose your own email**.
- An **outbox** shows every email the system produced and whether it was delivered.

## Quick start

```bash
pip install flask
cp .env.example .env        # then edit it
python app.py
```

Open http://localhost:5000 — log in as admin (default password `admin`,
change it in `.env`!), add participants, add holidays, set the whole-lab
email, then click **Generate / extend Friday schedule**.

## Email setup

Emails are sent through any SMTP server (your university mail server, Gmail
with an app password, etc.). Edit `.env`:

```
SMTP_HOST=smtp.your-university.edu
SMTP_PORT=587
SMTP_USER=journalclub@your-lab.edu
SMTP_PASS=secret
MAIL_FROM=journalclub@your-lab.edu
BASE_URL=https://jc.your-lab.edu     # used in email links
ADMIN_PASSWORD=pick-something-strong
SECRET_KEY=any-long-random-string
```

Until SMTP is configured, the app works fully but emails only land in the
admin outbox (marked "not sent") so you can test everything safely.

## How the automation runs

A background thread checks every hour (and on each page load, as a safety
net) whether any "volunteer needed" session is due for its reminder or its
cancellation. All actions are date-gated and sent at most once, so restarts
are harmless. The database is a single SQLite file (`journal_club.db`) — back
it up by copying that file.

## Deploying on Railway (recommended if you don't have a lab server)

1. Push this folder to a GitHub repository (the included `.gitignore` keeps
   `.env` and the database out of git).
2. On Railway: **New Project → Deploy from GitHub repo** and pick the repo.
   Railway detects Python and uses the included `Procfile` automatically.
3. **Attach a volume** (right-click the service → Attach volume) with mount
   path `/data`. Without this, the SQLite database is erased on every
   redeploy because Railway's filesystem is ephemeral.
4. In the service **Variables**, set:
   ```
   JC_DB=/data/journal_club.db
   ADMIN_PASSWORD=pick-something-strong
   SECRET_KEY=any-long-random-string
   SMTP_HOST=smtp.your-university.edu
   SMTP_PORT=587
   SMTP_USER=journalclub@your-lab.edu
   SMTP_PASS=secret
   MAIL_FROM=journalclub@your-lab.edu
   BASE_URL=https://<your-app>.up.railway.app
   ```
   (On Railway there is no `.env` file — variables are set in the dashboard.)
5. Under **Settings → Networking**, click **Generate Domain**, then put that
   URL into the `BASE_URL` variable so email links point to the right place.

Every `git push` to the main branch redeploys automatically. Note that the
site is publicly reachable on Railway, so use a strong admin password; member
actions (mark unavailable / take over) are intentionally open, which is fine
for a lab tool but means you shouldn't share the URL beyond your lab.

## Deploying on your own machine

Any always-on machine works (lab server, Raspberry Pi, university VM). For
something more robust than the built-in server:

```bash
pip install flask gunicorn
gunicorn -w 1 app:app -b 0.0.0.0:5000
```

Keep `-w 1` (one worker) so only one background automation thread runs.
Put it behind your institute's reverse proxy / VPN since member actions
(marking unavailable, taking over) are open to anyone who can reach the page —
it is designed as an internal lab tool.
