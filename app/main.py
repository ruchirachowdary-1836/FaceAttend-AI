"""FaceAttend server: REST API + WebSocket recognition stream + animated dashboard."""
import asyncio
import csv
import io
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import cv2
import numpy as np
from fastapi import (Depends, FastAPI, File, Form, HTTPException, Query, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from . import database as dbm
from .config import ATTENDANCE_TARGET, settings
from .liveness import is_live, yaw_proxy
from .notify import send_absentee_mails
from .recognizer import Recognizer

STATIC_DIR = Path(__file__).parent / "static"
VERSION = "2.0.0"
marked_cache = {"session": None, "ids": set()}
STREAK_GRACE = 2          # frames a student may drop out of view without resetting the streak


@asynccontextmanager
async def lifespan(app: FastAPI):
    dbm.init_db()
    if not hasattr(app.state, "rec"):             # tests inject a fake recognizer beforehand
        app.state.rec = Recognizer()              # loads / downloads the model once at start-up
    app.state.started = time.time()
    yield


app = FastAPI(title="FaceAttend AI", version=VERSION, lifespan=lifespan)


class Hub:
    """Tracks WebSocket clients (one per camera/screen) so a mark made anywhere appears everywhere."""

    def __init__(self):
        self.clients = {}

    async def connect(self, ws, camera):
        await ws.accept()
        self.clients[ws] = {"lock": asyncio.Lock(), "camera": camera}

    def disconnect(self, ws):
        self.clients.pop(ws, None)

    async def send(self, ws, payload):
        info = self.clients.get(ws)
        if info:
            async with info["lock"]:
                await ws.send_json(payload)

    async def broadcast(self, payload):
        for ws in list(self.clients):
            try:
                await self.send(ws, payload)
            except Exception:
                self.disconnect(ws)

    def presence(self):
        return {"type": "presence", "screens": len(self.clients),
                "cameras": sorted({c["camera"] for c in self.clients.values() if c["camera"]})}


hub = Hub()


def marked_set(session):
    sid = session["id"] if session else None
    if marked_cache["session"] != sid:
        marked_cache.update(session=sid, ids=dbm.marked_ids(sid) if sid else set())
    return marked_cache["ids"]


def reset_cache():
    marked_cache["session"] = None


def process_frame(rec, frame, streaks, camera, memo):
    """Detect -> embed -> match -> liveness -> (after N consecutive frames) mark attendance."""
    h, w = frame.shape[:2]
    session = dbm.active_session()
    ids = marked_set(session)
    confirm = max(1, settings.confirm_frames)
    faces_out, events, seen = [], [], set()

    for face in rec.detect(frame):
        x1, y1, x2, y2 = (int(v) for v in face.bbox)
        item = {"box": [x1, y1, x2, y2], "name": "Unknown", "status": "unknown", "score": 0,
                "progress": 0}
        if min(x2 - x1, y2 - y1) < settings.min_face_px:
            item.update(status="small")
            faces_out.append(item)
            continue
        student, score = rec.match(face.embedding)
        item["score"] = round(score, 3)
        if student:
            sid = student["id"]
            seen.add(sid)
            st = streaks.setdefault(sid, {"n": 0, "miss": 0, "yaws": []})
            st["n"] += 1
            st["miss"] = 0
            st["yaws"] = (st["yaws"] + [yaw_proxy(face.kps)])[-12:]
            item.update(name=student["name"], roll_no=student["roll_no"], student_id=sid,
                        department=student["department"], status="verifying",
                        progress=round(min(1, st["n"] / confirm), 2))
            if st["n"] >= confirm:
                live = (not settings.liveness) or is_live(st["yaws"], settings.liveness_min_yaw)
                if not live:
                    item.update(status="liveness")
                elif session is None:
                    item.update(status="paused")
                elif sid in ids:
                    item.update(status="present")
                else:
                    stamp = dbm.mark_attendance(sid, session, score, "face", camera)
                    ids.add(sid)
                    item.update(status="marked")
                    if stamp:
                        events.append({
                            "type": "marked", "student_id": sid, "name": student["name"],
                            "roll_no": student["roll_no"], "department": student["department"],
                            "time": stamp["time"], "status": stamp["status"], "confidence": round(score, 3),
                            "source": "face", "camera": camera, "session": session["name"]})
                        dbm.log_event("mark", f"{student['name']} marked {stamp['status']} via {camera or 'camera'}")
        else:
            now = time.time()
            if now - memo.get("unknown", 0) > 20:
                memo["unknown"] = now
                events.append({"type": "unknown", "time": dbm.now_iso(), "camera": camera, "score": round(score, 3)})
                dbm.log_event("unknown", f"Unrecognised face at {camera or 'camera'}")
        faces_out.append(item)

    for sid in [s for s in streaks if s not in seen]:
        streaks[sid]["miss"] += 1
        if streaks[sid]["miss"] > STREAK_GRACE:
            del streaks[sid]
    return {"w": w, "h": h, "faces": faces_out}, events


def public_config():
    rec = app.state.rec
    return {
        "version": VERSION, "backend": rec.backend.name, "threshold": round(rec.threshold, 3),
        "confirm_frames": settings.confirm_frames, "liveness": settings.liveness,
        "liveness_min_yaw": settings.liveness_min_yaw, "min_face_px": settings.min_face_px,
        "auth_required": auth.required(), "target": ATTENDANCE_TARGET,
        "session": dbm.active_session(),
    }


# ---------------- WebSocket: live recognition ----------------
@app.websocket("/ws/live")
async def live(ws: WebSocket, camera: str = Query("", max_length=40)):
    await hub.connect(ws, camera)
    streaks, memo = {}, {}
    try:
        await hub.send(ws, {"type": "hello", **public_config()})
        await hub.broadcast(hub.presence())
        while True:
            data = await ws.receive_bytes()       # one JPEG frame per message
            frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                await hub.send(ws, {"type": "frame", "w": 1, "h": 1, "faces": []})
                continue
            result, events = await run_in_threadpool(
                process_frame, ws.app.state.rec, frame, streaks, camera, memo)
            await hub.send(ws, {"type": "frame", **result})
            for ev in events:
                await hub.broadcast(ev)
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(ws)
        await hub.broadcast(hub.presence())


# ---------------- auth + config ----------------
class Login(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: Login):
    if not auth.required():
        return {"token": "", "auth_required": False}
    await asyncio.sleep(0.4)                       # slows down password guessing
    if not auth.check_password(body.password):
        raise HTTPException(401, "Wrong password")
    return {"token": auth.make_token(), "auth_required": True}


@app.get("/api/health")
def health():
    return {"status": "ok", "backend": app.state.rec.backend.name, "uptime": int(time.time() - app.state.started)}


@app.get("/api/config")
def get_config():
    return public_config()


class ConfigPatch(BaseModel):
    match_threshold: float | None = Field(None, ge=0.1, le=0.95)
    confirm_frames: int | None = Field(None, ge=1, le=15)
    liveness: bool | None = None


@app.patch("/api/config", dependencies=[Depends(auth.admin)])
async def patch_config(body: ConfigPatch):
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(settings, key, value)
    dbm.log_event("config", f"Settings changed: {body.model_dump(exclude_none=True)}")
    cfg = {"type": "config", **public_config()}
    await hub.broadcast(cfg)
    return cfg


# ---------------- students ----------------
def decode(raw):
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)


