#!/usr/bin/env python3
"""Dashboard server: relays rover telemetry to a browser (standard library only).

    python3 host/dashboard/server.py                   auto-detects Blynk hardware or simulator
    python3 host/dashboard/server.py --blynk            start in HARDWARE (Blynk IoT Cloud) mode
    python3 host/dashboard/server.py --sim              start in SIMULATOR mode (host/build/bin/sim live)
    python3 host/dashboard/server.py --sim --speed 2    ...running twice as fast as real time
    python3 host/dashboard/server.py --serial /dev/cu.usbserial-0001
                                                        real cart on USB serial (115200 baud)
Then open http://localhost:8080

Browser <- GET /events           Server-Sent Events, one message per line from the active source
                                 (J,{...} snapshots, S,{...} simulator truth, event text)
Browser -> POST /cmd             one command line ("start", "maint test", "sim wall", ...)

REST Telemetry API:
Browser <- GET /api/mode               Get active mode ("hardware" or "sim")
Browser -> POST /api/mode              Switch active mode ({"mode": "hardware" | "sim"})
Browser <- GET /api/telemetry/latest   Latest normalized telemetry payload
Browser <- GET /api/telemetry/history  Historical stored telemetry records (?limit=100&since=...&until=...)
Browser <- GET /api/telemetry/status   Integration and hardware connectivity health status
Browser <- GET /api/telemetry/events   Transition detection events (?limit=50&since=...)
"""

import argparse
import http.server
import json
import logging
import os
import queue
import socketserver
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

SIM = os.path.join(HERE, "..", "build", "bin", "sim")
if not os.path.exists(SIM) and os.path.exists(SIM + ".exe"):
    SIM = SIM + ".exe"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dashboard.server")


