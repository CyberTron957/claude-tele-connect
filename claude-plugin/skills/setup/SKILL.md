---
name: setup
description: Install and set up claude-tele-connect so Claude can place phone calls from the user's own Android phone. Use when the user wants to set up phone calling, when `claude-tele-connect` is not installed, or when `claude-tele-connect status` / `check` reports problems with the AI voice, adb, or the phone connection.
---

# Set up claude-tele-connect

The full instructions ship inside the tool, so they always match the installed version.

1. Run `claude-tele-connect --version`. If it fails, install the tool with the first that applies:
   - `uv tool install git+https://github.com/CyberTron957/claude-tele-connect`
   - `pipx install git+https://github.com/CyberTron957/claude-tele-connect`
   - neither exists: install `uv` first (macOS/Linux: `brew install uv` or
     `curl -LsSf https://astral.sh/uv/install.sh | sh`; Windows: `winget install --id=astral-sh.uv -e`), then the first line.
   Say what you are about to install before running a download-and-run installer. If the repository is not
   found or asks for credentials, it is private: tell the user to run `gh auth login`, and stop. If the command
   is still not found afterwards, run `uv tool update-shell` (or `pipx ensurepath`) and use the full path.
2. Run `claude-tele-connect guide setup` and follow what it prints. Never ask for an API key in the chat.
