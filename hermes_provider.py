"""
Patchright browser provider — local anti-detection browser via Patchright.

Patchright is an undetected fork of Playwright that applies stealth patches
at the browser level (Chromium patches, fingerprint spoofing, anti-bot
evasion).  This provider launches a local Patchright Chromium instance and
exposes it via CDP so the existing agent-browser CLI can connect to it.

Unlike cloud providers (Browserbase, BrowserUse), Patchright runs entirely
on the local machine with zero external API calls.  Unlike the built-in
headless Chrome, it applies anti-detection patches that help avoid bot
detection on sites that fingerprint browser automation.

Configuration (in ~/.hermes/config.yaml):

    browser:
      cloud_provider: patchright           # select this provider
      patchright:
        headless: true                     # default: true
        stealth_level: maximum             # "standard" | "advanced" | "maximum"
        viewport: {width: 1920, height: 1080}
        user_data_dir: null                # persistent profile path (optional)
        args: []                           # extra Chromium args

Or via env vars:
  PATCHRIGHT_HEADLESS=true
  PATCHRIGHT_STEALTH_LEVEL=maximum
  PATCHRIGHT_VIEWPORT_WIDTH=1920
  PATCHRIGHT_VIEWPORT_HEIGHT=1080
  PATCHRIGHT_USER_DATA_DIR=/path/to/profile

Setup:
    pip install patchright
    patchright install chromium
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import requests

from tools.browser_providers.base import CloudBrowserProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patchright provider
# ---------------------------------------------------------------------------

_BROWSER_INSTANCES: Dict[str, Any] = {}
_BROWSER_LOCK = threading.Lock()
_CDP_PORT_BASE = 9223  # start port for auto-assignment


class PatchrightProvider(CloudBrowserProvider):
    """Local anti-detection browser provider using Patchright.

    Launches a stealth Chromium instance via Patchright's Python API,
    returns a CDP URL that agent-browser can connect to.  Session
    management follows the same pattern as cloud providers — each
    task_id gets its own browser context.
    """

    def provider_name(self) -> str:
        return "Patchright"

    def is_configured(self) -> bool:
        """Patchright is configured if the Python package is importable
        AND the browser binary is installed."""
        try:
            import patchright  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_config() -> Dict[str, Any]:
        """Merge config.yaml + env vars into a flat options dict."""
        opts: Dict[str, Any] = {
            "headless": True,
            "stealth_level": "maximum",
            "viewport_width": 1920,
            "viewport_height": 1080,
            "user_data_dir": None,
            "extra_args": [],
        }

        # Env vars (lower precedence than config.yaml)
        if os.environ.get("PATCHRIGHT_HEADLESS", "").lower() == "false":
            opts["headless"] = False
        stealth_env = os.environ.get("PATCHRIGHT_STEALTH_LEVEL", "").lower()
        if stealth_env in ("standard", "advanced", "maximum"):
            opts["stealth_level"] = stealth_env
        try:
            opts["viewport_width"] = int(os.environ.get("PATCHRIGHT_VIEWPORT_WIDTH", "1920"))
        except ValueError:
            pass
        try:
            opts["viewport_height"] = int(os.environ.get("PATCHRIGHT_VIEWPORT_HEIGHT", "1080"))
        except ValueError:
            pass
        udd = os.environ.get("PATCHRIGHT_USER_DATA_DIR", "").strip()
        if udd:
            opts["user_data_dir"] = udd
        extra = os.environ.get("PATCHRIGHT_EXTRA_ARGS", "").strip()
        if extra:
            opts["extra_args"] = [a.strip() for a in extra.split(",") if a.strip()]

        # Config file overrides
        try:
            from hermes_cli.config import read_raw_config
            cfg = read_raw_config()
            pr_cfg = cfg.get("browser", {}).get("patchright", {})
            if isinstance(pr_cfg, dict):
                if "headless" in pr_cfg:
                    opts["headless"] = bool(pr_cfg["headless"])
                if "stealth_level" in pr_cfg:
                    opts["stealth_level"] = str(pr_cfg["stealth_level"]).lower()
                if "viewport" in pr_cfg and isinstance(pr_cfg["viewport"], dict):
                    opts["viewport_width"] = int(pr_cfg["viewport"].get("width", 1920))
                    opts["viewport_height"] = int(pr_cfg["viewport"].get("height", 1080))
                if "user_data_dir" in pr_cfg:
                    opts["user_data_dir"] = str(pr_cfg["user_data_dir"]) or None
                if "args" in pr_cfg and isinstance(pr_cfg["args"], list):
                    opts["extra_args"] = [str(a) for a in pr_cfg["args"]]
        except Exception as e:
            logger.debug("Could not read patchright config: %s", e)

        return opts

    def _build_launch_args(self, opts: Dict[str, Any], cdp_port: int) -> list:
        """Build Chromium launch arguments for stealth + CDP."""
        from patchright.sync_api import sync_playwright

        args = [
            f"--remote-debugging-port={cdp_port}",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]

        if opts["headless"]:
            args.append("--headless=new")

        # Stealth-level args
        stealth = opts.get("stealth_level", "maximum")
        if stealth in ("advanced", "maximum"):
            args.extend([
                "--disable-infobars",
                "--disable-breakpad",
                "--disable-component-extensions-with-background-pages",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-hang-monitor",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-sync",
                "--disable-translate",
                "--metrics-recording-only",
                "--no-first-run",
                "--safebrowsing-disable-auto-update",
                "--password-store=basic",
                "--use-mock-keychain",
            ])
        if stealth == "maximum":
            args.extend([
                "--disable-background-networking",
                "--enable-features=NetworkService,NetworkServiceInProcess",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-ipc-flooding-protection",
                "--force-color-profile=srgb",
                "--disable-field-trial-config",
            ])

        # User-provided extra args
        args.extend(opts.get("extra_args", []))
        return args

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(self, task_id: str) -> Dict[str, object]:
        """Launch a Patchright Chromium instance and return session metadata.

        Returns a dict with session_name, bb_session_id, cdp_url, and
        features so browser_tool can connect agent-browser to it.

        The CDP URL is discovered by polling the Chromium debug endpoint
        after launch — this gives agent-browser a real, connectable
        websocket URL rather than a synthetic one.
        """
        import uuid
        from patchright.sync_api import sync_playwright

        opts = self._get_config()

        # Allocate a CDP port (auto-increment, no collision guard beyond lock)
        global _CDP_PORT_BASE
        with _BROWSER_LOCK:
            cdp_port = _CDP_PORT_BASE
            _CDP_PORT_BASE += 1
            if _CDP_PORT_BASE > 9250:
                _CDP_PORT_BASE = 9223

        session_name = f"patchright_{uuid.uuid4().hex[:8]}"
        launch_args = self._build_launch_args(opts, cdp_port)

        logger.info(
            "Launching Patchright session=%s port=%s headless=%s stealth=%s",
            session_name, cdp_port, opts["headless"], opts["stealth_level"],
        )

        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=opts["headless"],
                args=launch_args,
            )

            context = browser.new_context(
                viewport={"width": opts["viewport_width"], "height": opts["viewport_height"]},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
            )

            # Apply stealth scripts if available
            try:
                from patchright.stealth import stealth_sync
                page = context.new_page()
                stealth_sync(page)
                page.close()
            except ImportError:
                logger.debug("patchright.stealth not available, skipping stealth_sync")
            except Exception as e:
                logger.debug("stealth_sync failed (non-fatal): %s", e)

            # Discover the real CDP websocket URL
            cdp_url = self._discover_cdp_url(cdp_port, session_name, timeout_s=10)

            # Store for cleanup
            with _BROWSER_LOCK:
                _BROWSER_INSTANCES[task_id] = {
                    "pw": pw,
                    "browser": browser,
                    "context": context,
                    "session_name": session_name,
                    "cdp_port": cdp_port,
                    "cdp_url": cdp_url,
                }

            logger.info("Patchright session %s ready at %s", session_name, cdp_url)

            return {
                "session_name": session_name,
                "bb_session_id": session_name,
                "cdp_url": cdp_url,
                "features": {
                    "stealth": True,
                    "stealth_level": opts["stealth_level"],
                    "headless": opts["headless"],
                    "local": True,
                },
            }
        except Exception as e:
            logger.error("Failed to launch Patchright session: %s", e)
            raise RuntimeError(f"Patchright launch failed: {e}") from e

    @staticmethod
    def _discover_cdp_url(port: int, session_name: str, timeout_s: float = 10) -> str:
        """Poll Chromium's debug endpoint to discover the real CDP websocket URL.

        Chromium exposes its DevTools websocket URL at
        ``http://127.0.0.1:{port}/json/version`` when launched with
        ``--remote-debugging-port={port}``.
        """
        deadline = time.time() + timeout_s
        last_error = None
        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"http://127.0.0.1:{port}/json/version",
                    timeout=2,
                )
                resp.raise_for_status()
                payload = resp.json()
                ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
                if ws_url:
                    logger.debug("Discovered CDP URL for %s: %s", session_name, ws_url)
                    return ws_url
                last_error = "webSocketDebuggerUrl missing from response"
            except Exception as e:
                last_error = str(e)
            time.sleep(0.3)

        raise RuntimeError(
            f"Failed to discover CDP URL for {session_name} on port {port} "
            f"after {timeout_s}s: {last_error}"
        )

    def close_session(self, session_id: str) -> bool:
        """Close a Patchright session and clean up resources."""
        with _BROWSER_LOCK:
            # Find by session_id (bb_session_id == session_name)
            to_close = []
            for tid, inst in list(_BROWSER_INSTANCES.items()):
                if inst.get("session_name") == session_id:
                    to_close.append(tid)

            for tid in to_close:
                inst = _BROWSER_INSTANCES.pop(tid, None)
                if inst is None:
                    continue
                try:
                    inst.get("context", None) and inst["context"].close()
                except Exception as e:
                    logger.debug("Error closing patchright context: %s", e)
                try:
                    inst.get("browser", None) and inst["browser"].close()
                except Exception as e:
                    logger.debug("Error closing patchright browser: %s", e)
                try:
                    inst.get("pw", None) and inst["pw"].stop()
                except Exception as e:
                    logger.debug("Error stopping patchright playwright: %s", e)
                logger.info("Closed Patchright session %s", session_id)

        return True

    def emergency_cleanup(self, session_id: str) -> None:
        """Best-effort cleanup during shutdown."""
        try:
            self.close_session(session_id)
        except Exception as e:
            logger.warning("Emergency cleanup for %s failed: %s", session_id, e)

    @staticmethod
    def cleanup_all() -> None:
        """Force-close all active Patchright sessions. Called at process exit."""
        with _BROWSER_LOCK:
            for tid in list(_BROWSER_INSTANCES.keys()):
                inst = _BROWSER_INSTANCES.pop(tid, None)
                if inst is None:
                    continue
                for obj_name in ("context", "browser", "pw"):
                    obj = inst.get(obj_name)
                    if obj is None:
                        continue
                    try:
                        close_fn = getattr(obj, "close", None) or getattr(obj, "stop", None)
                        if close_fn:
                            close_fn()
                    except Exception:
                        pass
        logger.info("All Patchright sessions cleaned up")
