"""Telemetry background polling service and dashboard bridge."""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .blynk_client import BlynkAuthError, BlynkClient
from .config import BlynkConfig
from .fuzzy_evaluator import RangeTrackerPy, compute_fuzzy_speed_factor, evaluate_road_classification
from .normalizer import normalize_telemetry_payload
from .storage import TelemetryStorage

logger = logging.getLogger("blynk.service")


class BlynkTelemetryService:
    """Manages periodic polling from Blynk Cloud, telemetry normalization,

    connection state machine, event transition detection, database persistence,
    and fan-out to the dashboard SSE hub.
    """

    def __init__(
        self,
        config: Optional[BlynkConfig] = None,
        storage: Optional[TelemetryStorage] = None,
        hub: Any = None,
    ):
        self.config = config or BlynkConfig()
        self.storage = storage or TelemetryStorage(self.config.database_path)
        self.hub = hub
        self.client = BlynkClient(self.config)

        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # State tracking
        self._start_time = time.time()
        self._last_poll_time: Optional[float] = None
        self._last_successful_fetch: Optional[float] = None
        self._last_raw_pins: Dict[str, Any] = {}
        self._latest_normalized: Optional[Dict[str, Any]] = None
        self._connection_status = "UNKNOWN"  # ONLINE, STALE, OFFLINE, ERROR, UNKNOWN
        self._last_error_message: Optional[str] = None

        # Transition detection
        self._last_pothole_state: Optional[bool] = None
        self._last_auto_state: Optional[bool] = None
        self._last_connection_state: Optional[str] = None

        # Range tracker for closing speed
        self._range_tracker = RangeTrackerPy()

        # Cumulative counters
        self._impacts_count = 0
        self._faults_count = 0
        self._estops_count = 0

    def start(self) -> None:
        """Starts the background telemetry polling worker."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._poll_loop, name="BlynkPollWorker", daemon=True)
            self._thread.start()
            logger.info("Blynk telemetry service started (poll interval=%s ms)", self.config.poll_interval_ms)

    def stop(self) -> None:
        """Gracefully stops the polling worker."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Blynk telemetry service stopped")

    def _poll_loop(self) -> None:
        """Main periodic polling loop."""
        # Initial small delay to let server initialize
        time.sleep(0.2)
        while self._running:
            self.poll_once()
            # Sleep in small increments for responsive shutdown
            interval = self.config.poll_interval_sec
            slept = 0.0
            while self._running and slept < interval:
                time.sleep(min(0.2, interval - slept))
                slept += 0.2

    def poll_once(self) -> Optional[Dict[str, Any]]:
        """Executes a single poll cycle with overlap protection."""
        if not self._poll_lock.acquire(blocking=False):
            # Previous poll is still running; avoid overlapping requests
            logger.debug("Skipping poll: previous request still active")
            return None

        try:
            now_epoch = time.time()
            self._last_poll_time = now_epoch
            raw_pins = {}
            hw_connected = False
            cloud_reachable = False
            error_str = None

            try:
                raw_pins, hw_connected = self.client.fetch_with_retry()
                cloud_reachable = True
                self._last_successful_fetch = now_epoch
                self._last_error_message = None
            except BlynkAuthError as e:
                self._last_error_message = f"Auth error: {e}"
                error_str = self._last_error_message
                logger.error("Blynk Authentication Error: %s", self._last_error_message)
            except Exception as e:
                self._last_error_message = f"Fetch error: {e}"
                error_str = self._last_error_message
                logger.warning("Blynk fetch failed: %s", self._last_error_message)

            # Determine connection status
            time_since_success = (now_epoch - self._last_successful_fetch) if self._last_successful_fetch else 999999.0
            is_stale = time_since_success > self.config.stale_after_sec

            if error_str and not self._last_successful_fetch:
                status = "ERROR"
            elif cloud_reachable and hw_connected and not is_stale:
                status = "ONLINE"
            elif cloud_reachable and (not hw_connected or is_stale):
                # Cloud is reachable, but hardware is disconnected or data has aged
                status = "STALE" if (time_since_success < self.config.stale_after_sec * 2) else "OFFLINE"
            else:
                status = "OFFLINE" if is_stale else "STALE"

            self._connection_status = status

            # Use latest pins if fetch succeeded, else retain cached pins if available
            if raw_pins:
                self._last_raw_pins = raw_pins

            pins_to_normalize = self._last_raw_pins

            # Normalize telemetry payload
            payload = normalize_telemetry_payload(
                raw_pins=pins_to_normalize,
                device_id=self.config.device_id,
                cloud_reachable=cloud_reachable,
                hardware_connected=hw_connected,
                telemetry_fresh=(status == "ONLINE"),
                status=status.lower(),
                received_at=datetime.now(timezone.utc),
            )

            with self._lock:
                self._latest_normalized = payload

            # Handle transition events (pothole, autonomous mode, connection)
            self._process_transitions(payload, status)

            # Persist to database
            if cloud_reachable and pins_to_normalize:
                try:
                    self.storage.save_telemetry(payload)
                except Exception as e:
                    logger.error("Database storage error: %s", e)

            # Publish to dashboard hub
            if self.hub:
                self._publish_to_hub(payload, status)

            return payload

        finally:
            self._poll_lock.release()

    def _process_transitions(self, payload: Dict[str, Any], current_status: str) -> None:
        """Detects state changes and logs transition events without duplicates."""
        tel = payload.get("telemetry", {})
        now_iso = payload.get("receivedAt")

        # 1. Pothole transition detection
        pothole_state = tel.get("potholeDetected")
        road_alert = tel.get("roadAlert")
        is_pothole_now = bool(pothole_state) or (road_alert == "POTHOLE!")

        if self._last_pothole_state is not None:
            if not self._last_pothole_state and is_pothole_now:
                # Transition: CLEAR -> POTHOLE
                self._impacts_count += 1
                msg = f"Pothole impact detected! (distance: {tel.get('distanceCm')} cm, pitch: {tel.get('pitchDeg')}°)"
                self.storage.save_event(
                    event_type="POTHOLE_ALERT",
                    message=msg,
                    severity="warn",
                    device_id=self.config.device_id,
                    data=tel,
                    received_at=now_iso,
                )
                if self.hub:
                    self.hub.publish(f"Impact: {msg}")
            elif self._last_pothole_state and not is_pothole_now:
                # Transition: POTHOLE -> CLEAR
                msg = "Road surface clear, pothole alert ended."
                self.storage.save_event(
                    event_type="POTHOLE_CLEARED",
                    message=msg,
                    severity="info",
                    device_id=self.config.device_id,
                    data=tel,
                    received_at=now_iso,
                )
                if self.hub:
                    self.hub.publish("State: Road surface clear")

        self._last_pothole_state = is_pothole_now

        # 2. Autonomous mode transition detection
        auto_state = tel.get("autoActive")
        if auto_state is not None:
            if self._last_auto_state is not None and self._last_auto_state != auto_state:
                state_str = "ACTIVE" if auto_state else "STOPPED"
                msg = f"Rover autonomous mode changed to {state_str} (V0={1 if auto_state else 0})"
                self.storage.save_event(
                    event_type="AUTO_MODE_CHANGE",
                    message=msg,
                    severity="info",
                    device_id=self.config.device_id,
                    data={"autoActive": auto_state},
                    received_at=now_iso,
                )
                if self.hub:
                    from_st = "RUNNING" if self._last_auto_state else "IDLE"
                    to_st = "RUNNING" if auto_state else "IDLE"
                    self.hub.publish(f"State {from_st} -> {to_st}")

            self._last_auto_state = auto_state

        # 3. Hardware connection status transition
        if self._last_connection_state is not None and self._last_connection_state != current_status:
            msg = f"Rover connection state changed: {self._last_connection_state} -> {current_status}"
            severity = "info" if current_status == "ONLINE" else "warn"
            self.storage.save_event(
                event_type="CONNECTION_CHANGE",
                message=msg,
                severity=severity,
                device_id=self.config.device_id,
                data={"status": current_status},
                received_at=now_iso,
            )
            if self.hub:
                if current_status == "ONLINE":
                    self.hub.publish("# blynk: Rover hardware connected and online")
                elif current_status == "STALE":
                    self.hub.publish("# blynk: Rover telemetry is STALE (hardware offline or delayed)")
                elif current_status == "OFFLINE":
                    self.hub.publish("FAULT Rover hardware is OFFLINE")
                    self._faults_count += 1

        self._last_connection_state = current_status

    def _publish_to_hub(self, payload: Dict[str, Any], status: str) -> None:
        """Formats and broadcasts J,{...} JSON snapshots to the dashboard SSE hub."""
        tel = payload.get("telemetry", {})
        now_sec = time.time() - self._start_time

        # Update range tracker
        dist_val = tel.get("distanceCm")
        _, closing_speed = self._range_tracker.update(dist_val, now_sec)

        # Evaluate fuzzy speed recommendation
        is_pothole = bool(tel.get("potholeDetected")) or (tel.get("roadAlert") == "POTHOLE!")
        fuzzy_u = compute_fuzzy_speed_factor(dist_val, closing_speed, is_pothole)

        # State determination
        auto_on = bool(tel.get("autoActive"))
        is_hw_online = (status == "ONLINE")
        cloud_reachable = bool(payload.get("connection", {}).get("cloudReachable"))

        if not is_hw_online:
            # Physical ESP32 is OFFLINE or Stale
            state = "OFFLINE"
            fault_reason = "ESP32 Offline" if status == "OFFLINE" else ("Telemetry Stale" if status == "STALE" else "Blynk Error")
            surface = "OFFLINE"
            top = "OFFLINE"
            probs = [0.0, 0.0, 0.0, 0.0]
            conf = 0.0
            uncertain = 1
            s_val = 0.0
            c_eff = 0.0
            motor_val = 0
            u_val = 0.0
            ready_val = 0
            dist_status = "stale" if dist_val is not None else "none"
            dist_out = round(dist_val, 1) if dist_val is not None else -1.0
            pitch_out = round(tel.get("pitchDeg"), 2) if tel.get("pitchDeg") is not None else 0.0
            roll_out = round(tel.get("rollDeg"), 2) if tel.get("rollDeg") is not None else 0.0
            accel_out = round(tel.get("accelZMs2"), 3) if tel.get("accelZMs2") is not None else 0.0
            servo_out = tel.get("servoAngle") if tel.get("servoAngle") is not None else 90
            classifier_name = "hardware-offline"
        else:
            # Physical ESP32 is verified ONLINE with fresh data
            ready_val = 1
            uncertain = 0
            conf = 0.95 if is_pothole else 0.90
            s_val = 1.0 if is_pothole else 0.5
            c_eff = 0.8
            motor_val = 120 if auto_on else 0
            u_val = fuzzy_u
            surface = "POTHOLE" if is_pothole else "SMOOTH"
            top = "POTHOLE" if is_pothole else "SMOOTH"
            probs = [0.0, 0.0, 0.0, 1.0] if is_pothole else [1.0, 0.0, 0.0, 0.0]
            classifier_name = "hardware-pothole"

            if auto_on:
                if dist_val is not None and dist_val <= 10.0:
                    state = "ESTOP"
                    fault_reason = "Obstacle emergency stop"
                    self._estops_count += 1
                elif dist_val is not None and dist_val <= 60.0:
                    state = "APPROACH"
                    fault_reason = ""
                else:
                    state = "RUNNING"
                    fault_reason = ""
            else:
                state = "IDLE"
                fault_reason = ""

            if dist_val is None:
                dist_status = "none"
                dist_out = -1.0
            elif dist_val < 2.0:
                dist_status = "TOO_CLOSE"
                dist_out = dist_val
            else:
                dist_status = "OK"
                dist_out = dist_val

            pitch_out = round(tel.get("pitchDeg") or 0.0, 2)
            roll_out = round(tel.get("rollDeg") or 0.0, 2)
            accel_out = round(tel.get("accelZMs2") or 0.0, 3)
            servo_out = tel.get("servoAngle") if tel.get("servoAngle") is not None else 90

        # Construct J,{...} JSON snapshot
        snapshot = {
            "source": "blynk",
            "hw_online": 1 if is_hw_online else 0,
            "cloud_online": 1 if cloud_reachable else 0,
            "hw_status": status,
            "t": round(now_sec, 2),
            "state": state,
            "fault": fault_reason,
            "imu_fault": 1 if tel.get("pitchDeg") is None else 0,
            "ready": ready_val,
            "classifier": classifier_name,
            "p": probs,
            "top": top,
            "surface": surface,
            "conf": conf,
            "mi": 0.0,
            "novelty": 0.0,
            "uncertain": uncertain,
            "jerk": 0.0,
            "p2p": 0.0,
            "rms": 0.0,
            "curve": "graded",
            "s": s_val,
            "gate": 0.6 if is_hw_online else 0.0,
            "c_eff": c_eff,
            "s_exp": s_val,
            "s_risk": 0.8 if (is_hw_online and is_pothole) else 0.0,
            "s_sky": 0.0,
            "c_low": 0.4,
            "c_high": 0.8,
            "s_safe": 0.5,
            "servo": servo_out,
            "servo_manual": 0,
            "speed_mode": "fuzzy-it2",
            "motor": motor_val,
            "u": u_val,
            "dist_status": dist_status,
            "dist": round(dist_out, 1),
            "range": round(dist_out, 1),
            "closing": round(closing_speed if is_hw_online else 0.0, 1),
            "roll": roll_out,
            "pitch": pitch_out,
            "vz": 0.0,
            "a_vert": accel_out,
            "maint": "HEALTHY",
            "maint_zeta": 0.28,
            "maint_tests": 0,
            "estops": self._estops_count,
            "faults": self._faults_count,
            "impacts": self._impacts_count,
        }

        line = "J," + json.dumps(snapshot, separators=(",", ":"))
        self.hub.publish(line)

    def get_latest_normalized(self) -> Dict[str, Any]:
        """Returns the most recent normalized telemetry payload."""
        with self._lock:
            if self._latest_normalized:
                return self._latest_normalized
            # If not yet polled, return default initial structure
            return normalize_telemetry_payload(
                raw_pins={},
                device_id=self.config.device_id,
                cloud_reachable=False,
                hardware_connected=False,
                telemetry_fresh=False,
                status="unknown",
            )

    def get_status(self) -> Dict[str, Any]:
        """Returns comprehensive connectivity and health status."""
        now = time.time()
        time_since_fetch = (now - self._last_successful_fetch) if self._last_successful_fetch else None
        return {
            "deviceId": self.config.device_id,
            "source": "blynk",
            "connectionStatus": self._connection_status,
            "cloudReachable": self._connection_status in ("ONLINE", "STALE"),
            "hardwareConnected": self._connection_status == "ONLINE",
            "telemetryFresh": self._connection_status == "ONLINE",
            "pollIntervalMs": self.config.poll_interval_ms,
            "staleThresholdMs": self.config.stale_after_ms,
            "lastSuccessfulFetchSecAgo": round(time_since_fetch, 2) if time_since_fetch is not None else None,
            "lastError": self._last_error_message,
            "counters": {
                "impacts": self._impacts_count,
                "faults": self._faults_count,
                "estops": self._estops_count,
            },
        }
