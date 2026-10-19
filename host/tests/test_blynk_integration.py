#!/usr/bin/env python3
"""Comprehensive test suite for Blynk IoT Telemetry Integration.

Covers all 16 required test cases specified in the requirements:
1. Successful Blynk response.
2. Invalid device token.
3. Unreachable Blynk server.
4. Request timeout.
5. Missing V6.
6. Missing V5 and V7.
7. Numeric values returned as strings.
8. Invalid numeric values.
9. Empty datastream object.
10. Pothole status changes from CLEAR to POTHOLE.
11. Duplicate polling while pothole remains active.
12. Hardware offline with previously cached values.
13. Simultaneous API consumers.
14. Database unavailable.
15. Frontend and REST endpoints displaying backend-reported data.
16. Application startup and cleanup.
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import sqlite3
import threading
import time
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from host.telemetry import (
    BlynkAuthError,
    BlynkClient,
    BlynkConfig,
    BlynkTelemetryService,
    BlynkTimeoutError,
    BlynkUnreachableError,
    TelemetryStorage,
    normalize_telemetry_payload,
)


class TestBlynkIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_telemetry.db")
        self.config = BlynkConfig()
        self.config.auth_token = "TEST_TOKEN_12345678"
        self.config.database_path = self.db_path
        self.config.poll_interval_ms = 100
        self.config.stale_after_ms = 500
        self.storage = TelemetryStorage(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. Successful Blynk response
    def test_01_successful_blynk_response(self):
        sample_pins = {
            "v0": 1,
            "v5": 0,
            "v6": 35.5,
            "v7": "CLEAR",
            "v8": 2.4,
            "v9": -1.2,
            "v10": 9.75,
            "v11": 90,
        }
        with patch.object(BlynkClient, "_execute_request") as mock_req:
            mock_req.side_effect = [json.dumps(sample_pins), "true"]
            client = BlynkClient(self.config)
            pins, hw_online = client.fetch_with_retry()

            self.assertEqual(pins["v6"], 35.5)
            self.assertTrue(hw_online)

            norm = normalize_telemetry_payload(pins, device_id="test-rover", status="online")
            self.assertEqual(norm["deviceId"], "test-rover")
            self.assertEqual(norm["telemetry"]["distanceCm"], 35.5)
            self.assertEqual(norm["telemetry"]["pitchDeg"], 2.4)
            self.assertEqual(norm["telemetry"]["rollDeg"], -1.2)
            self.assertEqual(norm["telemetry"]["accelZMs2"], 9.75)
            self.assertEqual(norm["telemetry"]["servoAngle"], 90)
            self.assertTrue(norm["telemetry"]["autoActive"])
            self.assertFalse(norm["telemetry"]["potholeDetected"])
            self.assertEqual(norm["telemetry"]["roadAlert"], "CLEAR")
            self.assertEqual(norm["dataQuality"], "GOOD")

    # 2. Invalid device token
    def test_02_invalid_device_token(self):
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.side_effect = urllib.error.HTTPError(
                url="https://blynk.cloud/external/api/getAll?token=***",
                code=400,
                msg="Invalid Token",
                hdrs={},
                fp=None,
            )
            client = BlynkClient(self.config)
            with self.assertRaises(BlynkAuthError) as ctx:
                client.get_all_datastreams()
            # Verify token is never exposed in error message
            self.assertNotIn("TEST_TOKEN_12345678", str(ctx.exception))
            self.assertEqual(ctx.exception.status_code, 400)

    # 3. Unreachable Blynk server
    def test_03_unreachable_blynk_server(self):
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.side_effect = urllib.error.URLError("getaddrinfo failed: host not found")
            client = BlynkClient(self.config)
            with self.assertRaises(BlynkUnreachableError):
                client.get_all_datastreams()

    # 4. Request timeout
    def test_04_request_timeout(self):
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.side_effect = TimeoutError("Connection timed out")
            client = BlynkClient(self.config)
            with self.assertRaises(BlynkTimeoutError):
                client.get_all_datastreams()

    # 5. Missing V6 (distance)
    def test_05_missing_v6(self):
        sample_pins = {
            "v0": 0,
            "v5": 0,
            "v7": "CLEAR",
            "v8": 1.0,
            "v9": 0.0,
            "v10": 9.8,
            "v11": 90,
            # v6 is omitted intentionally
        }
        norm = normalize_telemetry_payload(sample_pins)
        # Must be None, not 0 or fabricated
        self.assertIsNone(norm["telemetry"]["distanceCm"])

    # 6. Missing V5 and V7 (pothole flags)
    def test_06_missing_v5_and_v7(self):
        sample_pins = {
            "v6": 40.0,
            "v8": 0.5,
            "v9": -0.5,
            "v10": 9.81,
            # v5 and v7 omitted
        }
        norm = normalize_telemetry_payload(sample_pins)
        self.assertIsNone(norm["telemetry"]["potholeDetected"])
        self.assertIsNone(norm["telemetry"]["roadAlert"])

    # 7. Numeric values returned as strings
    def test_07_numeric_values_returned_as_strings(self):
        sample_pins = {
            "v0": "1",
            "v5": "0",
            "v6": "45.75",
            "v7": "CLEAR",
            "v8": "15.3",
            "v9": "-3.4",
            "v10": "9.805",
            "v11": "120",
        }
        norm = normalize_telemetry_payload(sample_pins)
        self.assertTrue(norm["telemetry"]["autoActive"])
        self.assertFalse(norm["telemetry"]["potholeDetected"])
        self.assertEqual(norm["telemetry"]["distanceCm"], 45.75)
        self.assertEqual(norm["telemetry"]["pitchDeg"], 15.3)
        self.assertEqual(norm["telemetry"]["rollDeg"], -3.4)
        self.assertEqual(norm["telemetry"]["accelZMs2"], 9.805)
        self.assertEqual(norm["telemetry"]["servoAngle"], 120)

    # 8. Invalid numeric values
    def test_08_invalid_numeric_values(self):
        sample_pins = {
            "v6": "not-a-number",
            "v8": float("nan"),
            "v9": float("inf"),
            "v10": None,
            "v11": "invalid",
        }
        norm = normalize_telemetry_payload(sample_pins)
        self.assertIsNone(norm["telemetry"]["distanceCm"])
        self.assertIsNone(norm["telemetry"]["pitchDeg"])
        self.assertIsNone(norm["telemetry"]["rollDeg"])
        self.assertIsNone(norm["telemetry"]["accelZMs2"])
        self.assertIsNone(norm["telemetry"]["servoAngle"])

    # 9. Empty datastream object
    def test_09_empty_datastream_object(self):
        norm = normalize_telemetry_payload({})
        self.assertEqual(norm["dataQuality"], "EMPTY")
        self.assertIsNone(norm["telemetry"]["distanceCm"])
        self.assertIsNone(norm["telemetry"]["pitchDeg"])
        self.assertIsNone(norm["telemetry"]["autoActive"])

    # 10. Pothole status changes from CLEAR to POTHOLE
    def test_10_pothole_status_change_from_clear_to_pothole(self):
        hub = MagicMock()
        service = BlynkTelemetryService(self.config, self.storage, hub)

        # First poll: CLEAR
        with patch.object(service.client, "fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = ({"v5": 0, "v7": "CLEAR", "v8": 1.0, "v9": 0.0, "v10": 9.8, "v11": 90}, True)
            service.poll_once()

        # Second poll: POTHOLE!
        with patch.object(service.client, "fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = ({"v5": 1, "v7": "POTHOLE!", "v8": 30.0, "v9": 5.0, "v10": 15.2, "v11": 90}, True)
            service.poll_once()

        events = self.storage.get_events()
        pothole_events = [e for e in events if e["eventType"] == "POTHOLE_ALERT"]
        self.assertEqual(len(pothole_events), 1)
        self.assertIn("Pothole impact", pothole_events[0]["message"])

    # 11. Duplicate polling while pothole remains active
    def test_11_duplicate_polling_while_pothole_active(self):
        hub = MagicMock()
        service = BlynkTelemetryService(self.config, self.storage, hub)

        with patch.object(service.client, "fetch_with_retry") as mock_fetch:
            # First poll: normal
            mock_fetch.return_value = ({"v5": 0, "v7": "CLEAR"}, True)
            service.poll_once()

            # Transition to pothole
            mock_fetch.return_value = ({"v5": 1, "v7": "POTHOLE!"}, True)
            service.poll_once()

            # Subsequent polls: pothole is STILL active
            service.poll_once()
            service.poll_once()
            service.poll_once()

        events = self.storage.get_events()
        pothole_events = [e for e in events if e["eventType"] == "POTHOLE_ALERT"]
        # Transition detection must ensure exactly 1 event is recorded, not 4
        self.assertEqual(len(pothole_events), 1)

    # 12. Hardware offline with previously cached values
    def test_12_hardware_offline_with_previously_cached_values(self):
        hub = MagicMock()
        service = BlynkTelemetryService(self.config, self.storage, hub)

        # 1. Successful poll with readings
        with patch.object(service.client, "fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = ({"v6": 32.0, "v8": 5.0, "v9": -2.0, "v10": 9.8, "v11": 90}, True)
            service.poll_once()

        # 2. Hardware disconnects
        with patch.object(service.client, "fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = ({"v6": 32.0, "v8": 5.0, "v9": -2.0, "v10": 9.8, "v11": 90}, False)
            service.poll_once()

        status = service.get_status()
        self.assertFalse(status["hardwareConnected"])
        latest = service.get_latest_normalized()
        # Cached values preserved
        self.assertEqual(latest["telemetry"]["distanceCm"], 32.0)
        # But connection status reflects not fresh
        self.assertFalse(latest["connection"]["telemetryFresh"])

    # 13. Simultaneous API consumers
    def test_13_simultaneous_api_consumers(self):
        hub = MagicMock()
        service = BlynkTelemetryService(self.config, self.storage, hub)

        with patch.object(service.client, "fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = ({"v6": 50.0, "v8": 1.0, "v9": 0.0, "v10": 9.81, "v11": 90}, True)
            service.poll_once()

        errors = []

        def worker_reader():
            try:
                for _ in range(30):
                    _ = service.get_latest_normalized()
                    _ = service.get_status()
                    _ = self.storage.get_history(limit=10)
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker_reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Thread concurrency errors: {errors}")

    # 14. Database unavailable
    def test_14_database_unavailable(self):
        # Point to an invalid read-only/directory path to trigger storage failure
        invalid_storage = TelemetryStorage("host/data/invalid_dir/bad.db")
        # Simulate connection error
        with patch.object(invalid_storage, "_get_connection", side_effect=sqlite3.OperationalError("disk I/O error")):
            hub = MagicMock()
            service = BlynkTelemetryService(self.config, invalid_storage, hub)
            with patch.object(service.client, "fetch_with_retry") as mock_fetch:
                mock_fetch.return_value = ({"v6": 25.0, "v8": 0.0, "v9": 0.0, "v10": 9.81, "v11": 90}, True)
                # Must not crash!
                payload = service.poll_once()
                self.assertIsNotNone(payload)
                self.assertEqual(payload["telemetry"]["distanceCm"], 25.0)

    # 15. Frontend and REST endpoints displaying backend-reported data
    def test_15_frontend_endpoints(self):
        from host.dashboard.server import Server, make_handler, Hub, BlynkSource
        hub = Hub()
        source = BlynkSource(hub, self.config, self.storage)
        test_port = 8899

        # Seed storage with a record
        norm = normalize_telemetry_payload({"v6": 42.0, "v8": 3.0, "v9": 1.0, "v10": 9.81, "v11": 90})
        self.storage.save_telemetry(norm, force=True)

        httpd = Server(("127.0.0.1", test_port), make_handler(hub, source))
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        try:
            conn = http.client.HTTPConnection("127.0.0.1", test_port, timeout=2)

            # Test 1: GET /
            conn.request("GET", "/")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            body = res.read().decode("utf-8")
            self.assertIn("Adaptive Suspension", body)

            # Test 2: GET /api/telemetry/latest
            conn.request("GET", "/api/telemetry/latest")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode("utf-8"))
            self.assertIn("telemetry", data)

            # Test 3: GET /api/telemetry/status
            conn.request("GET", "/api/telemetry/status")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode("utf-8"))
            self.assertEqual(data["source"], "blynk")

            # Test 4: GET /api/telemetry/history
            conn.request("GET", "/api/telemetry/history?limit=5")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode("utf-8"))
            self.assertIsInstance(data, list)

            # Test 5: GET /api/telemetry/events
            conn.request("GET", "/api/telemetry/events")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode("utf-8"))
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            source.close()

    # 16. Application startup and cleanup
    def test_16_application_startup_and_cleanup(self):
        hub = MagicMock()
        service = BlynkTelemetryService(self.config, self.storage, hub)
        service.start()
        self.assertTrue(service._running)
        self.assertIsNotNone(service._thread)
        self.assertTrue(service._thread.is_alive())

        service.stop()
        self.assertFalse(service._running)
        time.sleep(0.3)
        self.assertFalse(service._thread.is_alive())

    # 17. Requirement 12 Scenarios (Offline, Online, Mode Switching, Disconnect, Reconnect)
    def test_17_requirement_12_scenarios(self):
        from host.dashboard.server import Hub, ModeCoordinator, FilteredHub
        hub = Hub()
        coordinator = ModeCoordinator(hub, default_mode="hardware", config=self.config, storage=self.storage)

        # Scenario 1: ESP32 disconnected, HARDWARE selected
        self.assertEqual(coordinator.get_mode(), "hardware")
        service = coordinator.telemetry_service
        self.assertIsNotNone(service)
        with patch.object(service.client, "fetch_with_retry") as mock_fetch:
            # Cloud reachable but hardware disconnected
            mock_fetch.return_value = ({}, False)
            with patch.object(coordinator.blynk_source.hub, "publish") as mock_pub:
                service.poll_once()
                self.assertTrue(mock_pub.called)
                last_call = mock_pub.call_args_list[-1][0][0]
                self.assertTrue(last_call.startswith("J,"))
                data = json.loads(last_call[2:])
                self.assertEqual(data["hw_online"], 0)
                self.assertEqual(data["ready"], 0)
                self.assertEqual(data["state"], "OFFLINE")
                self.assertEqual(data["conf"], 0.0)
                self.assertEqual(data["s"], 0.0)
                # Ensure no fake simulator smooth values
                self.assertEqual(data["surface"], "OFFLINE")

        # Scenario 2: ESP32 connected to Blynk, HARDWARE selected
        sample_pins = {"v0": 1, "v5": 0, "v6": 85.0, "v7": "CLEAR", "v8": 1.5, "v9": -0.8, "v10": 9.81, "v11": 108}
        with patch.object(service.client, "fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = (sample_pins, True)
            with patch.object(coordinator.blynk_source.hub, "publish") as mock_pub:
                service.poll_once()
                self.assertTrue(mock_pub.called)
                last_call = mock_pub.call_args_list[-1][0][0]
                data = json.loads(last_call[2:])
                self.assertEqual(data["hw_online"], 1)
                self.assertEqual(data["ready"], 1)
                self.assertEqual(data["state"], "RUNNING")
                self.assertEqual(data["dist"], 85.0)
                self.assertEqual(data["servo"], 108)
                self.assertEqual(data["pitch"], 1.5)
                self.assertEqual(data["roll"], -0.8)

        # Scenario 3: Switching between modes (HARDWARE -> SIMULATOR)
        coordinator.set_mode("sim")
        self.assertEqual(coordinator.get_mode(), "sim")

        # In SIMULATOR mode, FilteredHub blocks Blynk telemetry
        blynk_filtered = FilteredHub(hub, coordinator, "hardware")
        with patch.object(hub, "publish") as mock_hub_pub:
            blynk_filtered.publish("J,{\"source\":\"blynk\"}")
            self.assertFalse(mock_hub_pub.called)

        # And passes simulator telemetry
        sim_filtered = FilteredHub(hub, coordinator, "sim")
        with patch.object(hub, "publish") as mock_hub_pub:
            sim_filtered.publish("S,{\"true_surface\":\"SMOOTH\"}")
            self.assertTrue(mock_hub_pub.called)

        # Scenario 4: Switching back to HARDWARE mode
        coordinator.set_mode("hardware")
        self.assertEqual(coordinator.get_mode(), "hardware")

        with patch.object(hub, "publish") as mock_hub_pub:
            sim_filtered.publish("S,{\"true_surface\":\"SMOOTH\"}")
            self.assertFalse(mock_hub_pub.called)  # Simulator line blocked!

        # Scenario 5: Disconnecting ESP32 while HARDWARE mode is active
        with patch.object(service.client, "fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = ({}, False)
            with patch.object(coordinator.blynk_source.hub, "publish") as mock_pub:
                service.poll_once()
                self.assertTrue(mock_pub.called)
                data = json.loads(mock_pub.call_args_list[-1][0][0][2:])
                self.assertEqual(data["hw_online"], 0)
                self.assertEqual(data["state"], "OFFLINE")
                # Coordinator must NEVER automatically fall back to sim!
                self.assertEqual(coordinator.get_mode(), "hardware")

        # Scenario 6: Reconnecting ESP32
        with patch.object(service.client, "fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = (sample_pins, True)
            with patch.object(coordinator.blynk_source.hub, "publish") as mock_pub:
                service.poll_once()
                self.assertTrue(mock_pub.called)
                data = json.loads(mock_pub.call_args_list[-1][0][0][2:])
                self.assertEqual(data["hw_online"], 1)
                self.assertEqual(data["state"], "RUNNING")
                self.assertEqual(coordinator.get_mode(), "hardware")

        coordinator.close()

    # 18. Mode API Endpoints (GET /api/mode and POST /api/mode)
    def test_18_mode_api_endpoints(self):
        from host.dashboard.server import Server, make_handler, Hub, ModeCoordinator
        hub = Hub()
        coordinator = ModeCoordinator(hub, default_mode="hardware", config=self.config, storage=self.storage)
        test_port = 8898

        httpd = Server(("127.0.0.1", test_port), make_handler(hub, coordinator))
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        try:
            conn = http.client.HTTPConnection("127.0.0.1", test_port, timeout=2)

            # 1. Check initial mode
            conn.request("GET", "/api/mode")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode("utf-8"))
            self.assertEqual(data["mode"], "hardware")
            self.assertTrue(data["blynkConfigured"])

            # 2. Switch to SIMULATOR mode via POST /api/mode
            headers = {"Content-Type": "application/json"}
            conn.request("POST", "/api/mode", body=json.dumps({"mode": "sim"}), headers=headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode("utf-8"))
            self.assertEqual(data["mode"], "sim")
            self.assertEqual(coordinator.get_mode(), "sim")

            # 3. Switch back to HARDWARE mode via POST /api/mode
            conn.request("POST", "/api/mode", body=json.dumps({"mode": "hardware"}), headers=headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode("utf-8"))
            self.assertEqual(data["mode"], "hardware")
            self.assertEqual(coordinator.get_mode(), "hardware")

            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            coordinator.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
