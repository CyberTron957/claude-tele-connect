"""Settings, read from the environment and from .env files (KEY=value lines).

Files read, first match wins per key: ./.env (current folder), then ~/.claude-tele-connect/.env.
`claude-tele-connect setup` writes ~/.claude-tele-connect/.env for you.
"""

import os
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_TELE_CONNECT_HOME", Path.home() / ".claude-tele-connect"))
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
    return os.environ.get(f"CLAUDE_TELE_CONNECT_{name}", default)


# ---- Voice provider: "gemini" (default; free tier with an AI Studio key) or "openai" ----
# Empty = Gemini if it's configured, else OpenAI if an OpenAI key is set.
PROVIDER = _get("PROVIDER").lower()
# Model and voice for the chosen provider. Empty = the provider's default (see voice.PROVIDERS).
MODEL = _get("MODEL")
VOICE = _get("VOICE")

# ---- Gemini: either an API key (Google AI Studio, simplest) or a Google Cloud project (Vertex AI) ----
GEMINI_API_KEY = _get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
GCP_PROJECT = _get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GCP_LOCATION = _get("GCP_LOCATION", "us-central1")
# Vertex only: a short-lived OAuth access token instead of Application Default Credentials, for borrowed or
# shared computers where you don't want to leave a long-lived login (expires after about an hour).
ACCESS_TOKEN = _get("ACCESS_TOKEN")

# ---- OpenAI Realtime ----
OPENAI_API_KEY = _get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
# Another endpoint with the same protocol (e.g. Azure OpenAI) can go here.
OPENAI_URL = _get("OPENAI_URL", "wss://api.openai.com/v1/realtime")
OPENAI_TRANSCRIBE_MODEL = _get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")

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
# `claude-tele-connect check` tells you which one your phone supports.
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