async def embed_uploads(rec, images):
    """Embed every uploaded photo, dropping blurry/tiny faces and outliers (shots of someone else)."""
    vectors = []
    for up in images[:12]:
        frame = decode(await up.read())
        if frame is None:
            continue
        face = await run_in_threadpool(rec.embed_largest, frame)
        if face is not None and min(face.bbox[2] - face.bbox[0], face.bbox[3] - face.bbox[1]) >= settings.min_face_px:
            vectors.append(face.embedding)
    if len(vectors) >= 3:                           # drop samples that do not resemble the rest
        mean = np.mean(vectors, axis=0)
        mean /= np.linalg.norm(mean) or 1
        vectors = [v for v in vectors if float(v @ mean) >= 0.3] or vectors
    return vectors


@app.post("/api/students", dependencies=[Depends(auth.admin)])
async def enroll(roll_no: str = Form(..., max_length=40), name: str = Form(..., max_length=80),
                 department: str = Form("", max_length=60), email: str = Form("", max_length=120),
                 guardian_email: str = Form("", max_length=120), images: list[UploadFile] = File(...)):
    rec = app.state.rec
    vectors = await embed_uploads(rec, images)
    if not vectors:
        raise HTTPException(422, "No clear face detected. Improve lighting, move closer and face the camera.")
    twin, score = rec.match(vectors[0])
    if twin:
        raise HTTPException(409, f"This face already matches {twin['name']} ({twin['roll_no']}) - {score:.0%} similar.")
    try:
        sid = dbm.add_student(roll_no.strip(), name.strip(), department.strip(), email.strip(),
                              guardian_email.strip(), vectors, rec.backend.name)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "This roll number is already enrolled.")
    rec.reload()
    await hub.broadcast({"type": "roster", "reason": "enrolled", "name": name.strip()})
    return {"id": sid, "faces_used": len(vectors)}


