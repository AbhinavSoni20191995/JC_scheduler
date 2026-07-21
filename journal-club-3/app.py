"""
Journal Club Scheduler
A small self-hosted platform to schedule a weekly (Friday) lab journal club,
with an automatic swap / reminder / cancellation email workflow.

License: Non-commercial use only. See LICENSE.
"""

import os
import re
import sqlite3
import smtplib
import threading
import time
from email.mime.text import MIMEText
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, flash, abort)

# ---------------------------------------------------------------- config ----

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv():
    """Tiny .env loader (no external dependency)."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-please")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Journal Club")
MAIL_REPLY_TO = os.environ.get("MAIL_REPLY_TO", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER or "journal-club@localhost")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").strip().rstrip("/")
if BASE_URL and not BASE_URL.startswith(("http://", "https://")):
    BASE_URL = "https://" + BASE_URL

# Database location. On Railway set JC_DB=/data/journal_club.db with a volume
# mounted at /data. If the directory is missing (volume not mounted) we create
# it, warn loudly, and keep running instead of crash-looping.
DB_PATH = os.environ.get("JC_DB", os.path.join(BASE_DIR, "journal_club.db"))
try:
    _db_dir = os.path.dirname(DB_PATH)
    if _db_dir and not os.path.isdir(_db_dir):
        print(f"WARNING: '{_db_dir}' does not exist — creating it. If this is "
              f"supposed to be a persistent volume it is NOT mounted, and data "
              f"will be lost on redeploy. Check the volume's mount path.")
        os.makedirs(_db_dir, exist_ok=True)
    with open(DB_PATH, "a"):
        pass
except OSError as e:
    print(f"WARNING: cannot use database at {DB_PATH} ({e}); "
          f"falling back to a local file. Attach a volume for persistence!")
    DB_PATH = os.path.join(BASE_DIR, "journal_club.db")

# ------------------------------------------------------------------ db ------

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    beers INTEGER NOT NULL DEFAULT 0,
    once_only INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS holidays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL UNIQUE,          -- ISO date
    label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,                 -- ISO date (a Friday)
    role TEXT NOT NULL DEFAULT 'jc',   -- 'jc' | 'progress'
    speaker_id INTEGER,
    original_speaker_id INTEGER,
    status TEXT NOT NULL DEFAULT 'scheduled',
        -- scheduled | needs_swap | cancelled
    unavailable_comment TEXT DEFAULT '',
    swap_requested_at TEXT,            -- ISO datetime
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    paper_title TEXT DEFAULT '',
    paper_link TEXT DEFAULT '',
    speaker_note TEXT DEFAULT '',
    UNIQUE(day, role)
);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    link TEXT DEFAULT '',
    suggested_by TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    day TEXT PRIMARY KEY,
    room TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    error TEXT DEFAULT ''
);
"""

DEFAULT_SETTINGS = {
    "lab_email": "",            # single mass-notification address
    "reminder_after_days": "2", # reminder if no volunteer after N days
    "cancel_before_days": "1",  # cancel N days before session if no volunteer
    "horizon_weeks": "16",      # how far ahead to generate the schedule
    "schedule_start": "",       # generate no sessions before this date (optional)
    "beer_window_days": "7",    # change requested within N days of the talk = owes a beer
    "notify_mode": "list",      # 'list' = whole-lab email; 'individuals' = every participant
    "extra_emails": "",         # non-presenting recipients (supervisor etc.), comma-separated
    "meeting_info": "",         # e.g. "Fridays 9:00, CRTD seminar room 3"
    "meeting_link": "",         # e.g. BBB / Zoom URL
    "jc_start_id": "",          # who starts the JC rotation on next generate (one-shot)
    "progress_start_id": "",    # who starts the progress rotation on next generate (one-shot)
    "lab_name": "Journal Club",
}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


ROLE_LABELS = {"jc": "Journal club", "progress": "Progress report"}


