# claude-tele-connect

Let an AI agent make phone calls from **your own Android phone and SIM**. The phone dials over adb, the
call audio reaches your computer, and an AI voice talks on the call: Gemini Live (free tier available),
OpenAI Realtime, Grok Voice, or your own mix of speech-to-text, LLM and voice (ElevenLabs, Deepgram,
Claude, Groq, ... or fully local and free). No telephony provider, no new number: people see your normal number.

```
claude-tele-connect call "Dr Mehta Clinic" --goal "Book a check-up for next Tuesday afternoon" --context "Name: Sam Lee"
```

It also comes as a **Claude Code plugin**, so you can just ask Claude: *"call the salon and move my
haircut to Saturday"*. Claude confirms the call with you, places it, and tells you what was agreed.

## How it works

- `adb` dials, hangs up and looks up contacts on the phone.
- A small program (`android/src/CallAudio.java`) runs on the phone with adb-shell permissions. It reports
  call state and uses Android's call audio interception API (Android 13+) to hear the other person and
  play the AI's voice into the call.
- On phones that don't allow that, the computer pairs with the phone as a Bluetooth hands-free kit
  instead, like a car (Windows and Linux).
- A speech-to-speech AI model does the talking: short turns, interruptions, keypad tones for phone menus,
  hanging up politely. Transcripts are saved on your computer.

## Voice providers

