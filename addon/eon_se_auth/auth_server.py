"""
E.ON Sweden Auth Add-on — HTTP server.

Exposes a minimal REST API that the E.ON Sweden HA integration calls
to perform browser-based HAAPI authentication without needing Playwright
on the HA host itself.

Endpoints:
  GET  /health  → {"status": "ok", "ready": true}
  POST /auth    → {"code": "...", "code_verifier": "..."}
                  Body (JSON, all optional — falls back to env/options.json):
                    {"username": "...", "password": "..."}

The server serialises auth requests — only one Chromium instance runs at a time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_level = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
_LOG = logging.getLogger("eon_auth")

# ---------------------------------------------------------------------------
# Load add-on options (HA writes these to /data/options.json)
# ---------------------------------------------------------------------------
OPTIONS_PATH = Path("/data/options.json")

def _load_options() -> dict:
    if OPTIONS_PATH.exists():
        try:
            return json.loads(OPTIONS_PATH.read_text())
        except Exception as e:
            _LOG.warning("Could not read options.json: %s", e)
    return {}

_options = _load_options()

DEFAULT_USERNAME = os.environ.get("EON_PERSONNUMMER") or _options.get("personnummer", "")
DEFAULT_PASSWORD = os.environ.get("EON_PASSWORD") or _options.get("password", "")

_LOG.info("Add-on started. Configured user: %s", DEFAULT_USERNAME or "(none — must be provided per-request)")

# ---------------------------------------------------------------------------
# Auth lock — serialise browser launches
# ---------------------------------------------------------------------------
_auth_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "ready": True,
        "configured": bool(DEFAULT_USERNAME),
    })


@app.post("/auth")
def auth():
    """Run haapi_browser_auth.py and return the auth code + verifier."""
    body = request.get_json(silent=True) or {}
    username = body.get("username") or DEFAULT_USERNAME
    password = body.get("password") or DEFAULT_PASSWORD

    if not username or not password:
        return jsonify({"error": "personnummer and password required"}), 400

    _LOG.info("Auth request for user %s", username)

    if not _auth_lock.acquire(blocking=True, timeout=120):
        return jsonify({"error": "Another auth is already in progress — try again shortly"}), 503

    try:
        script = Path(__file__).parent / "haapi_browser_auth.py"
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps({"username": username, "password": password}),
            capture_output=True,
            text=True,
            timeout=90,
        )

        if proc.stderr:
            _LOG.debug("Browser auth log:\n%s", proc.stderr[-3000:])

        if proc.returncode != 0:
            _LOG.error("Browser auth failed (exit %s): %s", proc.returncode, proc.stderr[-500:])
            return jsonify({"error": f"Browser auth failed (exit {proc.returncode})"}), 500

        output = proc.stdout.strip()
        if not output:
            return jsonify({"error": "Browser auth produced no output"}), 500

        result = json.loads(output)
        if "error" in result:
            _LOG.error("Browser auth error: %s", result["error"])
            return jsonify(result), 401

        _LOG.info("Auth successful for %s", username)
        return jsonify(result)

    except subprocess.TimeoutExpired:
        _LOG.error("Browser auth timed out after 90s")
        return jsonify({"error": "Browser auth timed out"}), 504
    except json.JSONDecodeError as e:
        _LOG.error("Invalid JSON from browser auth: %s", e)
        return jsonify({"error": "Invalid JSON from auth subprocess"}), 500
    except Exception as e:
        _LOG.exception("Unexpected error during auth")
        return jsonify({"error": str(e)}), 500
    finally:
        _auth_lock.release()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8099))
    _LOG.info("Listening on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
