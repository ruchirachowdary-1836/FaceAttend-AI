"""Central configuration. Every value can be overridden with an environment variable."""
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
MODELS_DIR = Path(os.getenv("MODELS_DIR", BASE_DIR / "models"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
MODELS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "attendance.db"))

# "auto" prefers InsightFace (ArcFace) when installed, otherwise OpenCV (YuNet + SFace).
BACKEND = os.getenv("BACKEND", "auto").lower()
MODEL_NAME = os.getenv("MODEL_NAME", "buffalo_l")        # InsightFace pack; buffalo_s = lighter
DET_SIZE = int(os.getenv("DET_SIZE", "640"))

# Auth: leave ADMIN_PASSWORD empty for open (kiosk / demo) mode.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "") or os.urandom(24).hex()
TOKEN_TTL_HOURS = int(os.getenv("TOKEN_TTL_HOURS", "12"))

# Optional SMTP for absentee alerts. Without SMTP_HOST the alerts run as a dry run.
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "faceattend@localhost")

ATTENDANCE_TARGET = int(os.getenv("ATTENDANCE_TARGET", "75"))   # % below which a student is "at risk"


@dataclass
class RuntimeSettings:
    """Tunables that the Settings screen can change while the server is running."""
    match_threshold: float | None = (
        float(os.environ["MATCH_THRESHOLD"]) if os.getenv("MATCH_THRESHOLD") else None)
    confirm_frames: int = int(os.getenv("CONFIRM_FRAMES", "3"))
    liveness: bool = os.getenv("LIVENESS", "off").lower() in ("1", "on", "true", "motion")
    liveness_min_yaw: float = float(os.getenv("LIVENESS_MIN_YAW", "0.07"))
    min_face_px: int = int(os.getenv("MIN_FACE_PX", "60"))


settings = RuntimeSettings()
