# ESP32 Rover - Blynk IoT Cloud Telemetry Integration

This document describes the architecture, telemetry pipeline, configuration, APIs, and verification procedures for ingesting live ESP32 hardware sensor readings from Blynk IoT Cloud into the autonomous rover software backend and dashboard.

---

## 1. Pipeline Architecture

```
+-------------------------------------------------------------+
|                     ESP32 Hardware Rover                     |
|  - HC-SR04 Ultrasonic Distance Sensor (TRIG=25, ECHO=26)    |
|  - MPU6050 Accelerometer & Gyroscope (SDA=21, SCL=22)       |
|  - Ultrasonic Scanner Servo Motor (PWM=33)                  |
|  - L298N Dual DC Motor Driver & Motors                      |
|  - Firmware Timers: 250ms Telemetry, 60ms IMU Check        |
+------------------------------+------------------------------+
                               | Wi-Fi (Blynk.virtualWrite)
                               v
+-------------------------------------------------------------+
|                      Blynk IoT Cloud                        |
|  Endpoints:                                                 |
|    GET /external/api/getAll?token=...                       |
|    GET /external/api/isHardwareConnected?token=...         |
+------------------------------+------------------------------+
                               | HTTPS Polling (2000 ms)
                               v
+-------------------------------------------------------------+
|                   Backend Telemetry Layer                   |
|  - host/telemetry/blynk_client.py   (HTTPS Auth & Retries)  |
|  - host/telemetry/normalizer.py     (Type-Safe Formatting)  |
|  - host/telemetry/service.py        (Polling & Transitions) |
|  - host/telemetry/fuzzy_evaluator.py(Speed Recommendations) |
|  - host/telemetry/storage.py        (SQLite Database)       |
+--------------+-------------------------------+--------------+
               |                               |
               v                               v
+-----------------------------+ +-----------------------------+
|    SQLite Database          | |   Live Dashboard Server     |
|   host/data/telemetry.db    | |   host/dashboard/server.py  |
| - telemetry records         | | - GET /events (SSE stream)  |
| - transition event log      | | - GET /api/telemetry/*      |
| - indexes on received_at    | | - host/dashboard/index.html |
+-----------------------------+ +-----------------------------+
```

---

## 2. Virtual Pin Mapping Specification

The backend maps incoming datastreams using a centralized specification in [`host/telemetry/blynk_mapping.py`](../host/telemetry/blynk_mapping.py):

| Virtual Pin | Meaning | Backend Field | Data Type | Units / Format | Validation & Handling |
|---|---|---|---|---|---|
| **V0** | Autonomous mode switch | `autoActive` | Boolean | True / False | Normalized from 0/1 or string representations |
| **V5** | Pothole detection flag | `potholeDetected` | Boolean | True / False | Transition detection prevents duplicate events |
| **V6** | Ultrasonic distance | `distanceCm` | Number | cm (1–399) | `None` when absent or out of bounds (no fake values) |
| **V7** | Pothole alert status text | `roadAlert` | String | "POTHOLE!" / "CLEAR" | Normalized text |
| **V8** | MPU6050 Pitch | `pitchDeg` | Number | degrees | Float conversion; rejects NaN/Inf |
| **V9** | MPU6050 Roll | `rollDeg` | Number | degrees | Float conversion; rejects NaN/Inf |
| **V10**| MPU6050 Z-Acceleration | `accelZMs2` | Number | m/s² | Float conversion; no artificial 9.81 fallback |
| **V11**| Scanner Servo Angle | `servoAngle` | Number | degrees (0–180) | Treated strictly as obstacle scanning angle |

---

## 3. Environment Configuration

Configuration is loaded from backend environment variables or a local `.env` file (which is excluded from Git via `.gitignore`).

Template file: [`.env.example`](../.env.example)

```env
# Blynk IoT Cloud Configuration
BLYNK_BASE_URL=https://blynk.cloud
BLYNK_AUTH_TOKEN=YOUR_BLYNK_AUTH_TOKEN
BLYNK_POLL_INTERVAL_MS=2000
BLYNK_REQUEST_TIMEOUT_MS=5000
TELEMETRY_STALE_AFTER_MS=10000

# Rover Identification
ROVER_DEVICE_ID=esp32-rover-01

# Storage
DATABASE_PATH=host/data/telemetry.db

# Server Port
PORT=8080
```

> [!SECURITY]
> Authentication tokens are NEVER exposed to the frontend browser, printed in application logs, or committed to version control. All URL query strings containing tokens are automatically masked (e.g., `token=***`).

---

## 4. Hardware Connection & Offline Detection

The backend implements a multi-state connection model:

