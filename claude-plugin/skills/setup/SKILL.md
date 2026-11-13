---
name: setup
description: Install and set up claude-tele-connect so Claude can place phone calls from the user's own Android phone. Use when the user wants to set up phone calling, when `claude-tele-connect` is not installed, or when `claude-tele-connect check` reports problems with adb, the phone connection, or Gemini. Walks through installing the tool and adb, connecting the phone, and a first test call.
---

# Set up claude-tele-connect

Goal: get from nothing to one successful test call, doing every step yourself except the few that only the
user can do. Work through the steps in order, run `claude-tele-connect check` after each fix, and tell the
user in one or two lines what you just did and what is left.

Only the user can: tap things on their phone, type a secret into the terminal, and approve a call. Never
ask the user to paste an API key into the chat.

## 1. Install the tool

Run `claude-tele-connect --version`. If it works, skip to step 2.

Otherwise install it. Use the first of these that applies:

1. `uv` exists (`uv --version`): `uv tool install git+https://github.com/CyberTron957/claude-tele-connect`
2. `pipx` exists: `pipx install git+https://github.com/CyberTron957/claude-tele-connect`
3. Neither: install `uv` first (macOS/Linux: `brew install uv` if Homebrew exists, otherwise
   `curl -LsSf https://astral.sh/uv/install.sh | sh`; Windows: `winget install --id=astral-sh.uv -e`), then
   use option 1. `uv` brings its own Python, so no separate Python install is needed.

Tell the user what you are about to install and where it comes from before running a download-and-run
installer. If the install says the repository is not found or asks for credentials, the repository is not
public yet: tell the user to run `gh auth login` (or ask the project owner for access) and stop. If
`claude-tele-connect` is still not found afterwards, run `uv tool update-shell` (or `pipx ensurepath`) and tell
the user to open a new terminal.

On Windows with a phone that fails the check (step 5), install with the Bluetooth extra instead:
`uv tool install "claude-tele-connect[bluetooth] @ git+https://github.com/CyberTron957/claude-tele-connect"`.

## 2. Install adb

Run `adb version`. If it is missing:

- macOS: `brew install android-platform-tools` (if there is no Homebrew, send the user to
  https://developer.android.com/tools/releases/platform-tools to unzip it and add it to PATH)
- Debian/Ubuntu: `sudo apt install adb` (ask before using sudo)
- Windows: `winget install --id=Google.PlatformTools -e`; if that fails, same download link as above

## 3. AI voice, its key, and the user's name

Run `claude-tele-connect check`. If the AI voice or the owner name is not set, tell the user to run
this themselves in their terminal (in Claude Code, typing `! claude-tele-connect setup`
runs it in this session):

```
claude-tele-connect setup
```

It asks which voice provider to use, that provider's API key (hidden input), the name the AI should say it
calls for, and the country code. Explain the choice briefly if the user asks:

- **Gemini** (default): free. A free key from https://aistudio.google.com/apikey is enough; the free
  tier has rate limits, and Google may use what is said on free-tier calls to improve its products
  (enabling billing on the key avoids that).
- **OpenAI Realtime**: paid per minute; needs an OpenAI API key with billing.
- **Grok Voice, ElevenLabs voices, Deepgram, Claude, Groq, or fully local and free (Whisper + Ollama +
  Kokoro)**: these run through the optional Pipecat extra (Python 3.11+). See the README section "More
  providers through Pipecat" for the install line and settings; `claude-tele-connect check` names any missing
  package or key. Mention that pipelines (speech-to-text + LLM + voice) answer more slowly than Gemini or
  OpenAI, so a fast LLM matters.

Do not run `setup` yourself: it needs the user's typing, and the key must not pass through the chat.

## 4. Connect the phone

The phone needs an Android 13+ version and a SIM that makes normal calls. Ask which they prefer if it is
not clear: **USB** is the most reliable and simplest the first time; **Wi-Fi** is handier day to day.

Give the user these taps, as a short numbered list for one route only, not both:

Developer options (needed either way): Settings → About phone → (Software information) → tap **Build
number** 7 times, enter the PIN.

USB: Settings → Developer options → turn on **USB debugging**. Plug the phone into the computer and tap
**Allow** on the phone, ticking "Always allow from this computer".

Wi-Fi (phone and computer on the same network): Settings → Developer options → **Wireless debugging** on →
**Pair device with pairing code**. Ask the user to read you the pairing IP:port and the 6-digit code from
that screen (these are short-lived and fine to share in the chat), then run `adb pair <ip:port> <code>`.
After pairing, run `adb devices`; if the phone is not listed, ask for the address shown at the top of the
Wireless debugging screen and run `adb connect <that address>`.

Confirm with `adb devices`: the phone must show as `device` (not `unauthorized` or `offline`;
unauthorized means the user has not yet tapped Allow).

## 5. Check

Run `claude-tele-connect check` and read the output to the user in plain words:

- "call audio interception: supported" and "All good." → continue to step 6.
- "NOT supported" → the phone needs Bluetooth mode, which works only from a Windows or Linux
  computer. On macOS, say plainly that this phone cannot be used from this Mac. Otherwise set
  `CLAUDE_TELE_CONNECT_AUDIO=bluetooth` in `~/.claude-tele-connect/.env`, have the user pair the phone with the
  computer over Bluetooth (with **Calls** allowed), and follow the README's "Bluetooth mode" section.
  Say that Bluetooth mode is new and has had little real-phone testing.
- Anything else under "Fix the items above" → fix it from the troubleshooting list in the call skill
  or the README, then run `check` again.

## 6. First test call

A first call should go to a number the user controls, or an automated line, not a stranger. Ask which
number to use, then use the **call** skill to place a short test call (for example: goal "Say hello, explain
this is a test of an AI calling assistant, and say goodbye", `--max-minutes 2`). That skill's rules apply:
show the user the exact call and get a clear yes first.

When the call finishes, tell them what the transcript shows, and that setup is done. Mention that wireless
debugging turns itself off when the phone changes Wi-Fi network or reboots, and that turning it back on is
all that is needed.
