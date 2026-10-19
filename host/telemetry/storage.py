"""SQLite persistence layer for rover telemetry and transition events."""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("blynk.storage")


class TelemetryStorage:
    """Thread-safe SQLite storage for genuine hardware telemetry and system events."""

    def __init__(self, db_path: str = "host/data/telemetry.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._last_saved_signature: Optional[str] = None
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Creates a connection with row factory configured."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initializes database schema and indexes."""
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        try:
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                # Telemetry readings table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS telemetry (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        device_timestamp TEXT,
                        distance_cm REAL,
                        pitch_deg REAL,
                        roll_deg REAL,
                        accel_z_ms2 REAL,
                        servo_angle INTEGER,
                        auto_active INTEGER,
                        pothole_detected INTEGER,
                        road_alert TEXT,
                        connection_status TEXT NOT NULL,
                        data_quality TEXT NOT NULL,
                        raw_payload TEXT
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_telemetry_time
                    ON telemetry (received_at DESC)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_telemetry_device
                    ON telemetry (device_id, received_at DESC)
                """)

                # Detection and system events table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_id TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        message TEXT NOT NULL,
                        data TEXT
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_events_time
                    ON events (received_at DESC)
                """)
                conn.commit()
        except Exception as e:
            logger.error("Database initialization error on %s: %s", self.db_path, e)

    def _signature(self, tel: Dict[str, Any], conn_status: str) -> str:
        """Generates a signature of sensor values to prevent storing identical back-to-back readings."""
        return (
            f"{tel.get('distanceCm')}:{tel.get('pitchDeg')}:{tel.get('rollDeg')}:"
            f"{tel.get('accelZMs2')}:{tel.get('servoAngle')}:{tel.get('autoActive')}:"
            f"{tel.get('potholeDetected')}:{tel.get('roadAlert')}:{conn_status}"
        )

    def save_telemetry(self, payload: Dict[str, Any], force: bool = False) -> Optional[int]:
        """Persists a normalized telemetry reading.

        Skips duplicate consecutive readings unless force=True or values changed.
        """
        device_id = payload.get("deviceId", "esp32-rover-01")
        source = payload.get("source", "blynk")
        received_at = payload.get("receivedAt", datetime.now(timezone.utc).isoformat())
        conn_info = payload.get("connection", {})
        status = conn_info.get("status", "unknown")
        tel = payload.get("telemetry", {})
        quality = payload.get("dataQuality", "UNKNOWN")

        sig = self._signature(tel, status)
        if not force and sig == self._last_saved_signature:
            # Skip identical telemetry to avoid database bloat while values are static
            return None

        try:
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO telemetry (
                        device_id, source, received_at, device_timestamp,
                        distance_cm, pitch_deg, roll_deg, accel_z_ms2,
                        servo_angle, auto_active, pothole_detected, road_alert,
                        connection_status, data_quality, raw_payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_id,
                        source,
                        received_at,
                        payload.get("deviceTimestamp"),
                        tel.get("distanceCm"),
                        tel.get("pitchDeg"),
                        tel.get("rollDeg"),
                        tel.get("accelZMs2"),
                        tel.get("servoAngle"),
                        1 if tel.get("autoActive") else 0 if tel.get("autoActive") is not None else None,
                        1 if tel.get("potholeDetected") else 0 if tel.get("potholeDetected") is not None else None,
                        tel.get("roadAlert"),
                        status,
                        quality,
                        json.dumps(payload),
                    ),
                )
                conn.commit()
                self._last_saved_signature = sig
                return cursor.lastrowid
        except Exception as e:
            logger.error("Failed to persist telemetry to database: %s", e)
            return None

    def save_event(
        self,
        event_type: str,
        message: str,
        severity: str = "info",
        device_id: str = "esp32-rover-01",
        data: Optional[Dict[str, Any]] = None,
        received_at: Optional[str] = None,
    ) -> Optional[int]:
        """Persists an event (e.g. pothole alert transition, connection state change)."""
        if received_at is None:
            received_at = datetime.now(timezone.utc).isoformat()
        try:
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO events (device_id, received_at, event_type, severity, message, data)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_id,
                        received_at,
                        event_type,
                        severity,
                        message,
                        json.dumps(data or {}),
                    ),
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error("Failed to persist event to database: %s", e)
            return None

    def get_latest(self, device_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Retrieves the most recent telemetry record."""
        try:
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                if device_id:
                    cursor.execute(
                        "SELECT * FROM telemetry WHERE device_id = ? ORDER BY id DESC LIMIT 1",
                        (device_id,),
                    )
                else:
                    cursor.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                if row:
                    return self._row_to_telemetry(row)
                return None
        except Exception as e:
            logger.error("Failed to query latest telemetry: %s", e)
            return None

    def get_history(
        self,
        limit: int = 100,
        since: Optional[str] = None,
        until: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieves historical telemetry records with bounds and pagination."""
        limit = max(1, min(1000, limit))
        clauses = []
        params = []

        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if since:
            clauses.append("received_at >= ?")
            params.append(since)
        if until:
            clauses.append("received_at <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM telemetry {where} ORDER BY received_at DESC LIMIT ?"
        params.append(limit)

        try:
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                rows = cursor.fetchall()
                # Return in chronological order
                results = [self._row_to_telemetry(r) for r in reversed(rows)]
                return results
        except Exception as e:
            logger.error("Failed to query historical telemetry: %s", e)
            return []

    def get_events(
        self,
        limit: int = 50,
        since: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieves transition events from the database."""
        limit = max(1, min(500, limit))
        clauses = []
        params = []

        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if since:
            clauses.append("received_at >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM events {where} ORDER BY received_at DESC LIMIT ?"
        params.append(limit)

        try:
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                rows = cursor.fetchall()
                results = []
                for r in reversed(rows):
                    data_obj = {}
                    if r["data"]:
                        try:
                            data_obj = json.loads(r["data"])
                        except Exception:
                            pass
                    results.append({
                        "id": r["id"],
                        "deviceId": r["device_id"],
                        "receivedAt": r["received_at"],
                        "eventType": r["event_type"],
                        "severity": r["severity"],
                        "message": r["message"],
                        "data": data_obj,
                    })
                return results
        except Exception as e:
            logger.error("Failed to query events: %s", e)
            return []

    def _row_to_telemetry(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Converts an SQLite row to the standardized telemetry dictionary."""
        auto_active = None
        if row["auto_active"] is not None:
            auto_active = bool(row["auto_active"] == 1)
        pothole_detected = None
        if row["pothole_detected"] is not None:
            pothole_detected = bool(row["pothole_detected"] == 1)

        return {
            "id": row["id"],
            "deviceId": row["device_id"],
            "source": row["source"],
            "receivedAt": row["received_at"],
            "deviceTimestamp": row["device_timestamp"],
            "connection": {
                "status": row["connection_status"],
            },
            "telemetry": {
                "distanceCm": row["distance_cm"],
                "pitchDeg": row["pitch_deg"],
                "rollDeg": row["roll_deg"],
                "accelZMs2": row["accel_z_ms2"],
                "servoAngle": row["servo_angle"],
                "autoActive": auto_active,
                "potholeDetected": pothole_detected,
                "roadAlert": row["road_alert"],
            },
            "dataQuality": row["data_quality"],
        }