def migrate(db):
    """Upgrade databases created before the 'role' column existed."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(sessions)")]
    if "role" not in cols:
        db.executescript("""
            ALTER TABLE sessions RENAME TO sessions_old;
        """)
        db.executescript(SCHEMA)
        db.execute("""
            INSERT INTO sessions (id, day, role, speaker_id, original_speaker_id,
                status, unavailable_comment, swap_requested_at, reminder_sent,
                paper_title, paper_link, speaker_note)
            SELECT id, day, 'jc', speaker_id, original_speaker_id,
                status, unavailable_comment, swap_requested_at, reminder_sent,
                paper_title, paper_link, speaker_note
            FROM sessions_old
        """)
        db.execute("DROP TABLE sessions_old")
        db.commit()


# Saxony (DE) legal holidays that fall on a FRIDAY in the scheduling window.
# Seeded once; the admin can remove or add dates freely (e.g. lab retreats,
# Christmas Eve / New Year's Eve 2027, which are Fridays but not legal holidays).
SEED_HOLIDAYS = {
    "2026-12-25": "1. Weihnachtstag",
    "2027-01-01": "Neujahr",
    "2027-03-26": "Karfreitag",
}


# One-time sync with the lab's existing Excel rotation (applied once per flag
# version, then never again — edit freely afterwards). Names must match
# participant names. Bumping the version re-applies the seed on next start.
EXCEL_SEED_FLAG = "excel_seed_v1"
EXCEL_SEED = {
    # day:        (journal club,   progress report — also F2F on Monday)
    "2026-07-17": ("Daniela",      "Jessica"),
    "2026-07-24": ("Michalis",     "Abhinav Soni"),
    "2026-07-31": ("Jiffin",       "Lucas"),
    "2026-08-07": ("Niklas",       "Daniela"),
    "2026-08-14": ("Yann",         "Michalis"),
    "2026-08-21": ("Pia",          "Niklas"),
}


def apply_excel_seed(db):
    done = db.execute("SELECT value FROM settings WHERE key=?", (EXCEL_SEED_FLAG,)).fetchone()
    if done:
        return
    people = {r[0]: r[1] for r in db.execute("SELECT name, id FROM participants")}
    matched = 0
    for day, (jc_name, pr_name) in EXCEL_SEED.items():
        for role, name in (("jc", jc_name), ("progress", pr_name)):
            pid = people.get(name)
            if not pid:
                continue  # participant not created yet; retried on next startup
            db.execute("""
                INSERT INTO sessions (day, role, speaker_id, status)
                VALUES (?, ?, ?, 'scheduled')
                ON CONFLICT(day, role) DO UPDATE SET
                    speaker_id = excluded.speaker_id, status = 'scheduled'
            """, (day, role, pid))
            matched += 1
    if matched:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
                   (EXCEL_SEED_FLAG,))
    db.commit()


def migrate2(db):
    """Add columns introduced after the first release."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(participants)")]
    if "beers" not in cols:
        db.execute("ALTER TABLE participants ADD COLUMN beers INTEGER NOT NULL DEFAULT 0")
    if "once_only" not in cols:
        db.execute("ALTER TABLE participants ADD COLUMN once_only INTEGER NOT NULL DEFAULT 0")
    db.commit()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    migrate(db)
    migrate2(db)
    for k, v in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    for day, label in SEED_HOLIDAYS.items():
        db.execute("INSERT OR IGNORE INTO holidays (day, label) VALUES (?, ?)", (day, label))
    db.commit()
    apply_excel_seed(db)
    db.close()


def setting(key):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(key, value):
    db = get_db()
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    db.commit()

# --------------------------------------------------------------- email ------


def _smtp_send(recipients, subject, body):
    from email.utils import formataddr
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM))
    msg["To"] = ", ".join(recipients)
    if MAIL_REPLY_TO:
        msg["Reply-To"] = MAIL_REPLY_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(MAIL_FROM, recipients, msg.as_string())