class Hub:
    """Fan-out of cart lines to every connected browser, plus a short history."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.history = []  # recent event (non-telemetry) lines for late joiners

    def publish(self, line):
        with self.lock:
            if not (line.startswith("J,") or line.startswith("S,")):
                self.history = (self.history + [line])[-200:]
            for q in list(self.clients):
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass  # slow browser: drop rather than block

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            for line in self.history:
                q.put_nowait(line)
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


class SimSource:
    def __init__(self, hub, speed):
        if not os.path.exists(SIM):
            sys.exit("Simulator not built. Run:  make -C host all")
        self.proc = subprocess.Popen(
            [SIM, "live", "--speed", str(speed)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.hub = hub
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.hub.publish(line.rstrip("\n"))
        self.hub.publish("# simulator stopped")

    def send(self, line):
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


class SerialSource:
    """Real cart on a USB serial port, configured with termios (no pyserial needed)."""

    def __init__(self, hub, device, baud=115200):
        import termios
        self.fd = os.open(device, os.O_RDWR | os.O_NOCTTY)
        attrs = termios.tcgetattr(self.fd)
        speed = getattr(termios, "B%d" % baud)
        attrs[0] = 0                                                  # iflag: raw
        attrs[1] = 0                                                  # oflag: raw
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL       # cflag: 8N1
        attrs[3] = 0                                                  # lflag: no echo, non-canonical
        attrs[4] = attrs[5] = speed
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.hub = hub
        threading.Thread(target=self._read, daemon=True).start()
        time.sleep(0.5)
        self.send("log json")

    def _read(self):
        buf = b""
        while True:
            chunk = os.read(self.fd, 4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.hub.publish(line.decode("utf-8", "replace").rstrip("\r"))

    def send(self, line):
        try:
            os.write(self.fd, (line + "\n").encode())
        except Exception:
            pass

    def close(self):
        try:
            os.close(self.fd)
        except Exception:
            pass


class BlynkSource:
    """Live hardware telemetry source connected to Blynk IoT Cloud."""

    def __init__(self, hub, config=None, storage=None):
        from host.telemetry import BlynkConfig, BlynkTelemetryService, TelemetryStorage
        self.config = config or BlynkConfig()
        self.storage = storage or TelemetryStorage(self.config.database_path)
        self.telemetry_service = BlynkTelemetryService(self.config, self.storage, hub)
        self.hub = hub
        self.telemetry_service.start()
        self.hub.publish("# blynk: Connected to Blynk IoT Cloud ingestion service")
        self.hub.publish(f"# blynk: Rover Device ID: {self.config.device_id}")

    def send(self, line):
        # Hardware motor commands are read-only by default for safety
        cmd = line.strip()
        if any(w in cmd.lower() for w in ("motor", "start", "stop", "speed", "servo")):
            logger.info("Ignoring remote actuation command '%s' in read-only telemetry mode", cmd)
            self.hub.publish("# cmd: Hardware actuation commands are read-only in telemetry mode (local ESP32 control)")
        else:
            self.hub.publish(f"# cmd: Received dashboard command '{cmd}'")

    def close(self):
        if hasattr(self, "telemetry_service"):
            self.telemetry_service.stop()


class FilteredHub:
    """Passes lines to the target hub only when its associated mode is active."""

    def __init__(self, target_hub, coordinator, active_mode_name):
        self.target_hub = target_hub
        self.coordinator = coordinator
        self.active_mode_name = active_mode_name

    def publish(self, line):
        if self.coordinator.get_mode() == self.active_mode_name:
            self.target_hub.publish(line)


class ModeCoordinator:
    """Coordinates and isolates SIMULATOR and HARDWARE sources."""

    def __init__(self, hub, default_mode="hardware", speed=1.0, config=None, storage=None):
        self.hub = hub
        self.lock = threading.Lock()
        self.speed = speed
        from host.telemetry import BlynkConfig, TelemetryStorage
        self.config = config or BlynkConfig()
        self.storage = storage or TelemetryStorage(self.config.database_path)

        self.current_mode = default_mode.lower()
        self.sim_source = None
        self.blynk_source = None
        self._init_sources()

    def _init_sources(self):
        # 1. Initialize Blynk Source if configured
        if self.config.is_configured:
            blynk_hub = FilteredHub(self.hub, self, "hardware")
            self.blynk_source = BlynkSource(blynk_hub, self.config, self.storage)

        # 2. Initialize Simulator Source if binary exists
        if os.path.exists(SIM):
            sim_hub = FilteredHub(self.hub, self, "sim")
            self.sim_source = SimSource(sim_hub, self.speed)

        # Broadcast initial mode to hub
        self.hub.publish(f"# mode: {self.current_mode}")

    def set_mode(self, mode: str) -> str:
        mode = mode.lower().strip()
        if mode not in ("sim", "hardware"):
            mode = "hardware"
        with self.lock:
            if self.current_mode != mode:
                logger.info("Switching dashboard source mode: %s -> %s", self.current_mode, mode)
                self.current_mode = mode
                self.hub.publish(f"# mode: {self.current_mode}")
                if mode == "hardware":
                    self.hub.publish("# source: hardware (Blynk IoT Cloud)")
                else:
                    self.hub.publish("# source: sim (C++ Physics Simulator)")
        return self.current_mode

    def get_mode(self) -> str:
        with self.lock:
            return self.current_mode

    @property
    def telemetry_service(self):
        if self.blynk_source:
            return getattr(self.blynk_source, "telemetry_service", None)
        return None

    def send(self, line: str):
        mode = self.get_mode()
        if mode == "sim" and self.sim_source:
            self.sim_source.send(line)
        elif mode == "hardware" and self.blynk_source:
            self.blynk_source.send(line)

    def close(self):
        if self.sim_source:
            self.sim_source.close()
        if self.blynk_source:
            self.blynk_source.close()


def make_handler(hub, source):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep terminal quiet for regular requests
            pass

        def _send_json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            clean_path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            if clean_path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif clean_path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                q = hub.subscribe()
                try:
                    while True:
                        try:
                            line = q.get(timeout=10)
                        except queue.Empty:
                            line = ""  # keep-alive comment below
                        msg = ("data: %s\n\n" % line) if line else ": ping\n\n"
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    hub.unsubscribe(q)

            elif clean_path == "/api/mode":
                active_mode = source.get_mode() if hasattr(source, "get_mode") else ("serial" if isinstance(source, SerialSource) else "sim")
                blynk_configured = bool(getattr(source, "blynk_source", None) or hasattr(source, "telemetry_service"))
                self._send_json({"mode": active_mode, "blynkConfigured": blynk_configured})

            elif clean_path == "/api/telemetry/latest":
                if hasattr(source, "telemetry_service") and source.telemetry_service:
                    data = source.telemetry_service.get_latest_normalized()
                elif hasattr(source, "storage") and source.storage:
                    data = source.storage.get_latest() or {"status": "no data"}
                else:
                    data = {"source": "sim/serial", "status": "active"}
                self._send_json(data)

            elif clean_path == "/api/telemetry/history":
                limit_val = int(query.get("limit", [100])[0])
                since_val = query.get("since", [None])[0]
                until_val = query.get("until", [None])[0]
                device_val = query.get("deviceId", [None])[0]
                storage = getattr(source, "storage", None)
                if storage:
                    records = storage.get_history(limit=limit_val, since=since_val, until=until_val, device_id=device_val)
                else:
                    records = []
                self._send_json(records)

            elif clean_path == "/api/telemetry/status":
                if hasattr(source, "telemetry_service") and source.telemetry_service:
                    data = source.telemetry_service.get_status()
                else:
                    data = {"source": "sim/serial", "status": "running"}
                if hasattr(source, "get_mode"):
                    data["activeMode"] = source.get_mode()
                self._send_json(data)

            elif clean_path == "/api/telemetry/events":
                limit_val = int(query.get("limit", [50])[0])
                since_val = query.get("since", [None])[0]
                device_val = query.get("deviceId", [None])[0]
                storage = getattr(source, "storage", None)
                if storage:
                    events = storage.get_events(limit=limit_val, since=since_val, device_id=device_val)
                else:
                    events = []
                self._send_json(events)

            else:
                self.send_error(404)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            clean_path = parsed.path

            if clean_path == "/api/mode":
                n = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(n).decode("utf-8", "replace")
                try:
                    req = json.loads(body)
                except Exception:
                    req = {}
                requested_mode = req.get("mode", "hardware")
                if hasattr(source, "set_mode"):
                    actual_mode = source.set_mode(requested_mode)
                else:
                    actual_mode = requested_mode
                self._send_json({"mode": actual_mode, "status": "ok"})
                return

            if clean_path != "/cmd":
                self.send_error(404)
                return

            n = int(self.headers.get("Content-Length", "0"))
            line = self.rfile.read(n).decode("utf-8", "replace").strip().splitlines()
            if line and len(line[0]) < 80:
                cmd = line[0].strip()
                if cmd.lower() in ("mode sim", "sim"):
                    if hasattr(source, "set_mode"):
                        source.set_mode("sim")
                elif cmd.lower() in ("mode hardware", "hardware", "mode blynk"):
                    if hasattr(source, "set_mode"):
                        source.set_mode("hardware")
                else:
                    source.send(cmd)
                    hub.publish("> " + cmd)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--blynk", action="store_true", help="start in HARDWARE (Blynk IoT Cloud) mode")
    src.add_argument("--sim", action="store_true", help="start in SIMULATOR mode")
    src.add_argument("--serial", metavar="DEVICE", help="real cart on this serial port")
    ap.add_argument("--speed", type=float, default=1.0, help="simulation speed (with --sim)")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    hub = Hub()
    from host.telemetry import BlynkConfig
    cfg = BlynkConfig()

    if args.serial:
        logger.info("Starting in USB SERIAL mode on device %s", args.serial)
        source = SerialSource(hub, args.serial)
    else:
        # Default to hardware if blynk is configured or requested; otherwise sim
        default_mode = "sim" if args.sim else ("hardware" if (args.blynk or cfg.is_configured) else "sim")
        logger.info("Starting server with ModeCoordinator (initial mode: %s, sim speed=%.1f)", default_mode, args.speed)
        source = ModeCoordinator(hub, default_mode=default_mode, speed=args.speed, config=cfg)

    httpd = Server(("0.0.0.0", args.port), make_handler(hub, source))
    print(f"Dashboard: http://localhost:{args.port}   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
    finally:
        if hasattr(source, "close"):
            source.close()


if __name__ == "__main__":
    main()
