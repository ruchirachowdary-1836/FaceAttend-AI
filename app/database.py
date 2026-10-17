"""SQLite persistence layer: students, face embeddings, class sessions, attendance, audit events."""
import random
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import numpy as np

from .config import ATTENDANCE_TARGET, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    roll_no        TEXT UNIQUE NOT NULL,
    name           TEXT NOT NULL,
    department     TEXT DEFAULT '',
    email          TEXT DEFAULT '',
    guardian_email TEXT DEFAULT '',
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS embeddings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    vector     BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    day            TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    late_after_min INTEGER
);
CREATE TABLE IF NOT EXISTS attendance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    marked_at   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'present',
    confidence  REAL NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'face',
    camera      TEXT DEFAULT '',
    UNIQUE(student_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_att_day ON attendance(day);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript(SCHEMA)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today():
    return date.today().isoformat()


# ---------- audit events ----------
def log_event(kind, message):
    with db() as con:
        con.execute("INSERT INTO events(ts, kind, message) VALUES (?,?,?)", (now_iso(), kind, message))


def recent_events(limit=40):
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT ts, kind, message FROM events ORDER BY id DESC LIMIT ?", (limit,))]


# ---------- students ----------
def add_student(roll_no, name, department, email, guardian_email, vectors, model):
    with db() as con:
        cur = con.execute(
            "INSERT INTO students(roll_no, name, department, email, guardian_email) VALUES (?,?,?,?,?)",
            (roll_no, name, department, email, guardian_email))
        sid = cur.lastrowid
        _insert_vectors(con, sid, vectors, model)
    log_event("enroll", f"Enrolled {name} ({roll_no})")
    return sid


def _insert_vectors(con, sid, vectors, model):
    con.executemany(
        "INSERT INTO embeddings(student_id, model, vector) VALUES (?,?,?)",
        [(sid, model, np.asarray(v, dtype=np.float32).tobytes()) for v in vectors])


def add_samples(sid, vectors, model):
    with db() as con:
        if not con.execute("SELECT 1 FROM students WHERE id=?", (sid,)).fetchone():
            return False
        _insert_vectors(con, sid, vectors, model)
    return True


def get_student(sid):
    with db() as con:
        r = con.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None


def class_days():
    with db() as con:
        return con.execute("SELECT COUNT(DISTINCT day) FROM attendance").fetchone()[0]


def list_students(day=None):
    day = day or today()
    total_days = class_days()
    with db() as con:
        rows = con.execute(
            """SELECT s.id, s.roll_no, s.name, s.department, s.email, s.guardian_email,
                      (SELECT COUNT(*) FROM embeddings e WHERE e.student_id = s.id) AS samples,
                      (SELECT COUNT(DISTINCT day) FROM attendance a WHERE a.student_id = s.id) AS days_present,
                      (SELECT status FROM attendance a WHERE a.student_id = s.id AND a.day = ?
                         ORDER BY marked_at LIMIT 1) AS today_status,
                      (SELECT MIN(marked_at) FROM attendance a WHERE a.student_id = s.id AND a.day = ?) AS today_time
               FROM students s ORDER BY s.roll_no""", (day, day)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["percent"] = round(100 * d["days_present"] / total_days) if total_days else None
        out.append(d)
    return out


def delete_student(sid):
    with db() as con:
        row = con.execute("SELECT name, roll_no FROM students WHERE id=?", (sid,)).fetchone()
        ok = con.execute("DELETE FROM students WHERE id=?", (sid,)).rowcount > 0
    if ok:
        log_event("delete", f"Removed {row['name']} ({row['roll_no']}) and all face data")
    return ok


def load_gallery(model):
    """Return (students_by_id, owner_id_per_row, embedding_matrix) for one embedding model."""
    with db() as con:
        students = {r["id"]: dict(r) for r in con.execute(
            "SELECT id, roll_no, name, department FROM students")}
        rows = con.execute("SELECT student_id, vector FROM embeddings WHERE model=?", (model,)).fetchall()
    owners = [r["student_id"] for r in rows]
    if rows:
        matrix = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
    else:
        matrix = np.zeros((0, 1), dtype=np.float32)
    return students, owners, matrix


# ---------- sessions ----------
def _session(row):
    return dict(row) if row else None


def active_session(create=True):
    """The open session for today; creates a default 'General' one if today has no session at all."""
    day = today()
    with db() as con:
        row = con.execute("SELECT * FROM sessions WHERE day=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                          (day,)).fetchone()
        if row or not create:
            return _session(row)
        if con.execute("SELECT 1 FROM sessions WHERE day=?", (day,)).fetchone():
            return None                       # sessions exist but all ended: marking is paused
        cur = con.execute("INSERT INTO sessions(name, day, started_at) VALUES (?,?,?)",
                          ("General", day, now_iso()))
        return _session(con.execute("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone())


def start_session(name, late_after_min=None):
    with db() as con:
        con.execute("UPDATE sessions SET ended_at=? WHERE day=? AND ended_at IS NULL", (now_iso(), today()))
        cur = con.execute("INSERT INTO sessions(name, day, started_at, late_after_min) VALUES (?,?,?,?)",
                          (name, today(), now_iso(), late_after_min))
        row = con.execute("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone()
    log_event("session", f"Started session '{name}'")
    return _session(row)


def end_session(sid):
    with db() as con:
        ok = con.execute("UPDATE sessions SET ended_at=? WHERE id=? AND ended_at IS NULL",
                         (now_iso(), sid)).rowcount > 0
    if ok:
        log_event("session", f"Ended session #{sid}")
    return ok


def list_sessions(limit=30):
    with db() as con:
        rows = con.execute(
            """SELECT s.*, (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS present,
                      (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id AND a.status='late') AS late
               FROM sessions s ORDER BY s.id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ---------- attendance ----------
def mark_attendance(student_id, session, confidence=0.0, source="face", camera=""):
    """Insert once per student per session. Returns the stored record, or None if already marked."""
    now = datetime.now()
    status = "present"
    if session.get("late_after_min") is not None:
        started = datetime.fromisoformat(session["started_at"])
        if now > started + timedelta(minutes=session["late_after_min"]):
            status = "late"
    with db() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO attendance(student_id, session_id, day, marked_at, status, confidence,"
            " source, camera) VALUES (?,?,?,?,?,?,?,?)",
            (student_id, session["id"], today(), now.isoformat(timespec="seconds"), status,
             round(float(confidence), 4), source, camera))
        if cur.rowcount != 1:
            return None
    return {"time": now.isoformat(timespec="seconds"), "status": status}


def unmark(student_id, session_id):
    with db() as con:
        return con.execute("DELETE FROM attendance WHERE student_id=? AND session_id=?",
                           (student_id, session_id)).rowcount > 0


def marked_ids(session_id):
    with db() as con:
        return {r[0] for r in con.execute("SELECT student_id FROM attendance WHERE session_id=?",
                                          (session_id,))}


def attendance_for(day, session_id=None, limit=500):
    q = """SELECT a.student_id, s.roll_no, s.name, s.department, a.marked_at AS time, a.status,
                  a.confidence, a.source, a.camera, a.session_id, se.name AS session
           FROM attendance a JOIN students s ON s.id = a.student_id
           JOIN sessions se ON se.id = a.session_id WHERE a.day=?"""
    args = [day]
    if session_id:
        q += " AND a.session_id=?"
        args.append(session_id)
    with db() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY a.marked_at DESC LIMIT ?", (*args, limit))]


def report_for(day, session_id=None):
    q = """SELECT s.roll_no, s.name, s.department, s.guardian_email, a.marked_at, a.status, a.confidence
           FROM students s LEFT JOIN attendance a ON a.student_id = s.id AND a.day=?"""
    args = [day]
    if session_id:
        q += " AND a.session_id=?"
        args.append(session_id)
    with db() as con:
        return [dict(r) for r in con.execute(q + " GROUP BY s.id ORDER BY s.roll_no", args)]


def range_matrix(start, end):
    """Per-student present/late/absent counts between two ISO dates (inclusive)."""
    with db() as con:
        days = [r[0] for r in con.execute(
            "SELECT DISTINCT day FROM attendance WHERE day BETWEEN ? AND ? ORDER BY day", (start, end))]
        rows = con.execute(
            """SELECT s.id, s.roll_no, s.name, s.department, a.day, a.status
               FROM students s LEFT JOIN attendance a ON a.student_id = s.id AND a.day BETWEEN ? AND ?
               ORDER BY s.roll_no""", (start, end)).fetchall()
    per = {}
    for r in rows:
        p = per.setdefault(r["id"], {"roll_no": r["roll_no"], "name": r["name"],
                                     "department": r["department"], "days": {}})
        if r["day"]:
            if p["days"].get(r["day"]) != "present":
                p["days"][r["day"]] = r["status"]
    return days, list(per.values())


def stats(day, session_id=None):
    where, args = "day=?", [day]
    if session_id:
        where += " AND session_id=?"
        args.append(session_id)
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        present = con.execute(f"SELECT COUNT(DISTINCT student_id) FROM attendance WHERE {where}", args).fetchone()[0]
        late = con.execute(f"SELECT COUNT(DISTINCT student_id) FROM attendance WHERE {where} AND status='late'",
                           args).fetchone()[0]
        week = con.execute(
            "SELECT day, COUNT(DISTINCT student_id) AS count FROM attendance "
            "GROUP BY day ORDER BY day DESC LIMIT 7").fetchall()
        hours = con.execute(
            f"SELECT CAST(substr(marked_at, 12, 2) AS INTEGER) AS h, COUNT(*) AS c "
            f"FROM attendance WHERE {where} GROUP BY h", args).fetchall()
        depts = con.execute(
            f"""SELECT s.department AS dept, COUNT(DISTINCT s.id) AS total,
                       COUNT(DISTINCT CASE WHEN a.id IS NOT NULL THEN s.id END) AS present
                FROM students s LEFT JOIN attendance a ON a.student_id = s.id AND a.{where}
                GROUP BY s.department""", args).fetchall()
        sources = con.execute(f"SELECT source, COUNT(*) AS c FROM attendance WHERE {where} GROUP BY source",
                              args).fetchall()
    hourly = [0] * 24
    for r in hours:
        hourly[r["h"]] = r["c"]
    return {
        "day": day, "total": total, "present": present, "late": late,
        "absent": max(total - present, 0),
        "rate": round(100 * present / total) if total else 0,
        "week": [dict(r) for r in reversed(week)],
        "hourly": hourly,
        "departments": [{"dept": r["dept"] or "General", "total": r["total"], "present": r["present"]}
                        for r in depts],
        "sources": {r["source"]: r["c"] for r in sources},
    }


def heatmap(days=84):
    """Attendance % per calendar day for the last N days (drives the activity heatmap)."""
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        rows = {r["day"]: r["c"] for r in con.execute(
            "SELECT day, COUNT(DISTINCT student_id) AS c FROM attendance WHERE day>=? GROUP BY day", (start,))}
    out = []
    for i in range(days):
        d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
        c = rows.get(d, 0)
        out.append({"day": d, "count": c, "percent": round(100 * c / total) if total else 0})
    return out


def insights():
    """At-risk students (below target) and the most punctual students."""
    students = [s for s in list_students() if s["percent"] is not None]
    at_risk = sorted((s for s in students if s["percent"] < ATTENDANCE_TARGET), key=lambda s: s["percent"])
    with db() as con:
        punctual = con.execute(
            """SELECT s.id, s.name, s.roll_no, COUNT(*) AS days,
                      ROUND(AVG(CAST(substr(a.marked_at, 12, 2) AS INTEGER) * 60
                                + CAST(substr(a.marked_at, 15, 2) AS INTEGER))) AS avg_min
               FROM attendance a JOIN students s ON s.id = a.student_id
               WHERE a.source != 'manual' GROUP BY s.id HAVING days >= 3
               ORDER BY avg_min LIMIT 5""").fetchall()
    return {"target": ATTENDANCE_TARGET, "at_risk": at_risk[:10],
            "punctual": [dict(r) for r in punctual]}


# ---------- demo data ----------
DEMO = [("Aarav Sharma", "CSE (AI and ML)"), ("Ananya Reddy", "CSE (AI and ML)"),
        ("Vihaan Gupta", "CSE"), ("Diya Patel", "CSE"), ("Rohan Iyer", "ECE"),
        ("Meera Nair", "ECE"), ("Kabir Singh", "IT"), ("Ishita Rao", "IT"),
        ("Arjun Mehta", "CSE (AI and ML)"), ("Saanvi Joshi", "ECE"), ("Advait Kulkarni", "IT"),
        ("Navya Chowdary", "CSE"), ("Reyansh Das", "CSE (AI and ML)"), ("Tara Menon", "ECE")]


def seed_demo():
    """Insert fictional students with ~5 weeks of history. Returns the number of students created."""
    rnd = random.Random(7)
    created = 0
    with db() as con:
        for i, (name, dept) in enumerate(DEMO, 1):
            roll = f"DEMO{i:03d}"
            if con.execute("SELECT 1 FROM students WHERE roll_no=?", (roll,)).fetchone():
                continue
            slug = name.lower().replace(" ", ".")
            con.execute("INSERT INTO students(roll_no,name,department,email,guardian_email) VALUES (?,?,?,?,?)",
                        (roll, name, dept, f"{slug}@example.edu", f"parent.{slug}@example.com"))
            created += 1
        ids = [(r["id"], i) for i, r in enumerate(
            con.execute("SELECT id FROM students WHERE roll_no LIKE 'DEMO%' ORDER BY roll_no"))]
        habit = {sid: rnd.choice([0.97, 0.92, 0.88, 0.82, 0.7, 0.6]) for sid, _ in ids}
        punct = {sid: rnd.uniform(0, 1) for sid, _ in ids}
        for back in range(35, 0, -1):
            d = date.today() - timedelta(days=back)
            if d.weekday() >= 5 or con.execute("SELECT 1 FROM sessions WHERE day=?", (d.isoformat(),)).fetchone():
                continue
            start = datetime.combine(d, datetime.min.time()).replace(hour=9)
            cur = con.execute("INSERT INTO sessions(name,day,started_at,ended_at,late_after_min) VALUES (?,?,?,?,15)",
                              ("Morning Lecture", d.isoformat(), start.isoformat(timespec="seconds"),
                               (start + timedelta(hours=1)).isoformat(timespec="seconds")))
            for sid, _ in ids:
                if rnd.random() > habit[sid]:
                    continue
                minute = int(rnd.gauss(8 + 22 * punct[sid], 7))
                at = start + timedelta(minutes=max(-12, min(minute, 55)))
                status = "late" if at > start + timedelta(minutes=15) else "present"
                con.execute("INSERT OR IGNORE INTO attendance(student_id,session_id,day,marked_at,status,"
                            "confidence,source) VALUES (?,?,?,?,?,?,'seed')",
                            (sid, cur.lastrowid, d.isoformat(), at.isoformat(timespec="seconds"), status,
                             round(rnd.uniform(0.6, 0.9), 3)))
    return created


def clear_demo():
    with db() as con:
        return con.execute("DELETE FROM students WHERE roll_no LIKE 'DEMO%'").rowcount
