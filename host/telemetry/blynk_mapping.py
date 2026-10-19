"""Centralized mapping and specifications for Blynk IoT virtual datastreams.

ESP32 Rover Virtual Pin Mapping:
- V0:  Autonomous mode switch (Boolean, 0=OFF, 1=ON)
- V5:  Pothole detection flag (Boolean, 0=CLEAR, 1=POTHOLE)
- V6:  Ultrasonic distance in cm (Number, 0..400)
- V7:  Pothole status text (String, "POTHOLE!" or "CLEAR")
- V8:  MPU6050 pitch in degrees (Number)
- V9:  MPU6050 roll in degrees (Number)
- V10: MPU6050 Z-axis acceleration in m/s^2 (Number)
- V11: Obstacle scanner servo angle in degrees (Number, 0..180)
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class DatastreamSpec:
    pin: str
    backend_field: str
    data_type: type
    unit: str
    description: str
    is_control: bool = False
    is_event_driven: bool = False


# Centralized Virtual Pin Registry
PIN_SPECS: Dict[str, DatastreamSpec] = {
    "v0": DatastreamSpec(
        pin="v0",
        backend_field="autoActive",
        data_type=bool,
        unit="",
        description="Autonomous mode switch",
        is_control=True,
    ),
    "v5": DatastreamSpec(
        pin="v5",
        backend_field="potholeDetected",
        data_type=bool,
        unit="",
        description="Pothole detection flag",
        is_event_driven=True,
    ),
    "v6": DatastreamSpec(
        pin="v6",
        backend_field="distanceCm",
        data_type=float,
        unit="cm",
        description="Ultrasonic obstacle distance",
    ),
    "v7": DatastreamSpec(
        pin="v7",
        backend_field="roadAlert",
        data_type=str,
        unit="",
        description="Pothole alert status text",
        is_event_driven=True,
    ),
    "v8": DatastreamSpec(
        pin="v8",
        backend_field="pitchDeg",
        data_type=float,
        unit="deg",
        description="MPU6050 pitch angle",
    ),
    "v9": DatastreamSpec(
        pin="v9",
        backend_field="rollDeg",
        data_type=float,
        unit="deg",
        description="MPU6050 roll angle",
    ),
    "v10": DatastreamSpec(
        pin="v10",
        backend_field="accelZMs2",
        data_type=float,
        unit="m/s^2",
        description="MPU6050 vertical Z acceleration",
    ),
    "v11": DatastreamSpec(
        pin="v11",
        backend_field="servoAngle",
        data_type=int,
        unit="deg",
        description="Ultrasonic scanning servo angle",
    ),
}

# Reverse lookup by backend field name
FIELD_TO_SPEC: Dict[str, DatastreamSpec] = {
    spec.backend_field: spec for spec in PIN_SPECS.values()
}

# Standardized road alert strings recognized from firmware
ROAD_ALERT_POTHOLE = "POTHOLE!"
ROAD_ALERT_CLEAR = "CLEAR"
