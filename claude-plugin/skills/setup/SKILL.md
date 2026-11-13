---
name: setup
description: Install and set up claude-tele-connect so Claude can place phone calls from the user's own Android phone. Use when the user wants to set up phone calling, when `claude-tele-connect` is not installed, or when `claude-tele-connect status` / `check` reports problems with the AI voice, adb, or the phone connection. Walks through installing the tool and adb, choosing the AI voice, connecting the phone, and a first test call.
---

# Set up claude-tele-connect

Goal: get from nothing to one successful test call. You run every command yourself; the user only does
what needs their hands: taps on the phone, pasting API keys into a local form, and approving the test
call. Tell the user in one or two lines what you did and what is next. Never ask for an API key in the
chat, and never put one on a command line.

(`claude-tele-connect setup` with no options is an interactive wizard for people at a terminal. You can't
answer its prompts, so use the commands below instead. If the user would rather do it all themselves, tell
them to run `claude-tele-connect setup` in their own terminal.)

## 1. Install the tool

Run `claude-tele-connect --version`. If it works, go to step 2. Otherwise install it, using the first that
applies:

1. `uv` exists (`uv --version`): `uv tool install git+https://github.com/CyberTron957/claude-tele-connect`
2. `pipx` exists: `pipx install git+https://github.com/CyberTron957/claude-tele-connect`
3. Neither: install `uv` first (macOS/Linux: `brew install uv` if Homebrew exists, otherwise
   `curl -LsSf https://astral.sh/uv/install.sh | sh`; Windows: `winget install --id=astral-sh.uv -e`), then
   option 1. `uv` brings its own Python.

Say what you're about to install before running a download-and-run installer. If the install says the
repository is not found or asks for credentials, the repository is private: tell the user to run
`gh auth login` (or ask the owner for access) and stop. If the command is still not found afterwards,
run `uv tool update-shell` (or `pipx ensurepath`) and use the full path, or ask the user to open a new terminal.

## 2. Follow `status`

Run `claude-tele-connect status --json`. It lists each part (`you`, `voice`, `adb`, `phone`, `audio`) with
`done`, and `next` says the step to do now with the command (`next.do`). Do that step, run `status --json`
again, and repeat until `ready` is true. The steps:

**you**: ask the user for the name the AI should say it calls for, and optionally pronouns and the country
code for numbers saved without one (e.g. 91, 1). Then
`claude-tele-connect setup --owner "<name>" [--country-code 91] [--pronouns "she/her"]`.

**voice**: ask which AI voice they want, explaining briefly:
- `gemini` (default): **free** with a free key from https://aistudio.google.com/apikey (rate-limited;
  on the free tier Google may use what is said on calls to improve its products, which enabling billing on
  the key avoids). Users with a Google Cloud project can use Vertex instead: `--gcp-project <id>`.
- `openai`: OpenAI Realtime, paid per minute.
- `grok`: Grok Voice by xAI, paid. Runs through the Pipecat extra.
- `pipeline`: their choice of speech-to-text, LLM and voice, e.g. Deepgram + Claude + ElevenLabs
  (`--stt deepgram --llm anthropic:claude-haiku-4-5 --tts elevenlabs:<voice id>`). Slower to answer than the
  speech-to-speech options, so a fast LLM matters. Runs through the Pipecat extra.
- `local`: Whisper + Ollama + Kokoro on this computer: free and private, but slower. Needs Ollama
  (https://ollama.com) and `ollama pull llama3.2`.

Save it: `claude-tele-connect setup --provider <name> [--stt ... --llm ... --tts ...]`. For `grok`, `pipeline`
and `local`, `status` lists any missing Pipecat packages with the install command; run it (for a uv install:
`uv tool install --reinstall "claude-tele-connect[pipecat] @ git+https://github.com/CyberTron957/claude-tele-connect" --with "pipecat-ai[<extras>]"`,
or the `[grok]` / `[local]` extra). Pipecat needs Python 3.11+.

**keys**: run `claude-tele-connect key` (allow it up to 10 minutes). It opens a form in the user's browser,
served only on this computer, where they paste the keys the chosen voice needs. Tell them: "A page opened in
your browser: paste your <provider> key there and press Save." It prints only which keys were saved, never
the keys. If no browser opens (remote machine), give the user the printed `http://127.0.0.1:...` address.

Then confirm the voice works: `claude-tele-connect test-voice --json` (a short live test, no phone call; it
costs a fraction of a cent on paid providers). If `ok` is false, read `error` to the user (a wrong key is the
usual cause) and redo **keys**.

**adb**: install it: macOS `brew install android-platform-tools`; Debian/Ubuntu `sudo apt install adb` (ask
before sudo); Windows `winget install --id=Google.PlatformTools -e`. Otherwise point the user to
https://developer.android.com/tools/releases/platform-tools.

**phone**: the phone needs Android 13+ and a SIM that makes calls. Ask whether they want USB (most reliable,
simplest the first time) or Wi-Fi. Give the taps for that route only, as a short numbered list:
- Developer options (once): Settings → About phone → (Software information) → tap **Build number** 7 times,
  enter the PIN.
- USB: Settings → Developer options → **USB debugging** on; plug in; tap **Allow** (tick "Always allow").
  Then `claude-tele-connect phone status --json`. If `devices` shows `unauthorized`, they haven't tapped Allow yet.
- Wi-Fi (same network): Settings → Developer options → **Wireless debugging** on → **Pair device with pairing
  code**. Ask the user to read you the IP address & port and the 6-digit code from that dialog (short-lived
  and fine to share), then `claude-tele-connect phone pair <ip:port> <code> --json`. If it says paired but no
  device, ask for the address at the top of the Wireless debugging screen and run
  `claude-tele-connect phone connect <ip:port> --json`.

**check**: `claude-tele-connect check --json`. It finds the phone's audio mode and saves it.
- `audio_mode: "phone"`: works over adb alone.
- `audio_mode: "bluetooth"`: the phone needs Bluetooth audio (Windows/Linux only). Help the user pair the phone
  with the computer in Bluetooth settings with **Calls** allowed, and see the README's "Bluetooth mode".
  Say this mode is new and has had little real-phone testing.
- `audio_mode: null` with a Bluetooth problem on macOS: say plainly this phone can't be used from a Mac.

## 3. First test call

Ask which number to use: one the user controls or an automated line, not a stranger. Then use the **call**
skill for a short test call (goal "Say hello, explain this is a test of an AI calling assistant, and say
goodbye", `--max-minutes 2`). That skill's rules apply: show the exact call and get a clear yes first.

Afterwards, tell them what the transcript shows and that setup is done. Mention that wireless debugging
turns itself off when the phone changes Wi-Fi or restarts; turning it back on is all that's needed
(`claude-tele-connect phone status` shows whether the phone is reachable).
