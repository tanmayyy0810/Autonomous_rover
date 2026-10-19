#!/usr/bin/env python3
"""Live integration test and diagnostics tool for Blynk ESP32 Rover Telemetry.

Usage:
    python tools/test_blynk_live.py
    python tools/test_blynk_live.py --watch       # Live polling terminal monitor
"""

import argparse
import os
import sys
import time

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from host.telemetry import (
    BlynkClient,
    BlynkConfig,
    PIN_SPECS,
    TelemetryStorage,
    compute_fuzzy_speed_factor,
    mask_token,
    normalize_telemetry_payload,
)


def run_live_diagnostics(watch: bool = False, poll_interval: float = 2.0) -> None:
    cfg = BlynkConfig()
    print("=" * 65)
    print("      ESP32 ROVER - BLYNK CLOUD LIVE TELEMETRY DIAGNOSTICS")
    print("=" * 65)
    print(f"Cloud Server:     {cfg.base_url}")
    print(f"Auth Token:       {mask_token(cfg.auth_token)}")
    print(f"Device ID:        {cfg.device_id}")
    print(f"Database Path:    {cfg.database_path}")
    print(f"Stale Threshold:  {cfg.stale_after_ms} ms")
    print("-" * 65)

    if not cfg.is_configured:
        print("[!] ERROR: BLYNK_AUTH_TOKEN is not configured in .env or environment!")
        print("    Please set your token in .env, for example:")
        print("    BLYNK_AUTH_TOKEN=your_token_here")
        sys.exit(1)

    client = BlynkClient(cfg)
    storage = TelemetryStorage(cfg.database_path)

    iteration = 0
    try:
        while True:
            iteration += 1
            t_start = time.time()
            if watch:
                # Clear terminal in watch mode
                os.system("cls" if os.name == "nt" else "clear")
                print(f"[Live Monitor - Poll #{iteration}] (Ctrl+C to quit)")
                print("-" * 65)

            # 1. Check Hardware Online Status
            print("[1] Checking ESP32 Hardware Status...")
            try:
                hw_online = client.check_hardware_connected()
                if hw_online:
                    print("    [*] Physical ESP32 Hardware: ONLINE (connected to Blynk Cloud)")
                else:
                    print("    [!] Physical ESP32 Hardware: OFFLINE (Blynk has cached values)")
            except Exception as e:
                print(f"    [!] Error checking hardware status: {e}")
                hw_online = False

            # 2. Fetch Datastreams
            print("\n[2] Fetching Stored Datastreams from Blynk Cloud (getAll API)...")
            try:
                raw_pins = client.get_all_datastreams()
                elapsed_ms = (time.time() - t_start) * 1000
                print(f"    [*] Received response in {elapsed_ms:.1f} ms")
            except Exception as e:
                print(f"    [!] Failed to retrieve datastreams: {e}")
                if not watch:
                    sys.exit(1)
                time.sleep(poll_interval)
                continue

            # 3. Normalize & Validate
            norm = normalize_telemetry_payload(
                raw_pins,
                device_id=cfg.device_id,
                cloud_reachable=True,
                hardware_connected=hw_online,
                status="online" if hw_online else "stale",
            )
            tel = norm["telemetry"]

            print("\n[3] Normalized Hardware Telemetry Table:")
            print(f"    {'Pin':<6} {'Field':<18} {'Value':<14} {'Unit':<8} {'Description'}")
            print("    " + "-" * 60)
            for pin in ("v0", "v5", "v6", "v7", "v8", "v9", "v10", "v11"):
                spec = PIN_SPECS[pin]
                val = tel.get(spec.backend_field)
                val_str = "<absent>" if val is None else str(val)
                print(f"    {pin.upper():<6} {spec.backend_field:<18} {val_str:<14} {spec.unit:<8} {spec.description}")

            # 4. Fuzzy Speed Recommendation
            dist_val = tel.get("distanceCm")
            is_pothole = bool(tel.get("potholeDetected")) or (tel.get("roadAlert") == "POTHOLE!")
            fuzzy_u = compute_fuzzy_speed_factor(dist_val, 0.0, is_pothole)
            print("\n[4] Software Evaluation & Recommendations:")
            print(f"    Fuzzy Speed Recommendation (u): {fuzzy_u:.2f} (0=stop, 1=full speed)")
            if dist_val is not None:
                if dist_val <= 10:
                    print("    Safety Zone: EMERGENCY STOP (< 10 cm)")
                elif dist_val <= 20:
                    print("    Safety Zone: SLOW CRAWL (< 20 cm)")
                elif dist_val <= 60:
                    print("    Safety Zone: APPROACH DECELERATION (< 60 cm)")
                else:
                    print("    Safety Zone: PATH CLEAR (> 60 cm)")
            else:
                print("    Safety Zone: NO ECHO / DISTANCE ABSENT")

            # 5. Database Persistence
            row_id = storage.save_telemetry(norm)
            if row_id:
                print(f"\n[5] Database: Persisted reading as Record #{row_id}")
            else:
                print("\n[5] Database: Skipped identical static telemetry (deduplicated)")

            if not watch:
                print("\n" + "=" * 65)
                print("  STATUS: Blynk telemetry ingestion pipeline is OPERATIONAL!")
                if not hw_online:
                    print("  NOTE: Rover hardware is currently OFFLINE.")
                    print("        Values shown above are stored datastreams from the last session.")
                    print("        Turn on ESP32 rover to stream fresh live readings.")
                print("=" * 65)
                break

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\nDiagnostics monitor stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", action="store_true", help="continuous live monitoring mode")
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")
    args = parser.parse_args()

    run_live_diagnostics(watch=args.watch, poll_interval=args.interval)
