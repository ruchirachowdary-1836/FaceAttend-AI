import time
from datetime import date, timedelta

from app import database as dbm
from app.config import settings

from .conftest import face_jpeg


def enroll(client, roll, name, ident, dept="CSE"):
    files = [("images", (f"{i}.png", face_jpeg(ident), "image/png")) for i in range(3)]
    return client.post("/api/students", data={"roll_no": roll, "name": name, "department": dept,
                                              "guardian_email": f"{roll}@parent.test"}, files=files)


def stream(ws, ident, n, yaw=128):
    last = None
    for _ in range(n):
        ws.send_bytes(face_jpeg(ident, yaw))
        last = ws.receive_json()
    return last


def open_ws(client, camera=""):
    ws = client.websocket_connect(f"/ws/live?camera={camera}")
    sock = ws.__enter__()
    assert sock.receive_json()["type"] == "hello"
    assert sock.receive_json()["type"] == "presence"
    return ws, sock


def test_enroll_and_list(client):
    assert enroll(client, "R1", "Asha", 11).status_code == 200
    rows = client.get("/api/students").json()
    assert rows[0]["name"] == "Asha" and rows[0]["samples"] == 3


def test_duplicate_roll_and_duplicate_face(client):
    enroll(client, "R1", "Asha", 11)
    assert enroll(client, "R1", "Other", 22).status_code == 409           # same roll
    r = enroll(client, "R2", "Asha Twin", 11)                              # same face, new roll
    assert r.status_code == 409 and "already matches" in r.json()["detail"]


def test_no_face_rejected(client):
    assert enroll(client, "R9", "Nobody", 0).status_code == 422


def test_live_recognition_marks_once(client):
    enroll(client, "R1", "Asha", 11)
    ws, sock = open_ws(client, "Door")
    assert stream(sock, 11, 1)["faces"][0]["status"] == "verifying"
    assert stream(sock, 11, 1)["faces"][0]["status"] == "marked"
    ev = sock.receive_json()
    assert ev["type"] == "marked" and ev["name"] == "Asha" and ev["camera"] == "Door"
    assert stream(sock, 11, 1)["faces"][0]["status"] == "present"
    ws.__exit__(None, None, None)
    att = client.get("/api/attendance").json()
    assert len(att) == 1 and att[0]["source"] == "face"


def test_unknown_face_event(client):
    enroll(client, "R1", "Asha", 11)
    ws, sock = open_ws(client)
    assert stream(sock, 99, 1)["faces"][0]["status"] == "unknown"
    assert sock.receive_json()["type"] == "unknown"
    ws.__exit__(None, None, None)


def test_liveness_blocks_static_face(client):
    enroll(client, "R1", "Asha", 11)
    settings.liveness = True
    ws, sock = open_ws(client)
    assert stream(sock, 11, 4, yaw=128)["faces"][0]["status"] == "liveness"     # perfectly still photo
    assert stream(sock, 11, 1, yaw=60)["faces"][0]["status"] == "marked"     # head turned
    assert sock.receive_json()["type"] == "marked"
    ws.__exit__(None, None, None)
    assert len(client.get("/api/attendance").json()) == 1


def test_manual_mark_unmark_and_late(client):
    sid = enroll(client, "R1", "Asha", 11).json()["id"]
    client.post("/api/sessions", json={"name": "Maths", "late_after_min": 0})
    time.sleep(1.1)
    assert client.post("/api/attendance/manual", json={"student_id": sid}).json()["status"] == "late"
    assert client.post("/api/attendance/manual", json={"student_id": sid}).json()["already"] is True
    client.post("/api/attendance/manual", json={"student_id": sid, "present": False})
    assert client.get("/api/attendance").json() == []


def test_ended_session_pauses_marking(client):
    sid = enroll(client, "R1", "Asha", 11).json()["id"]
    s = client.post("/api/sessions", json={"name": "Physics"}).json()
    client.post(f"/api/sessions/{s['id']}/end")
    assert client.post("/api/attendance/manual", json={"student_id": sid}).status_code == 409
    ws, sock = open_ws(client)
    assert stream(sock, 11, 2)["faces"][0]["status"] == "paused"
    ws.__exit__(None, None, None)


def test_stats_heatmap_insights_and_demo(client):
    assert client.post("/api/demo/seed").json()["created"] == 14
    stats = client.get("/api/stats").json()
    assert stats["total"] == 14 and len(stats["week"]) == 7
    assert len(client.get("/api/heatmap").json()) == 84
    ins = client.get("/api/insights").json()
    assert "at_risk" in ins and "punctual" in ins
    assert client.delete("/api/demo").json()["removed"] == 14


def test_exports_are_csv_and_injection_safe(client):
    enroll(client, "R1", "=cmd|calc", 11)
    text = client.get("/api/export").text
    assert "'=cmd|calc" in text and "Absent" in text
    start = (date.today() - timedelta(days=7)).isoformat()
    assert client.get(f"/api/export/range?start={start}&end={dbm.today()}").status_code == 200
    assert client.get("/api/export/range?start=bad&end=bad").status_code == 422


def test_absentee_alert_dry_run(client):
    enroll(client, "R1", "Asha", 11)
    out = client.post("/api/notify/absentees").json()
    assert out["absent"] == 1 and out["results"][0]["status"] == "dry-run"


def test_config_patch_and_validation(client):
    r = client.patch("/api/config", json={"confirm_frames": 5, "liveness": True}).json()
    assert r["confirm_frames"] == 5 and r["liveness"] is True
    assert client.patch("/api/config", json={"match_threshold": 5}).status_code == 422


def test_delete_student_removes_everything(client):
    sid = enroll(client, "R1", "Asha", 11).json()["id"]
    client.post("/api/attendance/manual", json={"student_id": sid})
    assert client.delete(f"/api/students/{sid}").status_code == 200
    assert client.get("/api/students").json() == [] and client.get("/api/attendance").json() == []