| Provider | Cost | Default model / voice | What you need |
|---|---|---|---|
| `gemini` (default) | **Free** on the Gemini API free tier (rate-limited); paid per minute if billing is on | `gemini-3.8-live` / Kore | Free API key from [Google AI Studio](https://aistudio.google.com/apikey), or a Vertex AI project |
| `openai` | Paid per minute of audio | `gpt-realtime-2.1` / marin | [OpenAI API key](https://platform.openai.com/api-keys) with billing |

`claude-tele-connect setup` asks which one to use; `--provider` overrides it per call. Other endpoints
that speak the OpenAI Realtime protocol (for example Azure OpenAI) work through
`CLAUDE_TELE_CONNECT_OPENAI_URL`.

### More providers through Pipecat

[Pipecat](https://github.com/pipecat-ai/pipecat) (BSD-2) already talks to dozens of AI voice services, so
claude-tele-connect uses it for everything beyond Gemini and OpenAI. It is an optional extra (Python 3.11+).

| Provider | What talks | Install |
|---|---|---|
| `grok` | Grok Voice (xAI) speech-to-speech; key in `CLAUDE_TELE_CONNECT_XAI_API_KEY` | `uv tool install "claude-tele-connect[grok] @ git+https://github.com/CyberTron957/claude-tele-connect"` |
| `pipeline` | Speech-to-text + LLM + text-to-speech that you choose | `[pipecat]` extra plus the services, see below |
| `local` | Whisper + Ollama + Kokoro on your computer: **free, nothing leaves it** | `[local]` extra, plus [Ollama](https://ollama.com) with `ollama pull llama3.2` |

`pipeline` services (pass `--stt/--llm/--tts`, or set `CLAUDE_TELE_CONNECT_STT/LLM/TTS`; add `:model`, or
`:voice` for TTS):

| Part | Choices |
|---|---|
| `--stt` | `deepgram`, `elevenlabs`, `openai`, `groq`, `whisper` (local) |
| `--llm` | `openai`, `anthropic`, `google`, `vertex`, `groq`, `grok`, `openrouter`, `ollama` (local) |
| `--tts` | `elevenlabs`, `cartesia`, `openai`, `deepgram`, `kokoro` (local), `piper` (local) |

```
uv tool install "claude-tele-connect[pipecat] @ git+https://github.com/CyberTron957/claude-tele-connect" \
    --with "pipecat-ai[deepgram,anthropic,elevenlabs]"
claude-tele-connect call "Salon" --goal "..." --provider pipeline \
    --stt deepgram --llm anthropic:claude-haiku-4-5 --tts elevenlabs:<voice id>
```

API keys go in `~/.claude-tele-connect/.env` named after the service: `CLAUDE_TELE_CONNECT_DEEPGRAM_API_KEY`,
`_ELEVENLABS_API_KEY`, `_ANTHROPIC_API_KEY`, `_GROQ_API_KEY`, `_CARTESIA_API_KEY`, `_OPENROUTER_API_KEY`,
`_XAI_API_KEY` (the plain names like `ELEVENLABS_API_KEY` work too). `claude-tele-connect check` says exactly
which package or key is missing.

**Speed:** speech-to-speech models (Gemini Live, OpenAI, Grok) answer in about a second. A pipeline adds
up the parts: in testing, local Whisper + Gemini 3.8 Flash + Kokoro took about 4-5 s from the caller
finishing to the AI speaking, most of it the LLM. Pick a fast LLM (Groq, a mini/flash/haiku model) for
pipelines. The `local` provider's speed depends on your computer.

**Free tier and privacy:** on Gemini's free tier, Google may use what is said on your calls (including the
other person's words) to improve its products. If that isn't acceptable, enable billing on the key
(paid tier) or use Vertex AI or OpenAI. The free tier's limits are shown in AI Studio for your key.

| Your phone | Audio mode | Computer |
|---|---|---|
| Passes `claude-tele-connect check` | `phone`: adb only | macOS, Windows, Linux |
| Fails the check | `bluetooth`: adb for control, Bluetooth for audio | Windows, Linux |

> **Status: early.** Tested so far: `phone` mode with a Samsung Galaxy M14 5G (Android 15) from macOS and
> Windows 11, over USB and wireless adb, in English and Telugu, with Gemini Live. The `openai` provider is
> tested against a simulated server only, not yet on a real call. Pipecat providers: `pipeline` tested in a
> simulated call (Whisper + Vertex Gemini + Kokoro); `grok`, `local` with Ollama, and the hosted
> STT/TTS services are untested so far. `bluetooth` mode is implemented and its
> audio code is tested, but it hasn't been used with a real phone yet. Reports from other phones are very
> welcome; run `claude-tele-connect check` and open an issue with the output.

## Requirements

- An **Android 13+ phone** with a SIM that makes normal calls.
- A computer with **Python 3.10+** and **adb** (Android platform-tools).
- **An AI voice**: a free Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) (or a
  Vertex AI project), or an OpenAI API key. See [Voice providers](#voice-providers).

Install adb: macOS `brew install android-platform-tools`; Ubuntu/Debian `sudo apt install adb`; Windows:
download *SDK Platform-Tools* from developer.android.com, unzip, and add the folder to `PATH`.

## Install

```
pipx install git+https://github.com/CyberTron957/claude-tele-connect
# or: pip install git+https://github.com/CyberTron957/claude-tele-connect
# Windows + bluetooth mode: pipx install "claude-tele-connect[bluetooth] @ git+https://github.com/CyberTron957/claude-tele-connect"
```

## Set up

```
claude-tele-connect setup
```

A step-by-step wizard does everything below with you, and you can stop it any time (finished steps are saved):

1. **About you**: the name the AI says it's calling for, optional pronouns, your country code.
2. **AI voice**: pick a provider (Gemini is free), enter its key (hidden), install any extra packages,
   and hear a quick voice test.
3. **Connect your phone**: installs adb if needed, then walks you through USB or Wi-Fi pairing (you type
   the pairing code shown on the phone).
4. **Phone check**: finds out whether your phone works over adb alone or needs Bluetooth.
5. **Test call** (optional) to a number you control.

Rerun one step with `claude-tele-connect setup --step you|voice|phone|check|call`. Settings live in
`~/.claude-tele-connect/.env`. For scripts, `setup --provider ... --api-key ... --owner ...` saves settings
without asking.

The sections below explain the same steps by hand.

### 1. AI voice and your name

Run `claude-tele-connect setup --step voice` and `--step you`, or set the values in `~/.claude-tele-connect/.env`
(see [Settings](#usage)).

### 2. Turn on developer options (phone)

**Settings → About phone → Software information**, tap **Build number** 7 times, enter your PIN.
(On many phones Build number is directly under About phone.)

### 3. Connect the phone over adb

USB is simplest for the first test; wireless is easier day to day.

**USB**
1. In **Settings → Developer options**, turn on **USB debugging**.
2. Plug the phone in and tap **Allow** (tick *Always allow from this computer*).
3. `adb devices` should list the phone as `device`.

**Wireless debugging** (phone and computer on the same Wi-Fi)
1. **Settings → Developer options → Wireless debugging**: turn it on.
2. Tap **Pair device with pairing code**. Keep the dialog open: it shows a 6-digit code and an IP:port.
3. On the computer: `adb pair <ip>:<port>` and type the code. This is done once per computer.
4. `adb devices` should list the phone within a few seconds. If not, `adb connect <ip>:<port>` with the
   address shown at the top of the Wireless debugging screen (not the pairing port).

Good to know: wireless debugging **changes its port** whenever it restarts, and Android **turns it off**
when the phone changes Wi-Fi network or reboots. claude-tele-connect finds the new port by itself and rides out short
drops; if wireless debugging got switched off, turn it back on. For a phone that sits on your desk, USB is
the most reliable.

### 4. Check

```
claude-tele-connect check
```

It copies the phone program to the phone, checks the AI voice settings, and tells you which audio mode your phone
needs. "call audio interception: supported" means you're done.

### 5. First call

Call a number you control, or an automated service line, before calling a person:

```
claude-tele-connect call "<contact name or number>" --goal "Say hello, explain this is a test of an AI calling assistant, and say goodbye."
```

## Use it from Claude Code

```
/plugin marketplace add CyberTron957/claude-tele-connect
/plugin install tele-connect@claude-tele-connect
```

Then say **"set up phone calling"**: the plugin's setup skill installs the tool and adb, connects the
phone, runs the check and does a test call, leaving only the phone taps and your API key to you. After
that, ask Claude to make a call. The plugin's skill makes Claude look up the contact, show you the
number, goal and details, and wait for your OK before every call. After the call it reads the transcript
and tells you the outcome. (You can also copy `claude-plugin/skills/call/` into `~/.claude/skills/`.)

Other agents can use the same command: `claude-tele-connect call ... --yes --json` prints the result and the
transcript as JSON.

## Usage

```
claude-tele-connect call <number-or-contact> --goal "what the call should achieve" [options]
claude-tele-connect lookup <name>
claude-tele-connect check
claude-tele-connect setup
```

| `call` option | Meaning |
|---|---|
| `--context "..."` | Facts the AI may share (booking name, order number, limits). It knows nothing else about you. |
| `--max-minutes N` | Hard limit with a polite wrap-up (default 5) |
| `--ring-timeout N` | Seconds to wait for an answer (default 45) |
| `--provider ...` | Which AI voice: `gemini`, `openai`, `grok`, `pipeline`, `local` (default from setup) |
| `--stt`, `--llm`, `--tts` | `pipeline`/`local`: the services to use |
| `--voice`, `--model` | Voice and model (defaults: Kore / `gemini-3.8-live`, marin / `gpt-realtime-2.1`) |
| `--audio phone\|bluetooth` | How call audio reaches the computer |
| `--record` | Also save both sides as WAV files |
| `--yes` | Skip the confirmation prompt |
| `--json` | Print the result and transcript as JSON |

Settings (in `~/.claude-tele-connect/.env`, a `.env` in the current folder, or environment variables):

| Setting | Meaning |
|---|---|
| `CLAUDE_TELE_CONNECT_PROVIDER` | `gemini`, `openai`, `grok`, `pipeline` or `local` |
| `CLAUDE_TELE_CONNECT_STT`, `_LLM`, `_TTS` | `pipeline` services, e.g. `deepgram`, `groq:llama-3.3-70b-versatile`, `elevenlabs:<voice id>` |
| `CLAUDE_TELE_CONNECT_GEMINI_API_KEY` | Gemini API key (also read from `GEMINI_API_KEY`) |
| `CLAUDE_TELE_CONNECT_GCP_PROJECT` | Use Vertex AI in this Google Cloud project instead (with `gcloud auth application-default login`) |
| `CLAUDE_TELE_CONNECT_OPENAI_API_KEY` | OpenAI API key (also read from `OPENAI_API_KEY`) |
| `CLAUDE_TELE_CONNECT_OPENAI_URL` | Realtime endpoint (default `wss://api.openai.com/v1/realtime`) |
| `CLAUDE_TELE_CONNECT_OPENAI_TRANSCRIBE_MODEL` | Model that transcribes the other person (default `gpt-4o-mini-transcribe`) |
| `CLAUDE_TELE_CONNECT_OWNER` | Your name, as the AI says it |
| `CLAUDE_TELE_CONNECT_OWNER_PRONOUNS` | Optional. Without it, the AI refers to you by name only |
| `CLAUDE_TELE_CONNECT_COUNTRY_CODE` | Country code added to 10-digit numbers saved without one |
| `CLAUDE_TELE_CONNECT_ADB_DEVICE` | Pin a phone (serial or ip:port) when several are connected, or when Wi-Fi discovery is blocked |
| `CLAUDE_TELE_CONNECT_AUDIO` | `phone` (default) or `bluetooth` |
| `CLAUDE_TELE_CONNECT_BT_DEVICE` | Bluetooth mode: part of the audio device name, if it isn't found automatically |
| `CLAUDE_TELE_CONNECT_MODEL`, `CLAUDE_TELE_CONNECT_VOICE` | Model and voice for the chosen provider |
| `CLAUDE_TELE_CONNECT_CALLS_DIR` | Where transcripts go (default `~/.claude-tele-connect/calls`) |

## Bluetooth mode

For phones that fail the check. The computer pairs with the phone like a car kit and receives the call
audio; adb still dials, hangs up and reports call state, so you need both.

While the phone is connected to the computer for calls, **every call's audio goes to the computer**,
including calls you make or take by hand. Turn off *Calls* for the computer in the phone's Bluetooth
settings when you're not using it.

**Linux** (PipeWire recommended; PulseAudio 15+ may work)
1. Pair the phone in your desktop's Bluetooth settings or with `bluetoothctl`. On the phone, make sure
   **Calls** is on for this computer.
2. Install `pactl`, `parec` and `pacat` (package `pulseaudio-utils`; they work with PipeWire).
3. PipeWire must allow the hands-free role (`hfp_hf` in `bluez5.roles`, on by default in recent versions).
   claude-tele-connect switches the phone's Bluetooth profile itself.
4. Set `CLAUDE_TELE_CONNECT_AUDIO=bluetooth`; `claude-tele-connect check` should list `bluez_input... / bluez_output...`.

**Windows 10/11** (experimental)
1. Pair the phone in **Settings → Bluetooth & devices**; on the phone, keep **Calls** on for this PC.
2. Install with the `bluetooth` extra (above) and set `CLAUDE_TELE_CONNECT_AUDIO=bluetooth`.
3. Windows names the phone's call-audio devices like `Microphone (<phone> Hands-Free HF Audio)` and may
   only show them during a call. If they aren't found by the phone's name, pass part of the name with
   `--bt-device`.
4. If Phone Link handles calls for this phone, it may take the call audio; quit it if no audio arrives.

## Responsible use

- You are responsible for every call. Only call people and businesses that would expect it.
- Calls from your number are covered by the consent, recording and unsolicited-call rules where you live
  (for example TRAI in India, TCPA in the US). Don't use this for cold calling, marketing or scams.
- The AI always says it is an AI assistant calling on your behalf. Don't remove that.
- claude-tele-connect refuses to dial emergency numbers. Call emergency services yourself.
- Transcripts in `~/.claude-tele-connect/calls` contain other people's words. Keep them private.

## Troubleshooting

| Problem | Fix |
|---|---|
| "No authorized phone found over adb" | Wireless debugging may have switched off (it does when the phone changes Wi-Fi). Turn it on, or plug in USB. |
| `adb devices` shows `offline` | Wireless debugging restarted on a new port: `adb mdns services`, then `adb connect <address>`, or just rerun claude-tele-connect. |
| Windows: phone not found over Wi-Fi | Windows Firewall may block adb's discovery. Set `CLAUDE_TELE_CONNECT_ADB_DEVICE=<ip>:<port>` (from the Wireless debugging screen), or allow adb.exe through the firewall. |
| "more than one device/emulator" | The phone is connected twice (USB and Wi-Fi). Set `CLAUDE_TELE_CONNECT_ADB_DEVICE`, or disconnect one. |
| Check says interception is NOT supported | Use Bluetooth mode on a Windows or Linux computer. |
| Bluetooth: "No Bluetooth hands-free audio for the phone" | Check *Calls* is on for the computer in the phone's Bluetooth settings, and reconnect. On Windows, try during a call or pass `--bt-device`. |
| The call connects but the AI hears nothing | In Bluetooth mode, the phone may be sending audio to other earbuds or a car; disconnect those. |

## Development

```
git clone https://github.com/CyberTron957/claude-tele-connect && cd claude-tele-connect
pip install -e .
python tests/test_bluetooth.py        # resampler and Bluetooth bridge logic, no hardware
python tests/test_voice.py            # voice providers (fake OpenAI server) and a simulated call
python tests/test_onboard.py          # setup wizard steps with scripted answers
python tests/pipecat_call_check.py    # a simulated call through a real Pipecat pipeline (macOS, needs services)
tests/linux_audio_test.sh             # Linux Bluetooth audio path in Docker, with virtual devices
```

`src/claude_tele_connect/callaudio.dex` is prebuilt. After changing `android/src/CallAudio.java`, rebuild it with
`python android/build.py` (needs JDK 17+ and the Android SDK with platform 35 and build-tools 35).

## Credits

The adb context workarounds in `CallAudio.java` follow the approach used by
[scrcpy](https://github.com/Genymobile/scrcpy).

## License

MIT