@app.post("/api/students/{sid}/samples", dependencies=[Depends(auth.admin)])
async def add_samples(sid: int, images: list[UploadFile] = File(...)):
    rec = app.state.rec
    vectors = await embed_uploads(rec, images)
    if not vectors:
        raise HTTPException(422, "No clear face detected in the new photos.")
    if not dbm.add_samples(sid, vectors, rec.backend.name):
        raise HTTPException(404, "Student not found")
    rec.reload()
    return {"id": sid, "faces_used": len(vectors)}


@app.get("/api/students")
def students(day: str | None = None):
    return dbm.list_students(day)


@app.delete("/api/students/{sid}", dependencies=[Depends(auth.admin)])
async def remove_student(sid: int):
    if not dbm.delete_student(sid):
        raise HTTPException(404, "Student not found")
    app.state.rec.reload()
    reset_cache()
    await hub.broadcast({"type": "roster", "reason": "deleted"})
    return {"deleted": sid}


# ---------------- attendance ----------------
class Manual(BaseModel):
    student_id: int
    present: bool = True
    source: str = Field("manual", pattern="^(manual|demo)$")


@app.post("/api/attendance/manual", dependencies=[Depends(auth.admin)])
async def manual_mark(body: Manual):
    student = dbm.get_student(body.student_id)
    if not student:
        raise HTTPException(404, "Student not found")
    session = dbm.active_session()
    if session is None:
        raise HTTPException(409, "No active session. Start one first.")
    if not body.present:
        dbm.unmark(student["id"], session["id"])
        reset_cache()
        dbm.log_event("manual", f"{student['name']} marked absent manually")
        await hub.broadcast({"type": "unmarked", "student_id": student["id"], "name": student["name"]})
        return {"ok": True}
    stamp = dbm.mark_attendance(student["id"], session, 1.0, body.source)
    reset_cache()
    if not stamp:
        return {"ok": True, "already": True}
    dbm.log_event("manual", f"{student['name']} marked {stamp['status']} manually")
    await hub.broadcast({
        "type": "marked", "student_id": student["id"], "name": student["name"], "roll_no": student["roll_no"],
        "department": student["department"], "time": stamp["time"], "status": stamp["status"],
        "confidence": 1.0, "source": body.source, "camera": "", "session": session["name"]})
    return {"ok": True, **stamp}


@app.get("/api/attendance")
def attendance(day: str | None = None, session_id: int | None = None):
    return dbm.attendance_for(day or dbm.today(), session_id)


