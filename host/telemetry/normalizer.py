"""Telemetry data normalization and validation for Blynk IoT datastreams."""

from datetime import datetime, timezone
import math
from typing import Any, Dict, Optional, Tuple

from .blynk_mapping import (
    PIN_SPECS,
    ROAD_ALERT_CLEAR,
    ROAD_ALERT_POTHOLE,
)


def _safe_float(val: Any) -> Optional[float]:
    """Safely converts value to float, preserving 0.0 and rejecting NaN/inf."""
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> Optional[int]:
    """Safely converts value to int, preserving 0."""
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return int(round(f))
    except (ValueError, TypeError):
        return None


def _safe_bool(val: Any) -> Optional[bool]:
    """Safely converts numeric or string flags to boolean."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(int(val) != 0)
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("1", "true", "t", "yes", "on"):
            return True
        if v in ("0", "false", "f", "no", "off", ""):
            return False
    return None


def _safe_str(val: Any) -> Optional[str]:
    """Safely normalizes string values."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def normalize_telemetry_payload(
    raw_pins: Dict[str, Any],
    device_id: str = "esp32-rover-01",
    cloud_reachable: bool = True,
    hardware_connected: bool = True,
    telemetry_fresh: bool = True,
    status: str = "online",
    received_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Converts raw Blynk pin values into a standardized, validated telemetry object.

    Handles lower/upper case pin keys (e.g. 'v6' or 'V6').
    """
    if received_at is None:
        received_at = datetime.now(timezone.utc)

    # Normalize keys to lowercase
    pins = {str(k).lower(): v for k, v in (raw_pins or {}).items()}

    # V0: Autonomous mode switch (Boolean)
    auto_active = _safe_bool(pins.get("v0"))

    # V5: Pothole detection flag (Boolean)
    pothole_detected = _safe_bool(pins.get("v5"))

    # V6: Distance in cm (Number, valid 1..399 cm from HC-SR04)
    raw_dist = _safe_float(pins.get("v6"))
    if raw_dist is not None and (raw_dist <= 0 or raw_dist >= 400):
        # Firmware suppresses distances <= 0 or >= 400
        distance_cm = None
    else:
        distance_cm = raw_dist

    # V7: Road alert text (POTHOLE! or CLEAR)
    road_alert = _safe_str(pins.get("v7"))
    if road_alert:
        # Standardize known alert keywords
        alert_upper = road_alert.upper()
        if "POTHOLE" in alert_upper:
            road_alert = ROAD_ALERT_POTHOLE
        elif "CLEAR" in alert_upper:
            road_alert = ROAD_ALERT_CLEAR

    # V8: Pitch degrees
    pitch_deg = _safe_float(pins.get("v8"))

    # V9: Roll degrees
    roll_deg = _safe_float(pins.get("v9"))

    # V10: Z-axis acceleration (m/s^2) - DO NOT fallback to 9.81 if missing
    accel_z = _safe_float(pins.get("v10"))

    # V11: Scanning servo angle (0..180 deg)
    servo_angle = _safe_int(pins.get("v11"))
    if servo_angle is not None:
        servo_angle = max(0, min(180, servo_angle))

    # Determine data quality
    essential_fields = [pitch_deg, roll_deg, accel_z]
    if all(f is not None for f in essential_fields):
        quality = "GOOD"
    elif any(f is not None for f in essential_fields):
        quality = "PARTIAL"
    else:
        quality = "EMPTY" if not pins else "INVALID"

    return {
        "deviceId": device_id,
        "source": "blynk",
        "receivedAt": received_at.isoformat(),
        "connection": {
            "cloudReachable": cloud_reachable,
            "hardwareConnected": hardware_connected,
            "telemetryFresh": telemetry_fresh,
            "status": status,
        },
        "telemetry": {
            "distanceCm": distance_cm,
            "pitchDeg": pitch_deg,
            "rollDeg": roll_deg,
            "accelZMs2": accel_z,
            "servoAngle": servo_angle,
            "autoActive": auto_active,
            "potholeDetected": pothole_detected,
            "roadAlert": road_alert,
        },
        "dataQuality": quality,
    }
