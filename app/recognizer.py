"""Face detection + embeddings + cosine matching with two interchangeable backends.

* ``insightface`` - SCRFD detector + ArcFace (512-d). Most accurate, needs a C++ toolchain on Windows.
* ``opencv``      - YuNet detector + SFace (128-d). Pure ``opencv-python``, models auto-download (~39 MB).

Embeddings are tagged with the backend that produced them, so switching backend never mixes vectors.
"""
import threading
import urllib.request
from dataclasses import dataclass

import numpy as np

from . import database as dbm
from .config import BACKEND, DET_SIZE, MODEL_NAME, MODELS_DIR, settings

ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
OPENCV_MODELS = {
    "yunet.onnx": f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "sface.onnx": f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}


@dataclass
class Face:
    bbox: np.ndarray        # x1, y1, x2, y2
    kps: np.ndarray         # 5 x 2 landmarks (eyes, nose, mouth corners)
    embedding: np.ndarray   # L2-normalised
    score: float            # detector confidence


def _normalise(v):
    v = np.asarray(v, dtype=np.float32).ravel()
    n = np.linalg.norm(v)
    return v / n if n else v


def _download(name):
    path = MODELS_DIR / name
    if path.exists() and path.stat().st_size > 1000:
        return str(path)
    print(f"[faceattend] downloading {name} ...")
    tmp = path.with_suffix(".part")
    urllib.request.urlretrieve(OPENCV_MODELS[name], tmp)
    tmp.replace(path)
    return str(path)


class OpenCVBackend:
    name = "opencv-sface"
    default_threshold = 0.363          # SFace's published cosine threshold

    def __init__(self):
        import cv2
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(_download("yunet.onnx"), "", (320, 320), 0.8, 0.3, 50)
        self.embedder = cv2.FaceRecognizerSF.create(_download("sface.onnx"), "")

    def detect(self, bgr):
        h, w = bgr.shape[:2]
        self.detector.setInputSize((w, h))
        _, rows = self.detector.detect(bgr)
        faces = []
        for r in (rows if rows is not None else []):
            x, y, fw, fh = r[:4]
            kps = r[4:14].reshape(5, 2)
            feat = self.embedder.feature(self.embedder.alignCrop(bgr, r))
            faces.append(Face(np.array([x, y, x + fw, y + fh], dtype=np.float32), kps,
                              _normalise(feat), float(r[14])))
        return faces


class InsightFaceBackend:
    name = "insightface-arcface"
    default_threshold = 0.45

    def __init__(self):
        from insightface.app import FaceAnalysis
        self.engine = FaceAnalysis(name=MODEL_NAME, providers=["CPUExecutionProvider"])
        self.engine.prepare(ctx_id=-1, det_size=(DET_SIZE, DET_SIZE))

    def detect(self, bgr):
        return [Face(np.asarray(f.bbox, dtype=np.float32), np.asarray(f.kps, dtype=np.float32),
                     _normalise(f.normed_embedding), float(f.det_score))
                for f in self.engine.get(bgr)]


def build_backend():
    if BACKEND in ("auto", "insightface"):
        try:
            return InsightFaceBackend()
        except Exception as exc:                       # not installed / failed to load
            if BACKEND == "insightface":
                raise
            print(f"[faceattend] InsightFace unavailable ({type(exc).__name__}); using OpenCV SFace")
    return OpenCVBackend()


class Recognizer:
    def __init__(self, backend=None):
        self.backend = backend or build_backend()
        self._lock = threading.Lock()
        self.gallery = dbm.load_gallery(self.backend.name)

    @property
    def threshold(self):
        t = settings.match_threshold
        return t if t is not None else self.backend.default_threshold

    def reload(self):
        """Refresh the in-memory gallery after enrolling or deleting a student."""
        self.gallery = dbm.load_gallery(self.backend.name)

    def detect(self, bgr):
        with self._lock:
            return self.backend.detect(bgr)

    def embed_largest(self, bgr):
        faces = self.detect(bgr)
        if not faces:
            return None
        return max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))

    def match(self, embedding):
        """Return (student_dict | None, best_cosine_score)."""
        students, owners, matrix = self.gallery
        if matrix.shape[0] == 0 or matrix.shape[1] != embedding.shape[0]:
            return None, 0.0
        sims = matrix @ embedding          # normalised vectors -> dot product = cosine similarity
        best = int(np.argmax(sims))
        score = float(sims[best])
        return (students[owners[best]], score) if score >= self.threshold else (None, score)