@app.get("/api/stats")
def stats(day: str | None = None, session_id: int | None = None):
    return dbm.stats(day or dbm.today(), session_id)


@app.get("/api/heatmap")
def heatmap(days: int = Query(84, ge=7, le=365)):
    return dbm.heatmap(days)


@app.get("/api/insights")
def insights():
    return dbm.insights()


@app.get("/api/events")
def events(limit: int = Query(40, ge=1, le=200)):
    return dbm.recent_events(limit)


# ---------------- sessions ----------------
class NewSession(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    late_after_min: int | None = Field(None, ge=0, le=240)


@app.get("/api/sessions")
def sessions():
    return dbm.list_sessions()


@app.post("/api/sessions", dependencies=[Depends(auth.admin)])
async def new_session(body: NewSession):
    session = dbm.start_session(body.name.strip(), body.late_after_min)
    reset_cache()
    await hub.broadcast({"type": "session", "session": session})
    return session


@app.post("/api/sessions/{sid}/end", dependencies=[Depends(auth.admin)])
async def stop_session(sid: int):
    if not dbm.end_session(sid):
        raise HTTPException(404, "Session not found or already ended")
    reset_cache()
    await hub.broadcast({"type": "session", "session": dbm.active_session(create=False)})
    return {"ended": sid}


# ---------------- exports + alerts ----------------
def safe(cell):
    """Neutralise spreadsheet formula injection from user-controlled text."""
    s = "" if cell is None else str(cell)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def csv_response(rows, filename):
    buf = io.StringIO()
    csv.writer(buf).writerows([[safe(c) for c in row] for row in rows])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/export")
def export(day: str | None = None, session_id: int | None = None):
    day = day or dbm.today()
    rows = [["Roll No", "Name", "Department", "Status", "Time", "Confidence"]]
    for r in dbm.report_for(day, session_id):
        rows.append([r["roll_no"], r["name"], r["department"],
                     (r["status"] or "absent").capitalize() if r["marked_at"] else "Absent",
                     r["marked_at"] or "", r["confidence"] if r["marked_at"] else ""])
    return csv_response(rows, f"attendance_{day}.csv")


@app.get("/api/export/range")
def export_range(start: str, end: str):
    try:
        date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(422, "start and end must be YYYY-MM-DD")
    days, people = dbm.range_matrix(start, end)
    rows = [["Roll No", "Name", "Department", *days, "Present", "Late", "Absent", "Percent"]]
    for p in people:
        marks = [{"present": "P", "late": "L"}.get(p["days"].get(d), "A") for d in days]
        present, late = marks.count("P"), marks.count("L")
        pct = round(100 * (present + late) / len(days)) if days else 0
        rows.append([p["roll_no"], p["name"], p["department"], *marks, present, late, marks.count("A"), f"{pct}%"])
    return csv_response(rows, f"attendance_{start}_to_{end}.csv")


@app.post("/api/notify/absentees", dependencies=[Depends(auth.admin)])
async def notify(day: str | None = None):
    day = day or dbm.today()
    absent = [r for r in dbm.report_for(day) if not r["marked_at"]]
    results = await run_in_threadpool(send_absentee_mails, day, absent)
    dbm.log_event("notify", f"Absentee alerts for {day}: {len(results)} recipient(s)")
    return {"day": day, "absent": len(absent), "results": results}


# ---------------- demo data ----------------
@app.post("/api/demo/seed", dependencies=[Depends(auth.admin)])
async def demo_seed():
    created = await run_in_threadpool(dbm.seed_demo)
    reset_cache()
    await hub.broadcast({"type": "roster", "reason": "demo"})
    return {"created": created}


@app.delete("/api/demo", dependencies=[Depends(auth.admin)])
async def demo_clear():
    removed = dbm.clear_demo()
    app.state.rec.reload()
    reset_cache()
    await hub.broadcast({"type": "roster", "reason": "demo"})
    return {"removed": removed}


# Static dashboard (must be mounted last)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
