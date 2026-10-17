import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def secured(monkeypatch):
    from app import auth, main
    from .conftest import FakeBackend
    from app.recognizer import Recognizer
    monkeypatch.setattr(auth, "ADMIN_PASSWORD", "s3cret")
    from app import database as dbm
    dbm.init_db()
    main.app.state.rec = Recognizer(FakeBackend())
    with TestClient(main.app) as c:
        yield c


def test_mutations_need_token(secured):
    assert secured.post("/api/sessions", json={"name": "X"}).status_code == 401
    assert secured.get("/api/students").status_code == 200            # reads stay open for the kiosk
    assert secured.post("/api/login", json={"password": "nope"}).status_code == 401
    token = secured.post("/api/login", json={"password": "s3cret"}).json()["token"]
    ok = secured.post("/api/sessions", json={"name": "X"}, headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


def test_tampered_token_rejected(secured):
    token = secured.post("/api/login", json={"password": "s3cret"}).json()["token"]
    assert secured.post("/api/sessions", json={"name": "X"},
                        headers={"Authorization": f"Bearer {token[:-4]}AAAA"}).status_code == 401
