"""HTTP client for communicating with Blynk IoT Cloud REST APIs."""

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .config import BlynkConfig, mask_token, sanitize_url

logger = logging.getLogger("blynk.client")


class BlynkAPIError(Exception):
    """Base error for Blynk API interactions."""
    def __init__(self, message: str, status_code: Optional[int] = None, safe_url: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.safe_url = safe_url


class BlynkAuthError(BlynkAPIError):
    """Raised when authentication fails (invalid or expired token)."""
    pass


class BlynkTimeoutError(BlynkAPIError):
    """Raised when a request to Blynk times out."""
    pass


class BlynkUnreachableError(BlynkAPIError):
    """Raised when the Blynk server is unreachable (DNS, socket error)."""
    pass


class BlynkClient:
    """Robust HTTPS client for Blynk IoT Cloud datastreams."""

    def __init__(self, config: Optional[BlynkConfig] = None):
        self.config = config or BlynkConfig()
        self._consecutive_failures = 0
        self._last_success_time: Optional[float] = None

    def _execute_request(self, url: str) -> str:
        """Executes an HTTPS GET request with timeouts and sanitized exception handling."""
        safe_url = sanitize_url(url)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "AutonomousRover-Backend/1.0",
                "Accept": "application/json, text/plain",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout_sec) as resp:
                data = resp.read().decode("utf-8", errors="replace")
                return data
        except urllib.error.HTTPError as e:
            msg = f"Blynk HTTP {e.code} on {safe_url}: {e.reason}"
            if e.code in (400, 401, 403, 404):
                raise BlynkAuthError(f"Authentication/Request error: {msg}", status_code=e.code, safe_url=safe_url)
            raise BlynkAPIError(msg, status_code=e.code, safe_url=safe_url)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError,)):
                raise BlynkTimeoutError(f"Request timeout after {self.config.request_timeout_sec}s on {safe_url}", safe_url=safe_url)
            raise BlynkUnreachableError(f"Blynk unreachable ({e.reason}) on {safe_url}", safe_url=safe_url)
        except TimeoutError:
            raise BlynkTimeoutError(f"Request timeout after {self.config.request_timeout_sec}s on {safe_url}", safe_url=safe_url)
        except Exception as e:
            raise BlynkAPIError(f"Unexpected network error on {safe_url}: {type(e).__name__}: {e}", safe_url=safe_url)

    def check_hardware_connected(self) -> bool:
        """Checks if the physical ESP32 hardware is actively connected to Blynk Cloud.

        Blynk endpoint: /external/api/isHardwareConnected?token=...
        Returns:
            True if hardware is online, False otherwise.
        """
        if not self.config.is_configured:
            return False

        url = f"{self.config.base_url}/external/api/isHardwareConnected?token={urllib.parse.quote(self.config.auth_token)}"
        try:
            resp_text = self._execute_request(url).strip().lower()
            return resp_text == "true"
        except BlynkAuthError:
            raise
        except Exception as e:
            logger.warning("Could not check hardware connectivity: %s", e)
            return False

    def get_all_datastreams(self) -> Dict[str, Any]:
        """Fetches all datastreams for the rover from Blynk Cloud.

        Preferred endpoint: /external/api/getAll?token=...
        Fallback endpoint: /external/api/get?token=...&v0&v5&v6&v7&v8&v9&v10&v11
        """
        if not self.config.is_configured:
            raise BlynkAuthError("Blynk auth token is not configured. Set BLYNK_AUTH_TOKEN in environment or .env")

        token_encoded = urllib.parse.quote(self.config.auth_token)
        primary_url = f"{self.config.base_url}/external/api/getAll?token={token_encoded}"

        try:
            raw_text = self._execute_request(primary_url)
            parsed = json.loads(raw_text) if raw_text.strip() else {}
            if isinstance(parsed, dict):
                self._consecutive_failures = 0
                self._last_success_time = time.time()
                return parsed
            return {}
        except (BlynkTimeoutError, BlynkUnreachableError, json.JSONDecodeError) as e:
            # Try documented multi-pin fallback endpoint if getAll failed
            logger.debug("Primary getAll failed, trying multi-pin endpoint: %s", e)
            fallback_pins = ["v0", "v5", "v6", "v7", "v8", "v9", "v10", "v11"]
            query_pins = "&".join(fallback_pins)
            fallback_url = f"{self.config.base_url}/external/api/get?token={token_encoded}&{query_pins}"
            try:
                raw_text = self._execute_request(fallback_url)
                parsed = json.loads(raw_text) if raw_text.strip() else {}
                if isinstance(parsed, dict):
                    self._consecutive_failures = 0
                    self._last_success_time = time.time()
                    return parsed
                return {}
            except Exception:
                self._consecutive_failures += 1
                raise e

    def fetch_with_retry(self, max_retries: int = 2) -> Tuple[Dict[str, Any], bool]:
        """Fetches datastreams and hardware status with controlled exponential backoff for transient errors.

        Returns:
            Tuple of (datastreams_dict, hardware_is_connected).
        """
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                # 1. Fetch datastreams
                pins = self.get_all_datastreams()
                # 2. Check hardware status
                try:
                    hw_connected = self.check_hardware_connected()
                except Exception:
                    hw_connected = False
                return pins, hw_connected
            except BlynkAuthError:
                # Do not retry permanent auth errors
                raise
            except (BlynkTimeoutError, BlynkUnreachableError, BlynkAPIError) as e:
                last_exc = e
                if attempt < max_retries:
                    # Exponential backoff with jitter (e.g. 0.3s, 0.7s)
                    backoff = (0.3 * (2 ** attempt)) + (random.random() * 0.2)
                    time.sleep(backoff)

        if last_exc:
            raise last_exc
        return {}, False
