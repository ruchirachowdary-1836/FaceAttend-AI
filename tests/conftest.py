"""Shared fixtures: a fake face backend so tests need no camera and no model download."""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["DATA_DIR"] = _tmp
os.environ["MODELS_DIR"] = _tmp

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import database as dbm  # noqa: E402
from app import main  # noqa: E402
from app.config import settings  # noqa: E402
from app.recognizer import Face, Recognizer  # noqa: E402


def identity_vec(i, dim=16):
    v = np.random.default_rng(i).normal(size=dim).astype(np.float32)
    return v / np.linalg.norm(v)


class FakeBackend:
    """Identity comes from the top-left pixel; the yaw offset comes from the second pixel."""
    name = "fake"
    default_threshold = 0.6

    def detect(self, bgr):
        ident = int(bgr[0, 0, 0])
        if ident == 0:
            return []
        h, w = bgr.shape[:2]
        yaw = (int(bgr[0, 1, 0]) - 128) / 128 * 10          # eye distance is 0.3*w = 60 px
        kps = np.array([[w * .35, h * .4], [w * .65, h * .4], [w * .5 + yaw, h * .55],
                        [w * .4, h * .7], [w * .6, h * .7]], dtype=np.float32)
        return [Face(np.array([w * .2, h * .1, w * .8, h * .9], dtype=np.float32), kps,
                     identity_vec(ident), 0.99)]


def face_jpeg(ident, yaw_px=128, size=200):
    img = np.full((size, size, 3), 90, dtype=np.uint8)
    img[0, 0] = (ident, ident, ident)
    img[0, 1] = (yaw_px, yaw_px, yaw_px)
    _, buf = cv2.imencode(".png", img)             # png keeps the marker pixels lossless
    return buf.tobytes()


@pytest.fixture()
def client():
    if os.path.exists(dbm.DB_PATH):
        os.remove(dbm.DB_PATH)
    settings.liveness = False
    settings.confirm_frames = 2
    settings.min_face_px = 20
    settings.match_threshold = None
    dbm.init_db()
    main.app.state.rec = Recognizer(FakeBackend())
    main.reset_cache()
    with TestClient(main.app) as c:
        yield c