def _brevo_send(recipients, subject, body):
    """Send via Brevo's HTTPS API (works on hosts that block SMTP ports,
    e.g. Railway free/hobby plans)."""
    import json
    import urllib.request
    import urllib.error
    payload = {
        "sender": {"email": MAIL_FROM, "name": MAIL_FROM_NAME},
        "to": [{"email": r} for r in recipients],
        "subject": subject,
        "textContent": body,
    }
    if MAIL_REPLY_TO:
        payload["replyTo"] = {"email": MAIL_REPLY_TO}
    payload = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email", data=payload, method="POST",
        headers={"api-key": BREVO_API_KEY,
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status not in (200, 201, 202):
                raise RuntimeError(f"Brevo returned HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"Brevo HTTP {e.code}: {detail}") from e


def send_email(recipient, subject, body, db=None):
    """Send an email to one address or a list of addresses; always log it to
    the outbox. Never crash the app. Provider order: Brevo HTTPS API (if
    BREVO_API_KEY set), else SMTP."""
    if isinstance(recipient, str):
        recipients = [recipient] if recipient else []
    else:
        recipients = [r for r in (recipient or []) if r]
    recipients = list(dict.fromkeys(recipients))  # dedupe, keep order
    if not recipients:
        return False
    own_conn = db is None
    if own_conn:
        db = sqlite3.connect(DB_PATH)
    sent, error = 0, ""
    if BREVO_API_KEY:
        try:
            _brevo_send(recipients, subject, body)
            sent = 1
        except Exception as e:
            error = str(e)
    elif SMTP_HOST:
        try:
            _smtp_send(recipients, subject, body)
            sent = 1
        except Exception as e:  # log, don't crash
            error = str(e)
    else:
        error = ("No email provider configured "
                 "(set BREVO_API_KEY or SMTP_HOST — see README)")
    recipient = ", ".join(recipients)
    db.execute("INSERT INTO outbox (created_at, recipient, subject, body, sent, error) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               (datetime.now().isoformat(timespec="seconds"),
                recipient, subject, body, sent, error))
    db.commit()
    if own_conn:
        db.close()
    return bool(sent)

# ------------------------------------------------------------ scheduling ----


def next_friday(from_day):
    days_ahead = (4 - from_day.weekday()) % 7  # Friday = 4
    if days_ahead == 0:
        days_ahead = 7
    return from_day + timedelta(days=days_ahead)


def generate_schedule(db):
    """Append sessions for Fridays up to the horizon, skipping holidays.
    Every Friday gets a journal-club speaker AND a progress-report speaker,
    rotated so the same person never has both on the same day. Participants
    marked "once only" get exactly one JC and one progress slot, then are
    skipped. If a start person is set for a track (one-shot setting), the
    rotation begins with them and the setting is cleared."""
    people = db.execute(
        "SELECT * FROM participants WHERE active = 1 ORDER BY position, id").fetchall()
    if not people:
        return 0
    holidays = {r["day"] for r in db.execute("SELECT day FROM holidays")}
    existing = {(r["day"], r["role"]) for r in db.execute("SELECT day, role FROM sessions")}

    horizon = int(setting("horizon_weeks") or 16)
    ids = [p["id"] for p in people]
    by_id = {p["id"]: p for p in people}
    n = len(ids)

    # once-only bookkeeping: roles each person already has (past or future)
    used = {"jc": set(), "progress": set()}
    for r in db.execute("SELECT speaker_id, role FROM sessions "
                        "WHERE speaker_id IS NOT NULL AND status != 'cancelled'"):
        used[r["role"]].add(r["speaker_id"])

    def eligible(pid, role):
        return not (by_id[pid]["once_only"] and pid in used[role])

    def pick(role, idx, avoid_id):
        """Next eligible speaker for role from idx; avoid double duty if
        possible. Returns (speaker_id or None, next idx)."""
        for relax_conflict in (False, True):
            j = idx
            for _ in range(n):
                pid = ids[j]
                if eligible(pid, role) and (relax_conflict or n == 1 or pid != avoid_id):
                    return pid, (j + 1) % n
                j = (j + 1) % n
        return None, idx  # nobody eligible (all once-only people used up)

    def start_index(role, default):
        # one-shot start person set by the admin?
        override = setting(f"{'jc' if role == 'jc' else 'progress'}_start_id")
        if override:
            db.execute("UPDATE settings SET value='' WHERE key=?",
                       (f"{'jc' if role == 'jc' else 'progress'}_start_id",))
            db.commit()
            try:
                pid = int(override)
                if pid in ids:
                    return ids.index(pid)
            except ValueError:
                pass
        # otherwise continue after the last assigned speaker of this role
        last = db.execute(
            "SELECT speaker_id FROM sessions WHERE role=? AND speaker_id IS NOT NULL "
            "ORDER BY day DESC LIMIT 1", (role,)).fetchone()
        if last and last["speaker_id"] in ids:
            return (ids.index(last["speaker_id"]) + 1) % n
        return default % n

    jc_idx = start_index("jc", 0)
    pr_idx = start_index("progress", jc_idx + max(1, n // 2))

    day = next_friday(date.today())
    start_from = setting("schedule_start")
    if start_from:
        try:
            wanted = date.fromisoformat(start_from)
            while day < wanted:
                day += timedelta(days=7)
        except ValueError:
            pass
    created = 0
    for _ in range(horizon):
        iso = day.isoformat()
        if iso in holidays:
            for role in ("jc", "progress"):
                if (iso, role) not in existing:
                    db.execute("INSERT INTO sessions (day, role, status, unavailable_comment) "
                               "VALUES (?, ?, 'cancelled', 'Holiday')", (iso, role))
                    created += 1
        else:
            row_jc = db.execute("SELECT speaker_id FROM sessions WHERE day=? AND role='jc'",
                                (iso,)).fetchone()
            row_pr = db.execute("SELECT speaker_id FROM sessions WHERE day=? AND role='progress'",
                                (iso,)).fetchone()
            jc_speaker = row_jc["speaker_id"] if row_jc else None
            pr_speaker = row_pr["speaker_id"] if row_pr else None
            if row_jc is None and (iso, "jc") not in existing:
                jc_speaker, jc_idx = pick("jc", jc_idx, pr_speaker)
                db.execute("INSERT INTO sessions (day, role, speaker_id) VALUES (?, 'jc', ?)",
                           (iso, jc_speaker))
                if jc_speaker:
                    used["jc"].add(jc_speaker)
                created += 1
            if row_pr is None and (iso, "progress") not in existing:
                pr_speaker, pr_idx = pick("progress", pr_idx, jc_speaker)
                db.execute("INSERT INTO sessions (day, role, speaker_id) VALUES (?, 'progress', ?)",
                           (iso, pr_speaker))
                if pr_speaker:
                    used["progress"].add(pr_speaker)
                created += 1
        day += timedelta(days=7)
    db.commit()
    return created

# ------------------------------------------------- swap / reminder logic ----


def session_by_id(db, sid):
    return db.execute("SELECT s.*, p.name AS speaker_name, p.email AS speaker_email "
                      "FROM sessions s LEFT JOIN participants p ON p.id = s.speaker_id "
                      "WHERE s.id = ?", (sid,)).fetchone()


def request_swap(db, sess, comment):
    lab_email = lab_recipients(db)
    role = ROLE_LABELS.get(sess["role"], "session")
    db.execute("UPDATE sessions SET status='needs_swap', unavailable_comment=?, "
               "swap_requested_at=?, reminder_sent=0, original_speaker_id=speaker_id "
               "WHERE id=?",
               (comment, datetime.now().isoformat(timespec="seconds"), sess["id"]))

    # short-notice change → the speaker owes the lab a beer
    beer_line = ""
    try:
        beer_window = int(setting("beer_window_days") or 7)
    except ValueError:
        beer_window = 7
    days_left = (date.fromisoformat(sess["day"]) - date.today()).days
    if days_left <= beer_window and sess["speaker_id"]:
        db.execute("UPDATE participants SET beers = beers + 1 WHERE id=?",
                   (sess["speaker_id"],))
        beer_line = (f"\nHouse rule: this change comes {days_left} day(s) before the "
                     f"talk, so {sess['speaker_name']} owes the lab a beer. 🍺")
    db.commit()

    f2f_line = ""
    if sess["role"] == "progress":
        monday = (date.fromisoformat(sess["day"]) + timedelta(days=3)).isoformat()
        f2f_line = f"\n(The progress-report slot includes the F2F meeting on Monday {monday}.)"

    link = f"{BASE_URL}/session/{sess['id']}"
    send_email(
        lab_email,
        f"[{setting('lab_name')}] {role} speaker needed for {sess['day']}",
        f"{sess['speaker_name']} is not available for the {role.lower()} on "
        f"Friday {sess['day']}.\n"
        f"Reason: {comment or '—'}{f2f_line}{beer_line}\n\n"
        f"Can anyone take over this slot? Volunteer here:\n{link}\n\n"
        f"If nobody volunteers, a reminder will be sent, and the session may be "
        f"cancelled automatically.",
        db=db)


def participant_emails(db):
    """All active participants' own emails (used by manual reminders that must
    reach individuals even when the list is the normal channel)."""
    rows = db.execute("SELECT email FROM participants "
                      "WHERE active=1 AND COALESCE(email,'') != ''").fetchall()
    emails = [r["email"] for r in rows]
    extras_row = db.execute("SELECT value FROM settings WHERE key='extra_emails'").fetchone()
    extras = extras_row["value"] if extras_row else ""
    emails += [x.strip() for x in extras.replace(";", ",").split(",") if x.strip()]
    return emails


def lab_recipients(db):
    """Where lab-wide notifications go: the list address (or every active
    participant's own email when notify_mode is 'individuals'), plus any
    extra non-presenting recipients such as the supervisor."""
    def _get(key):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")
    recipients = []
    if _get("notify_mode") == "individuals":
        rows = db.execute("SELECT email FROM participants "
                          "WHERE active=1 AND COALESCE(email,'') != ''").fetchall()
        recipients = [r["email"] for r in rows]
    if not recipients and _get("lab_email"):
        recipients = [_get("lab_email")]
    extras = [x.strip() for x in
              _get("extra_emails").replace(";", ",").split(",") if x.strip()]
    return recipients + extras


def other_role_speaker(db, day, role):
    """Who presents the *other* track on this day (or None)."""
    other = "progress" if role == "jc" else "jc"
    row = db.execute("SELECT speaker_id FROM sessions WHERE day=? AND role=? "
                     "AND status!='cancelled'", (day, other)).fetchone()
    return row["speaker_id"] if row else None


def take_over(db, sess, volunteer_id):
    """Volunteer takes the slot. If the volunteer has a future scheduled slot
    of the same kind, the two speakers are swapped so the original speaker
    presents later. Double duty (JC + progress on one day) is allowed but
    flagged in the confirmation email."""
    volunteer = db.execute("SELECT * FROM participants WHERE id=?", (volunteer_id,)).fetchone()
    if not volunteer:
        return False
    role = ROLE_LABELS.get(sess["role"], "session")
    original_id = sess["speaker_id"]
    future = db.execute(
        "SELECT * FROM sessions WHERE speaker_id=? AND role=? AND status='scheduled' "
        "AND day>? ORDER BY day LIMIT 1", (volunteer_id, sess["role"], sess["day"])).fetchone()
    db.execute("UPDATE sessions SET speaker_id=?, status='scheduled' WHERE id=?",
               (volunteer_id, sess["id"]))
    swapped = ""
    if future and original_id:
        db.execute("UPDATE sessions SET speaker_id=? WHERE id=?", (original_id, future["id"]))
        swapped = (f"\nIn exchange, {sess['speaker_name']} takes over "
                   f"{volunteer['name']}'s {role.lower()} slot on {future['day']}.")
    double = ""
    if other_role_speaker(db, sess["day"], sess["role"]) == volunteer_id:
        double = (f"\nNote: {volunteer['name']} now presents BOTH the journal club "
                  f"and the progress report on {sess['day']}. If someone can take "
                  f"one of the two, please volunteer on the site.")
    f2f_line = ""
    if sess["role"] == "progress":
        monday = (date.fromisoformat(sess["day"]) + timedelta(days=3)).isoformat()
        f2f_line = f"\nThis slot includes the F2F meeting on Monday {monday}."
    db.commit()
    send_email(
        lab_recipients(db),
        f"[{setting('lab_name')}] {sess['day']} {role.lower()}: {volunteer['name']} will present",
        f"{volunteer['name']} has volunteered to take over the {role.lower()} on "
        f"Friday {sess['day']} from {sess['speaker_name']}.{swapped}{double}{f2f_line}\n\n"
        f"Schedule: {BASE_URL}",
        db=db)
    return True


def run_automation(db=None):
    """Daily housekeeping: send reminders and cancel unresolved sessions.
    Safe to run any number of times."""
    own = db is None
    if own:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row

    def _setting(key):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

    today = date.today()
    remind_after = int(_setting("reminder_after_days") or 2)
    cancel_before = int(_setting("cancel_before_days") or 1)
    lab_email = lab_recipients(db)
    lab_name = _setting("lab_name")

    rows = db.execute(
        "SELECT s.*, p.name AS speaker_name FROM sessions s "
        "LEFT JOIN participants p ON p.id = s.original_speaker_id "
        "WHERE s.status = 'needs_swap' AND s.day >= ?", (today.isoformat(),)).fetchall()

    for s in rows:
        day = date.fromisoformat(s["day"])
        requested = datetime.fromisoformat(s["swap_requested_at"]) if s["swap_requested_at"] else None
        link = f"{BASE_URL}/session/{s['id']}"
        role = ROLE_LABELS.get(s["role"], "session").lower()

        # final step: cancel shortly before the date
        if (day - today).days <= cancel_before:
            db.execute("UPDATE sessions SET status='cancelled' WHERE id=?", (s["id"],))
            db.commit()
            send_email(
                lab_email,
                f"[{lab_name}] {role.capitalize()} on {s['day']} is cancelled",
                f"No one was able to take over the {role} on Friday {s['day']} "
                f"(original speaker: {s['speaker_name']}).\n"
                f"The session has been cancelled. The rest of the schedule is unchanged:\n"
                f"{BASE_URL}",
                db=db)
            continue

        # middle step: one reminder
        if (not s["reminder_sent"] and requested
                and datetime.now() - requested >= timedelta(days=remind_after)):
            db.execute("UPDATE sessions SET reminder_sent=1 WHERE id=?", (s["id"],))
            db.commit()
            send_email(
                lab_email,
                f"[{lab_name}] Reminder: {role} speaker still needed for {s['day']}",
                f"Still looking for someone to take over the {role} on "
                f"Friday {s['day']} ({s['speaker_name']} is unavailable).\n\n"
                f"Volunteer here: {link}\n\n"
                f"If nobody volunteers, the session will be cancelled "
                f"{cancel_before} day(s) before the date.",
                db=db)

    if own:
        db.close()


def automation_loop():
    while True:
        try:
            run_automation()
        except Exception as e:
            print("automation error:", e)
        time.sleep(3600)  # hourly check; actions are date-gated so this is idempotent

# ---------------------------------------------------------------- auth ------


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper

# --------------------------------------------------------------- routes -----


@app.route("/")
def index():
    db = get_db()
    run_automation(db)  # lazy safety net in case the background thread died
    today = date.today().isoformat()

    q = ("SELECT s.*, p.name AS speaker_name, p.beers AS speaker_beers, "
         "o.name AS original_name FROM sessions s "
         "LEFT JOIN participants p ON p.id = s.speaker_id "
         "LEFT JOIN participants o ON o.id = s.original_speaker_id ")

    def grouped(rows):
        days = {}
        for s in rows:
            entry = days.setdefault(s["day"], {"monday": (
                date.fromisoformat(s["day"]) + timedelta(days=3)).isoformat()})
            entry[s["role"]] = s
        return days

    upcoming = grouped(db.execute(
        q + "WHERE s.day >= ? ORDER BY s.day", (today,)).fetchall())
    past = grouped(db.execute(
        q + "WHERE s.day < ? ORDER BY s.day DESC LIMIT 50", (today,)).fetchall())

    next_day = None
    for d, pair in upcoming.items():
        if any(k in ("jc", "progress") and pair[k]["status"] != "cancelled" for k in pair):
            next_day = d
            break

    people = db.execute(
        "SELECT * FROM participants WHERE active=1 ORDER BY position, id").fetchall()
    rooms = {r["day"]: r["room"] for r in db.execute("SELECT day, room FROM rooms")}
    return render_template("index.html", upcoming=upcoming, past=past, rooms=rooms,
                           next_day=next_day, people=people, today=today,
                           meeting_info=setting("meeting_info"),
                           meeting_link=setting("meeting_link"),
                           lab_name=setting("lab_name"))


@app.route("/session/<int:sid>", methods=["GET", "POST"])
def session_view(sid):
    db = get_db()
    sess = session_by_id(db, sid)
    if not sess:
        abort(404)
    people = db.execute("SELECT * FROM participants WHERE active=1 ORDER BY position, id").fetchall()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "paper":
            title = request.form.get("paper_title", "")[:300]
            link = request.form.get("paper_link", "")[:500]
            note = request.form.get("speaker_note", "")[:280]
            if link and not re.match(r"^https?://", link):
                link = "https://" + link
            db.execute("UPDATE sessions SET paper_title=?, paper_link=?, speaker_note=? "
                       "WHERE id=?", (title, link, note, sid))
            db.commit()
            flash("Paper details saved.")
        elif action == "unavailable" and sess["status"] == "scheduled" and sess["speaker_id"]:
            comment = request.form.get("comment", "")[:280]
            request_swap(db, sess, comment)
            flash("Marked as unavailable — the lab has been asked for a volunteer.")
        elif action == "takeover" and sess["status"] == "needs_swap":
            volunteer_id = request.form.get("volunteer_id", type=int)
            if volunteer_id and volunteer_id != sess["speaker_id"]:
                take_over(db, sess, volunteer_id)
                flash("Thanks for taking over — a confirmation email was sent to the lab.")
            else:
                flash("Please pick a name.")
        return redirect(url_for("session_view", sid=sid))

    return render_template("session.html", s=sess, people=people,
                           conflict_id=other_role_speaker(db, sess["day"], sess["role"]),
                           role_label=ROLE_LABELS.get(sess["role"], "Session"),
                           lab_name=setting("lab_name"), today=date.today().isoformat())

# ------------------------------------------------------------ admin ---------


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("admin"))
        flash("Wrong password.")
    return render_template("login.html", lab_name=setting("lab_name"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin/changes")
@admin_required
def admin_changes():
    db = get_db()
    today = date.today().isoformat()
    pending = db.execute(
        "SELECT s.*, p.name AS speaker_name, o.name AS original_name "
        "FROM sessions s LEFT JOIN participants p ON p.id=s.speaker_id "
        "LEFT JOIN participants o ON o.id=s.original_speaker_id "
        "WHERE s.status='needs_swap' AND s.day >= ? ORDER BY s.day", (today,)).fetchall()
    emails = participant_emails(db)
    return render_template("changes.html", pending=pending, emails=emails,
                           roles=ROLE_LABELS, lab_name=setting("lab_name"))


@app.route("/admin/remind/<int:sid>", methods=["POST"])
@admin_required
def admin_remind(sid):
    """Send a reminder to each participant's own email asking someone to take
    over this specific slot. Bypasses the mailing list on purpose."""
    db = get_db()
    s = db.execute("SELECT s.*, o.name AS original_name FROM sessions s "
                   "LEFT JOIN participants o ON o.id=s.original_speaker_id "
                   "WHERE s.id=?", (sid,)).fetchone()
    if not s:
        abort(404)
    who = s["original_name"] or "the assigned speaker"
    role = ROLE_LABELS.get(s["role"], "session").lower()
    link = f"{BASE_URL}/session/{s['id']}"
    recipients = participant_emails(db)
    note = request.form.get("note", "").strip()
    body_parts = [f"{who} needs to swap out of the {role} on Friday {s['day']}."]
    if note:
        body_parts.append(note)
    body_parts.append(f"If you can take this slot, please volunteer here:\n{link}")
    body_parts.append("(Reminder sent by the admin.)")
    ok = send_email(
        recipients,
        f"[{setting('lab_name')}] Can anyone take the {role} on {s['day']}?",
        "\n\n".join(body_parts),
        db=db)
    db.execute("UPDATE sessions SET reminder_sent=1 WHERE id=?", (sid,))
    db.commit()
    flash("Reminder sent to all listed participants." if ok
          else "Could not send — check the outbox for the error.")
    return redirect(url_for("admin_changes"))


@app.route("/admin/remind_all", methods=["POST"])
@admin_required
def admin_remind_all():
    """One reminder listing every pending change, to all participant emails."""
    db = get_db()
    today = date.today().isoformat()
    pending = db.execute(
        "SELECT s.*, o.name AS original_name FROM sessions s "
        "LEFT JOIN participants o ON o.id=s.original_speaker_id "
        "WHERE s.status='needs_swap' AND s.day >= ? ORDER BY s.day", (today,)).fetchall()
    if not pending:
        flash("No pending changes to remind about.")
        return redirect(url_for("admin_changes"))
    lines = []
    for s in pending:
        role = ROLE_LABELS.get(s["role"], "session").lower()
        who = s["original_name"] or "assigned speaker"
        lines.append(f"- {s['day']} ({role}): {who} needs a swap "
                     f"-> {BASE_URL}/session/{s['id']}")
    body = ("These journal club / progress report slots still need someone to take over:\n\n"
            + "\n".join(lines)
            + "\n\nClick a link above to volunteer. Thanks!")
    ok = send_email(participant_emails(db),
                    f"[{setting('lab_name')}] {len(pending)} slot(s) need a volunteer",
                    body, db=db)
    flash("Reminder sent to all listed participants." if ok
          else "Could not send — check the outbox for the error.")
    return redirect(url_for("admin_changes"))


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    people = db.execute("SELECT * FROM participants ORDER BY position, id").fetchall()
    holidays = db.execute("SELECT * FROM holidays ORDER BY day").fetchall()
    outbox = db.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 30").fetchall()
    cfg = {k: setting(k) for k in DEFAULT_SETTINGS}
    smtp_ok = bool(SMTP_HOST)
    return render_template("admin.html", people=people, holidays=holidays,
                           outbox=outbox, cfg=cfg, smtp_ok=smtp_ok,
                           lab_name=setting("lab_name"))


@app.route("/admin/participant", methods=["POST"])
@admin_required
def admin_participant():
    db = get_db()
    action = request.form.get("action")
    if action == "add":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()   # optional — lab-wide email is the default channel
        if name:
            pos = db.execute("SELECT COALESCE(MAX(position),0)+1 AS p FROM participants").fetchone()["p"]
            db.execute("INSERT INTO participants (name, email, position) VALUES (?, ?, ?)",
                       (name, email, pos))
            db.commit()
    elif action == "move":
        pid = request.form.get("id", type=int)
        direction = request.form.get("dir")
        rows = db.execute("SELECT id FROM participants ORDER BY position, id").fetchall()
        order = [r["id"] for r in rows]
        if pid in order:
            i = order.index(pid)
            j = i - 1 if direction == "up" else i + 1
            if 0 <= j < len(order):
                order[i], order[j] = order[j], order[i]
                for pos, one in enumerate(order):
                    db.execute("UPDATE participants SET position=? WHERE id=?", (pos, one))
                db.commit()
    elif action == "toggle":
        pid = request.form.get("id", type=int)
        db.execute("UPDATE participants SET active = 1 - active WHERE id=?", (pid,))
        db.commit()
    elif action == "once":
        pid = request.form.get("id", type=int)
        db.execute("UPDATE participants SET once_only = 1 - once_only WHERE id=?", (pid,))
        db.commit()
    elif action == "email":
        pid = request.form.get("id", type=int)
        email = request.form.get("email", "").strip()
        db.execute("UPDATE participants SET email=? WHERE id=?", (email, pid))
        db.commit()
    elif action == "delete":
        pid = request.form.get("id", type=int)
        db.execute("UPDATE sessions SET speaker_id=NULL WHERE speaker_id=? AND day>=?",
                   (pid, date.today().isoformat()))
        db.execute("DELETE FROM participants WHERE id=?", (pid,))
        db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/holiday", methods=["POST"])
@admin_required
def admin_holiday():
    db = get_db()
    if request.form.get("action") == "delete":
        db.execute("DELETE FROM holidays WHERE id=?", (request.form.get("id", type=int),))
    else:
        day = request.form.get("day", "")
        label = request.form.get("label", "").strip()
        if day:
            db.execute("INSERT OR REPLACE INTO holidays (day, label) VALUES (?, ?)", (day, label))
            # cancel any sessions (both tracks) already on that date
            db.execute("UPDATE sessions SET status='cancelled', unavailable_comment=? "
                       "WHERE day=? AND status!='cancelled'",
                       (f"Holiday: {label}" if label else "Holiday", day))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    for key in DEFAULT_SETTINGS:
        if key in request.form:
            set_setting(key, request.form[key].strip())
    flash("Settings saved.")
    return redirect(url_for("admin"))


@app.route("/admin/generate", methods=["POST"])
@admin_required
def admin_generate():
    n = generate_schedule(get_db())
    flash(f"Schedule updated — {n} new slot(s) added.")
    return redirect(url_for("admin"))


@app.route("/admin/rebuild", methods=["POST"])
@admin_required
def admin_rebuild():
    """Re-flow the future rotation, e.g. after adding a holiday or reordering
    people. Keeps: past sessions, sessions with paper/topic details, pending
    swaps, and manually cancelled sessions on non-holiday days. Everything
    else in the future is regenerated fresh."""
    db = get_db()
    today = date.today().isoformat()
    holidays = {r["day"] for r in db.execute("SELECT day FROM holidays")}
    # drop plain future sessions that carry no information yet
    victims = db.execute(
        "SELECT id, day, status FROM sessions WHERE day >= ? AND "
        "COALESCE(paper_title,'')='' AND COALESCE(paper_link,'')='' AND "
        "COALESCE(speaker_note,'')='' AND status IN ('scheduled','cancelled')",
        (today,)).fetchall()
    removed = 0
    for v in victims:
        # keep cancelled rows only if their day really is a holiday
        if v["status"] == "cancelled" and v["day"] in holidays:
            continue
        db.execute("DELETE FROM sessions WHERE id=?", (v["id"],))
        removed += 1
    db.commit()
    n = generate_schedule(db)
    flash(f"Rebuilt the future schedule — {removed} slot(s) re-flowed, {n} generated.")
    return redirect(url_for("admin"))


@app.route("/admin/email", methods=["POST"])
@admin_required
def admin_email():
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    to_raw = request.form.get("to", "").strip()
    if to_raw:
        to = [x.strip() for x in to_raw.replace(";", ",").split(",") if x.strip()]
    else:
        to = lab_recipients(get_db())
    if subject and body and to:
        ok = send_email(to, f"[{setting('lab_name')}] {subject}", body, db=get_db())
        flash("Email sent." if ok else "Email queued in outbox (SMTP not configured or failed).")
    else:
        flash("Recipient, subject and body are all required.")
    return redirect(url_for("admin"))


@app.route("/admin/quick/<int:sid>", methods=["POST"])
@admin_required
def admin_quick(sid):
    """Inline schedule editing from the front page: set a speaker directly,
    or shift a speaker one Friday up/down within the same track."""
    db = get_db()
    sess = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not sess:
        abort(404)
    action = request.form.get("action")
    if action == "set":
        pid = request.form.get("speaker_id", type=int)
        if pid:
            db.execute("UPDATE sessions SET speaker_id=?, status='scheduled' WHERE id=?",
                       (pid, sid))
            db.commit()
    elif action == "shift":
        direction = request.form.get("dir")
        op, order = ("<", "DESC") if direction == "up" else (">", "ASC")
        neighbor = db.execute(
            f"SELECT * FROM sessions WHERE role=? AND status='scheduled' "
            f"AND day {op} ? ORDER BY day {order} LIMIT 1",
            (sess["role"], sess["day"])).fetchone()
        if neighbor:
            db.execute("UPDATE sessions SET speaker_id=? WHERE id=?",
                       (neighbor["speaker_id"], sess["id"]))
            db.execute("UPDATE sessions SET speaker_id=? WHERE id=?",
                       (sess["speaker_id"], neighbor["id"]))
            db.commit()
        else:
            flash("No adjacent Friday to swap with.")
    return redirect(url_for("index"))


@app.route("/admin/beer", methods=["POST"])
@admin_required
def admin_beer():
    """Acknowledge a paid beer (decrement) or clear someone's tab."""
    db = get_db()
    pid = request.form.get("id", type=int)
    if request.form.get("action") == "clear":
        db.execute("UPDATE participants SET beers=0 WHERE id=?", (pid,))
    else:
        db.execute("UPDATE participants SET beers=MAX(beers-1, 0) WHERE id=?", (pid,))
    db.commit()
    return redirect(request.referrer or url_for("admin"))


def _norm_link(link):
    link = (link or "").strip()[:500]
    if link and not re.match(r"^https?://", link):
        link = "https://" + link
    return link


@app.route("/papers")
def papers():
    db = get_db()
    run_automation(db)
    today = date.today().isoformat()
    upcoming = db.execute(
        "SELECT s.*, p.name AS speaker_name FROM sessions s "
        "LEFT JOIN participants p ON p.id = s.speaker_id "
        "WHERE s.role='jc' AND s.day >= ? AND s.status != 'cancelled' "
        "ORDER BY s.day", (today,)).fetchall()
    archive = db.execute(
        "SELECT s.*, p.name AS speaker_name FROM sessions s "
        "LEFT JOIN participants p ON p.id = s.speaker_id "
        "WHERE s.role='jc' AND s.day < ? AND s.status != 'cancelled' "
        "ORDER BY s.day DESC", (today,)).fetchall()
    suggs = {}
    for r in db.execute("SELECT * FROM suggestions ORDER BY id"):
        suggs.setdefault(r["session_id"], []).append(r)
    return render_template("papers.html", upcoming=upcoming, archive=archive,
                           suggs=suggs, lab_name=setting("lab_name"))


@app.route("/papers/paper/<int:sid>", methods=["POST"])
def papers_paper(sid):
    """Anyone can set/correct a session's paper (title + link). Date stays."""
    db = get_db()
    if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
        abort(404)
    title = request.form.get("paper_title", "").strip()[:300]
    link = _norm_link(request.form.get("paper_link"))
    db.execute("UPDATE sessions SET paper_title=?, paper_link=? WHERE id=?",
               (title, link, sid))
    db.commit()
    flash("Paper saved.")
    return redirect(url_for("papers"))


@app.route("/papers/suggest/<int:sid>", methods=["POST"])
def papers_suggest(sid):
    db = get_db()
    if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
        abort(404)
    title = request.form.get("title", "").strip()[:300]
    link = _norm_link(request.form.get("link"))
    by = request.form.get("suggested_by", "").strip()[:80]
    if title or link:
        db.execute("INSERT INTO suggestions (session_id, title, link, suggested_by, created_at) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (sid, title or link, link, by,
                    datetime.now().isoformat(timespec="minutes")))
        db.commit()
        flash("Suggestion added — thanks!")
    return redirect(url_for("papers"))


@app.route("/papers/use/<int:sugg_id>", methods=["POST"])
def papers_use(sugg_id):
    """Adopt a suggestion as the session's paper."""
    db = get_db()
    s = db.execute("SELECT * FROM suggestions WHERE id=?", (sugg_id,)).fetchone()
    if not s:
        abort(404)
    db.execute("UPDATE sessions SET paper_title=?, paper_link=? WHERE id=?",
               (s["title"], s["link"], s["session_id"]))
    db.commit()
    flash("Suggestion adopted as the paper for that session.")
    return redirect(url_for("papers"))


@app.route("/papers/suggestion/<int:sugg_id>/delete", methods=["POST"])
@admin_required
def papers_suggestion_delete(sugg_id):
    db = get_db()
    db.execute("DELETE FROM suggestions WHERE id=?", (sugg_id,))
    db.commit()
    return redirect(url_for("papers"))


@app.route("/admin/room", methods=["POST"])
@admin_required
def admin_room():
    day = request.form.get("day", "")
    room = request.form.get("room", "").strip()[:60]
    if day:
        db = get_db()
        db.execute("INSERT INTO rooms (day, room) VALUES (?, ?) "
                   "ON CONFLICT(day) DO UPDATE SET room = excluded.room", (day, room))
        db.commit()
    return redirect(url_for("index"))


@app.route("/export.pdf")
def export_pdf():
    """Timestamped PDF snapshot of the whole schedule, for the archive."""
    try:
        from fpdf import FPDF
    except ImportError:
        flash("PDF export needs the 'fpdf2' package (pip install fpdf2).")
        return redirect(url_for("index"))
    db = get_db()
    rows = db.execute(
        "SELECT s.*, p.name AS speaker_name FROM sessions s "
        "LEFT JOIN participants p ON p.id = s.speaker_id ORDER BY s.day, s.role").fetchall()
    rooms = {r["day"]: r["room"] for r in db.execute("SELECT day, room FROM rooms")}
    days = {}
    for s in rows:
        days.setdefault(s["day"], {})[s["role"]] = s

    def clean(x):
        return (x or "").encode("latin-1", "replace").decode("latin-1")

    def label(s):
        if not s:
            return "-"
        name = s["speaker_name"] or "-"
        if s["status"] == "cancelled":
            note = s["unavailable_comment"] or "cancelled"
            return f"({clean(note)})" if not s["speaker_name"] else f"{clean(name)} (cancelled)"
        if s["status"] == "needs_swap":
            return f"{clean(name)} (change requested)"
        return clean(name)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 8, clean(f"{setting('lab_name')} - schedule snapshot"), ln=1)
    pdf.set_font("helvetica", "", 9)
    pdf.cell(0, 6, f"Generated {now} - journal club & progress report (F2F = following Monday, "
                   f"same speaker as progress report)", ln=1)
    pdf.ln(2)
    widths = (24, 62, 62, 30, 40, 55)
    headers = ("Friday", "Journal club", "Progress report", "F2F (Mon)", "Room", "JC paper")
    pdf.set_font("helvetica", "B", 9)
    pdf.set_fill_color(235, 235, 230)
    for w, h in zip(widths, headers):
        pdf.cell(w, 7, h, border=1, fill=True)
    pdf.ln()
    pdf.set_font("helvetica", "", 9)
    for day in sorted(days):
        pair = days[day]
        monday = (date.fromisoformat(day) + timedelta(days=3)).isoformat()
        jc = pair.get("jc")
        paper = clean(jc["paper_title"])[:42] if jc and jc["paper_title"] else ""
        cells = (day, label(pair.get("jc")), label(pair.get("progress")),
                 monday, clean(rooms.get(day, "")), paper)
        for w, c in zip(widths, cells):
            pdf.cell(w, 6.5, c, border=1)
        pdf.ln()
    out = bytes(pdf.output())
    from flask import Response
    fname = f"schedule_{datetime.now().strftime('%Y-%m-%d_%H%M')}.pdf"
    return Response(out, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.route("/admin/session/<int:sid>", methods=["POST"])
@admin_required
def admin_session(sid):
    db = get_db()
    action = request.form.get("action")
    if action == "cancel":
        db.execute("UPDATE sessions SET status='cancelled' WHERE id=?", (sid,))
    elif action == "restore":
        db.execute("UPDATE sessions SET status='scheduled', reminder_sent=0, "
                   "swap_requested_at=NULL WHERE id=?", (sid,))
    elif action == "reassign":
        pid = request.form.get("speaker_id", type=int)
        db.execute("UPDATE sessions SET speaker_id=?, status='scheduled' WHERE id=?", (pid, sid))
    db.commit()
    return redirect(url_for("session_view", sid=sid))

# ---------------------------------------------------------------- main ------

init_db()
threading.Thread(target=automation_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