- **ONLINE**: Blynk Cloud is reachable, the physical ESP32 hardware is actively connected (`isHardwareConnected == true`), and readings are within the freshness threshold.
- **STALE**: Blynk Cloud responded with cached datastreams, but the physical hardware has disconnected or telemetry is older than `TELEMETRY_STALE_AFTER_MS` (10 seconds).
- **OFFLINE**: Rover hardware is verified disconnected, or cloud server is unreachable.
- **ERROR**: Authentication failure (e.g. invalid token) or persistent HTTP errors.

When the hardware goes offline, the dashboard displays cached readings with an explicit **OFFLINE / STALE** indicator and does NOT fabricate live intermediate values.

---

## 5. REST API Endpoints

The backend server exposes the following REST endpoints on `http://localhost:8080`:

### `GET /api/telemetry/latest`
Returns the most recent normalized and validated hardware telemetry object.

```json
{
  "deviceId": "esp32-rover-01",
  "source": "blynk",
  "receivedAt": "2026-10-19T05:51:24.123456+00:00",
  "connection": {
    "cloudReachable": true,
    "hardwareConnected": false,
    "telemetryFresh": false,
    "status": "stale"
  },
  "telemetry": {
    "distanceCm": 31.0,
    "pitchDeg": 62.019,
    "rollDeg": 0.298,
    "accelZMs2": 4.151,
    "servoAngle": 90,
    "autoActive": false,
    "potholeDetected": true,
    "roadAlert": "POTHOLE!"
  },
  "dataQuality": "GOOD"
}
```

### `GET /api/telemetry/history`
Returns historical records from the SQLite database with chronological ordering.
- Query parameters:
  - `limit` (optional, default 100, max 1000)
  - `since` (optional, ISO timestamp)
  - `until` (optional, ISO timestamp)

### `GET /api/telemetry/status`
Returns connectivity, polling health, and error status:
```json
{
  "deviceId": "esp32-rover-01",
  "source": "blynk",
  "connectionStatus": "STALE",
  "cloudReachable": true,
  "hardwareConnected": false,
  "telemetryFresh": false,
  "pollIntervalMs": 2000,
  "staleThresholdMs": 10000,
  "lastSuccessfulFetchSecAgo": 1.25,
  "lastError": null,
  "counters": {
    "impacts": 1,
    "faults": 0,
    "estops": 0
  }
}
```

### `GET /api/telemetry/events`
Returns transition events (e.g. pothole alert triggers, clearing, autonomous mode toggles, connection state changes).
- Query parameters:
  - `limit` (optional, default 50)
  - `since` (optional, ISO timestamp)

---

## 6. How to Run & Verify

### Step 1: Run Automated Integration Tests (16/16 test cases)
```sh
python host/tests/test_blynk_integration.py
```

### Step 2: Test Live Blynk Cloud Connection via Diagnostics CLI
```sh
python tools/test_blynk_live.py
```
For continuous terminal monitoring:
```sh
python tools/test_blynk_live.py --watch
```

### Step 3: Start Dashboard Server
```sh
# Defaults to Blynk mode if BLYNK_AUTH_TOKEN is configured in .env
python host/dashboard/server.py

# Or explicitly:
python host/dashboard/server.py --blynk
```
Open **http://localhost:8080** in your browser.

---

## 7. Algorithm Compatibility & Firmware Recommendations

### Road Classification
- **Current Capability**: The ESP32 firmware locally evaluates IMU disturbances and reports pothole status via V5 and V7. The backend maps this into road detection ("POTHOLE" vs "CLEAR/SMOOTH").
- **Neural Network Limitation**: The repository's C++ quantized neural ensemble (`src/core/ml_model.cpp`) requires a 100-sample high-frequency buffer of 6-axis raw IMU data (ax, ay, az, gx, gy, gz sampled at 100 Hz over 1 second). Because low-bandwidth Blynk Cloud datastreams cannot sustain 100 Hz raw vector streaming, the ML ensemble runs in summary mode.
- **Future Enhancement**: To retrain and execute the full neural ensemble on physical hardware recordings, drive the rover with USB serial logging enabled (`tools/record_cart.py`) or implement local SPIFFS SD-card recording.

### Fuzzy Speed Control
- **Current Firmware**: Uses `SAFE_DISTANCE_CM = 25` with simple obstacle avoidance logic locally on the ESP32.
- **Backend Role**: Computes recommended interval Type-2 fuzzy speed factors ($u \in [0, 1]$) from live distance readings and displays recommendations on the dashboard, strictly separated from motor execution. Local hardware motor autonomy remains intact.

### Scanning Servo vs Suspension Actuator
- V11 reports the obstacle-avoidance scanning servo angle (e.g. 90° center, 150° left, 30° right).
- The backend labels V11 as **SCANNER SERVO** rather than suspension stiffness, avoiding conflicts with the servo's physical mounting.
