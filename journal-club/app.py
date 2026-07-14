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
DB_PATH = os.environ.get("JC_DB", os.path.join(BASE_DIR, "journal_club.db"))


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
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER or "journal-club@localhost")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

# ------------------------------------------------------------------ db ------

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS holidays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL UNIQUE,          -- ISO date
    label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL UNIQUE,          -- ISO date (a Friday)
    speaker_id INTEGER,
    original_speaker_id INTEGER,
    status TEXT NOT NULL DEFAULT 'scheduled',
        -- scheduled | needs_swap | cancelled
    unavailable_comment TEXT DEFAULT '',
    swap_requested_at TEXT,            -- ISO datetime
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    paper_title TEXT DEFAULT '',
    paper_link TEXT DEFAULT '',
    speaker_note TEXT DEFAULT ''
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


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    db.commit()
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


def _smtp_send(recipient, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = recipient
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(MAIL_FROM, [recipient], msg.as_string())


def send_email(recipient, subject, body, db=None):
    """Send an email; always log it to the outbox. Never crash the app."""
    if not recipient:
        return False
    own_conn = db is None
    if own_conn:
        db = sqlite3.connect(DB_PATH)
    sent, error = 0, ""
    if SMTP_HOST:
        try:
            _smtp_send(recipient, subject, body)
            sent = 1
        except Exception as e:  # log, don't crash
            error = str(e)
    else:
        error = "SMTP not configured (set SMTP_HOST in .env)"
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
    """Append sessions for Fridays up to the horizon, round-robin speakers,
    skipping holidays. Existing sessions are never touched."""
    people = db.execute(
        "SELECT * FROM participants WHERE active = 1 ORDER BY position, id").fetchall()
    if not people:
        return 0
    holidays = {r["day"] for r in db.execute("SELECT day FROM holidays")}
    existing = {r["day"] for r in db.execute("SELECT day FROM sessions")}

    horizon = int(setting("horizon_weeks") or 16)
    # continue rotation from the last future assignment
    last = db.execute(
        "SELECT speaker_id FROM sessions WHERE speaker_id IS NOT NULL "
        "ORDER BY day DESC LIMIT 1").fetchone()
    ids = [p["id"] for p in people]
    idx = 0
    if last and last["speaker_id"] in ids:
        idx = (ids.index(last["speaker_id"]) + 1) % len(ids)

    day = next_friday(date.today())
    created = 0
    for _ in range(horizon):
        iso = day.isoformat()
        if iso not in existing:
            if iso in holidays:
                db.execute("INSERT INTO sessions (day, status) VALUES (?, 'cancelled')", (iso,))
                db.execute("UPDATE sessions SET unavailable_comment = ? WHERE day = ?",
                           ("Holiday", iso))
            else:
                db.execute("INSERT INTO sessions (day, speaker_id) VALUES (?, ?)",
                           (iso, ids[idx]))
                idx = (idx + 1) % len(ids)
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
    lab_email = setting("lab_email")
    db.execute("UPDATE sessions SET status='needs_swap', unavailable_comment=?, "
               "swap_requested_at=?, reminder_sent=0, original_speaker_id=speaker_id "
               "WHERE id=?",
               (comment, datetime.now().isoformat(timespec="seconds"), sess["id"]))
    db.commit()
    link = f"{BASE_URL}/session/{sess['id']}"
    send_email(
        lab_email,
        f"[{setting('lab_name')}] Speaker needed for {sess['day']}",
        f"{sess['speaker_name']} is not available to present on Friday {sess['day']}.\n"
        f"Reason: {comment or '—'}\n\n"
        f"Can anyone take over this slot? Volunteer here:\n{link}\n\n"
        f"If nobody volunteers, a reminder will be sent, and the session may be "
        f"cancelled automatically.",
        db=db)


def take_over(db, sess, volunteer_id):
    """Volunteer takes the slot. If the volunteer has a future scheduled slot,
    the two speakers are swapped so the original speaker presents later."""
    volunteer = db.execute("SELECT * FROM participants WHERE id=?", (volunteer_id,)).fetchone()
    if not volunteer:
        return False
    original_id = sess["speaker_id"]
    future = db.execute(
        "SELECT * FROM sessions WHERE speaker_id=? AND status='scheduled' AND day>? "
        "ORDER BY day LIMIT 1", (volunteer_id, sess["day"])).fetchone()
    db.execute("UPDATE sessions SET speaker_id=?, status='scheduled' WHERE id=?",
               (volunteer_id, sess["id"]))
    swapped = ""
    if future and original_id:
        db.execute("UPDATE sessions SET speaker_id=? WHERE id=?", (original_id, future["id"]))
        swapped = (f"\nIn exchange, {sess['speaker_name']} takes over "
                   f"{volunteer['name']}'s slot on {future['day']}.")
    db.commit()
    send_email(
        setting("lab_email"),
        f"[{setting('lab_name')}] {sess['day']}: {volunteer['name']} will present",
        f"{volunteer['name']} has volunteered to take over the journal club on "
        f"Friday {sess['day']} from {sess['speaker_name']}.{swapped}\n\n"
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
    lab_email = _setting("lab_email")
    lab_name = _setting("lab_name")

    rows = db.execute(
        "SELECT s.*, p.name AS speaker_name FROM sessions s "
        "LEFT JOIN participants p ON p.id = s.original_speaker_id "
        "WHERE s.status = 'needs_swap' AND s.day >= ?", (today.isoformat(),)).fetchall()

    for s in rows:
        day = date.fromisoformat(s["day"])
        requested = datetime.fromisoformat(s["swap_requested_at"]) if s["swap_requested_at"] else None
        link = f"{BASE_URL}/session/{s['id']}"

        # final step: cancel shortly before the date
        if (day - today).days <= cancel_before:
            db.execute("UPDATE sessions SET status='cancelled' WHERE id=?", (s["id"],))
            db.commit()
            send_email(
                lab_email,
                f"[{lab_name}] Journal club on {s['day']} is cancelled",
                f"No one was able to take over the session on Friday {s['day']} "
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
                f"[{lab_name}] Reminder: speaker still needed for {s['day']}",
                f"Still looking for someone to take over the journal club on "
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
    upcoming = db.execute(
        "SELECT s.*, p.name AS speaker_name FROM sessions s "
        "LEFT JOIN participants p ON p.id = s.speaker_id "
        "WHERE s.day >= ? ORDER BY s.day", (today,)).fetchall()
    past = db.execute(
        "SELECT s.*, p.name AS speaker_name FROM sessions s "
        "LEFT JOIN participants p ON p.id = s.speaker_id "
        "WHERE s.day < ? ORDER BY s.day DESC LIMIT 25", (today,)).fetchall()
    people = db.execute("SELECT * FROM participants WHERE active=1 ORDER BY position, id").fetchall()
    return render_template("index.html", upcoming=upcoming, past=past,
                           people=people, lab_name=setting("lab_name"))


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
        email = request.form.get("email", "").strip()
        if name and email:
            pos = db.execute("SELECT COALESCE(MAX(position),0)+1 AS p FROM participants").fetchone()["p"]
            db.execute("INSERT INTO participants (name, email, position) VALUES (?, ?, ?)",
                       (name, email, pos))
            db.commit()
    elif action == "toggle":
        pid = request.form.get("id", type=int)
        db.execute("UPDATE participants SET active = 1 - active WHERE id=?", (pid,))
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
            # cancel a session already scheduled on that date
            row = db.execute("SELECT id FROM sessions WHERE day=? AND status!='cancelled'",
                             (day,)).fetchone()
            if row:
                db.execute("UPDATE sessions SET status='cancelled', unavailable_comment=? "
                           "WHERE id=?", (f"Holiday: {label}" if label else "Holiday", row["id"]))
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
    flash(f"Schedule updated — {n} new Friday(s) added.")
    return redirect(url_for("admin"))


@app.route("/admin/email", methods=["POST"])
@admin_required
def admin_email():
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    to = request.form.get("to", "").strip() or setting("lab_email")
    if subject and body and to:
        ok = send_email(to, f"[{setting('lab_name')}] {subject}", body, db=get_db())
        flash("Email sent." if ok else "Email queued in outbox (SMTP not configured or failed).")
    else:
        flash("Recipient, subject and body are all required.")
    return redirect(url_for("admin"))


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
