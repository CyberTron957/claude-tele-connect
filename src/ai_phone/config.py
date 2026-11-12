"""Settings, read from the environment and from .env files (KEY=value lines).

Files read, first match wins per key: ./.env (current folder), then ~/.ai-phone/.env.
`ai-phone setup` writes ~/.ai-phone/.env for you.
"""

import os
from pathlib import Path

HOME = Path(os.environ.get("AI_PHONE_HOME", Path.home() / ".ai-phone"))
ENV_FILE = HOME / ".env"


def _load_env(path: Path) -> None:
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.split(" #", 1)[0]  # allow trailing comments
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(Path.cwd() / ".env")
_load_env(ENV_FILE)


def _get(name: str, default: str = "") -> str:
    return os.environ.get(f"AI_PHONE_{name}", default)


# ---- Gemini: either an API key (Google AI Studio, simplest) or a Google Cloud project (Vertex AI) ----
GEMINI_API_KEY = _get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
GCP_PROJECT = _get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GCP_LOCATION = _get("GCP_LOCATION", "us-central1")
# Vertex only: a short-lived OAuth access token instead of Application Default Credentials, for borrowed or
# shared computers where you don't want to leave a long-lived login (expires after about an hour).
ACCESS_TOKEN = _get("ACCESS_TOKEN")
MODEL = _get("MODEL", "gemini-3.8-live")
VOICE = _get("VOICE", "Kore")

# ---- You ----
# The name the AI uses when it says whose behalf it calls on.
OWNER_NAME = _get("OWNER")
# Optional, e.g. "she/her". Unset = the AI refers to you by name only, never with gendered pronouns.
OWNER_PRONOUNS = _get("OWNER_PRONOUNS")

# ---- Phone ----
# adb device, e.g. 192.168.1.20:40031 (wireless debugging) or a USB serial. Only needed when several
# phones are connected, or where Wi-Fi discovery is blocked (then the ip:port is connected directly).
ADB_DEVICE = _get("ADB_DEVICE")
# Default country code for 10-digit numbers without one (e.g. 91 for India, 1 for the US). Empty = leave as is.
COUNTRY_CODE = _get("COUNTRY_CODE")

# How call audio reaches the computer:
#   phone      the phone's call-audio interception API, over adb (no Bluetooth needed)
#   bluetooth  the computer is paired as the phone's Bluetooth hands-free device (Windows/Linux only)
# `ai-phone check` tells you which one your phone supports.
AUDIO = _get("AUDIO", "phone")
# Bluetooth mode: part of the computer's audio device name for the phone (default: the phone's Bluetooth name).
BT_DEVICE = _get("BT_DEVICE")

CALLS_DIR = Path(_get("CALLS_DIR") or HOME / "calls")


def gemini_backend() -> str:
    """'api_key', 'vertex', or '' if nothing is configured."""
    if GEMINI_API_KEY:
        return "api_key"
    if GCP_PROJECT:
        return "vertex"
    return ""
