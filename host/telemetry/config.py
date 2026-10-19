"""Configuration management for Blynk IoT integration and rover backend."""

import os
import re
from typing import Optional


def _load_dotenv(filepath: str) -> None:
    """Minimal standard-library .env file parser."""
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
    except Exception:
        pass


# Look for .env in project root or current directory
_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_load_dotenv(os.path.join(_ROOT_DIR, ".env"))
_load_dotenv(".env")


def mask_token(token: Optional[str]) -> str:
    """Masks a sensitive token for safe logging/display (e.g. '***aCNl')."""
    if not token:
        return "<none>"
    if len(token) <= 8:
        return "***"
    return f"***{token[-6:]}"


def sanitize_url(url: str) -> str:
    """Sanitizes any URL by redacting the 'token' query parameter."""
    if not url:
        return ""
    return re.sub(r"([?&]token=)([^&]+)", r"\1***", url)


class BlynkConfig:
    def __init__(self):
        self.base_url: str = os.getenv("BLYNK_BASE_URL", "https://blynk.cloud").rstrip("/")
        self.auth_token: str = os.getenv("BLYNK_AUTH_TOKEN", "").strip()
        self.poll_interval_ms: int = int(os.getenv("BLYNK_POLL_INTERVAL_MS", "2000"))
        self.request_timeout_ms: int = int(os.getenv("BLYNK_REQUEST_TIMEOUT_MS", "5000"))
        self.stale_after_ms: int = int(os.getenv("TELEMETRY_STALE_AFTER_MS", "10000"))
        self.device_id: str = os.getenv("ROVER_DEVICE_ID", "esp32-rover-01")
        self.database_path: str = os.getenv("DATABASE_PATH", os.path.join("host", "data", "telemetry.db"))
        self.port: int = int(os.getenv("PORT", "8080"))

    @property
    def poll_interval_sec(self) -> float:
        return max(0.5, self.poll_interval_ms / 1000.0)

    @property
    def request_timeout_sec(self) -> float:
        return max(1.0, self.request_timeout_ms / 1000.0)

    @property
    def stale_after_sec(self) -> float:
        return max(2.0, self.stale_after_ms / 1000.0)

    @property
    def is_configured(self) -> bool:
        return bool(self.auth_token and self.auth_token != "YOUR_BLYNK_AUTH_TOKEN")

    def __repr__(self) -> str:
        return (
            f"BlynkConfig(base_url='{self.base_url}', token={mask_token(self.auth_token)}, "
            f"poll_interval={self.poll_interval_ms}ms, stale_after={self.stale_after_ms}ms, "
            f"device_id='{self.device_id}', db='{self.database_path}')"
        )
