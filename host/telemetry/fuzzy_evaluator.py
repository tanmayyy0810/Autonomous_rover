"""Fuzzy speed recommendation and classification adapter for live telemetry.

Implements distance-based fuzzy speed factor recommendation mirroring the core logic
while keeping recommended values strictly separated from actual motor execution.
Reports missing high-frequency IMU requirements for the full neural ensemble.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


class RangeTrackerPy:
    """Alpha-beta filter tracking obstacle distance and closing speed."""

    def __init__(self, alpha: float = 0.6, beta: float = 0.2):
        self.alpha = alpha
        self.beta = beta
        self.valid = False
        self.d = 0.0
        self.v = 0.0
        self.last_t: Optional[float] = None

    def reset(self) -> None:
        self.valid = False
        self.d = 0.0
        self.v = 0.0
        self.last_t = None

    def update(self, cm: Optional[float], t_now: float) -> Tuple[float, float]:
        """Updates distance and closing speed (positive = getting closer)."""
        if cm is None or cm < 0:
            # Out of range / clear path
            self.reset()
            return -1.0, 0.0

        if not self.valid or self.last_t is None:
            self.d = cm
            self.v = 0.0
            self.valid = True
            self.last_t = t_now
            return self.d, self.v

        dt = max(0.01, min(1.0, t_now - self.last_t))
        self.last_t = t_now

        # Prediction
        d_pred = self.d - self.v * dt
        residual = cm - d_pred

        # Correction
        self.d = d_pred + self.alpha * residual
        self.v = self.v - (self.beta / dt) * residual

        return self.d, self.v


def compute_fuzzy_speed_factor(
    distance_cm: Optional[float],
    closing_cms: float = 0.0,
    is_pothole: bool = False,
) -> float:
    """Computes recommended drive speed factor u in [0, 1] based on genuine distance.

    Safety thresholds:
    - Distance <= 10 cm: Emergency stop recommendation (u = 0.0)
    - Distance <= 20 cm: Slow crawl (u = 0.2 .. 0.3)
    - Distance <= 60 cm: Approach deceleration band (u proportional to distance)
    - Path clear (> 60 cm): Full speed (u = 1.0)
    - Active pothole detection reduces recommended speed factor by 40% for vehicle stability.
    """
    if distance_cm is None or distance_cm < 0:
        # Path is clear
        base_u = 1.0
    elif distance_cm <= 10.0:
        base_u = 0.0
    elif distance_cm <= 20.0:
        # Slow crawl band
        base_u = 0.15 + (distance_cm - 10.0) * 0.015
    elif distance_cm <= 60.0:
        # Approach band
        base_u = 0.30 + ((distance_cm - 20.0) / 40.0) * 0.70
    else:
        base_u = 1.0

    # Closing speed penalty (if approaching rapidly)
    if closing_cms > 15.0:
        base_u = max(0.0, base_u - 0.25)

    # Road surface penalty (slow down on pothole)
    if is_pothole:
        base_u = min(base_u, 0.40)

    return max(0.0, min(1.0, round(base_u, 3)))


@dataclass
class RoadClassificationInfo:
    """Status of road classification based on available vs missing signals."""
    current_surface: str
    confidence: float
    is_alert: bool
    ml_ensemble_available: bool = False
    status_note: str = ""
    required_missing_signals: Tuple[str, ...] = (
        "100Hz Raw 6-axis IMU burst (ax, ay, az, gx, gy, gz)",
        "Jerk computation buffer (100 samples/sec)",
        "Rolling RMS & peak-to-peak vibration window",
        "FFT band energy distribution",
    )


def evaluate_road_classification(
    pothole_detected: Optional[bool],
    road_alert: Optional[str],
) -> RoadClassificationInfo:
    """Evaluates road classification using available firmware-reported pothole alerts.

    Explicitly reports that full ML ensemble inference is in degraded/summary mode
    because the ESP32 does not stream high-frequency raw 100Hz IMU buffers over Blynk.
    """
    is_pothole = bool(pothole_detected) or (road_alert == "POTHOLE!")
    if is_pothole:
        surface = "POTHOLE"
        conf = 0.95
        note = "Pothole detected by firmware IMU jerk threshold (V5/V7)"
    else:
        surface = "SMOOTH"
        conf = 0.85
        note = "Road path nominal / clear (firmware summary mode)"

    return RoadClassificationInfo(
        current_surface=surface,
        confidence=conf,
        is_alert=is_pothole,
        ml_ensemble_available=False,
        status_note=note,
    )
