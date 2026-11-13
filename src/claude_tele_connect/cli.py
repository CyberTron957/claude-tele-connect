"""claude-tele-connect: let an AI agent make phone calls from your own Android phone, with Gemini Live or
OpenAI Realtime as the voice.

  claude-tele-connect setup                          choose the AI voice, save its key and your name (once)
  claude-tele-connect check                          check phone + voice, and which audio mode your phone needs
  claude-tele-connect lookup <name>                  find a contact's number on the phone
  claude-tele-connect call <number-or-contact> --goal "what the call should achieve" [--context "..."]

The phone dials over adb. Once answered, call audio reaches the AI voice through the phone's call-audio
interception API (audio mode "phone") or over Bluetooth with this computer as the phone's hands-free
device (audio mode "bluetooth", Windows/Linux). Transcripts are saved in ~/.claude-tele-connect/calls/.
"""

import argparse
import asyncio
import contextlib
import getpass
import json
import os
import platform
import sys

from . import __version__, config, phone, voice


# ---------------------------------------------------------------- setup

FREE_TIER_NOTE = ("Gemini's free tier costs nothing (with rate limits). On the free tier Google may use what is said "
                  "on calls to improve its products; enable billing on the key if that isn't acceptable.")


def setup(a) -> int:
    flags = (a.provider, a.api_key, a.openai_api_key, a.gcp_project, a.owner, a.country_code,
             a.pronouns, a.stt, a.llm, a.tts, a.audio)
    if a.step or (sys.stdin.isatty() and all(f is None for f in flags)):
        from . import onboard
        return onboard.wizard(a.step or "")
    # Options given: save exactly those (no questions). Used by scripts and by AI agents.
    from . import onboard
    k = onboard.key_name
    values = {k("PROVIDER"): a.provider, k("GEMINI_API_KEY"): a.api_key, k("OPENAI_API_KEY"): a.openai_api_key,
              k("GCP_PROJECT"): a.gcp_project, k("OWNER"): a.owner,
              k("COUNTRY_CODE"): a.country_code.lstrip("+") if a.country_code is not None else None,
              k("OWNER_PRONOUNS"): a.pronouns, k("STT"): a.stt, k("LLM"): a.llm, k("TTS"): a.tts, k("AUDIO"): a.audio}
    values = {key: v.strip() for key, v in values.items() if v is not None}
    if not values:
        print("Nothing to save. Run `claude-tele-connect setup` in a terminal for the step-by-step setup, or pass options.")
        return 1
    onboard.save(values)
    secret = ("API_KEY",)
    shown = [f"{key.removeprefix('CLAUDE_TELE_CONNECT_')}={'(hidden)' if key.endswith(secret) else v or '(cleared)'}"
             for key, v in values.items()]
    print(f"Saved to {config.ENV_FILE}: {', '.join(shown)}")
    if a.api_key:
        print(FREE_TIER_NOTE)
    print("Next: claude-tele-connect status")
    return 0


# ---------------------------------------------------------------- check

def check(a) -> int:
    """Report what works on this phone + computer, and which audio mode to use."""
    apply_pipeline_flags(a)
    ok = True
    print("Phone (adb)")
    serial = phone.ensure_device()
    phone.push_dex()
    info = phone.probe()
    print(f"  connected: {serial}  ({info['model'] or 'unknown model'}, Android SDK {info['sdk']})")
    print(f"  call audio interception: {'supported' if info['interceptable'] else 'NOT supported'}")
    bt_name = phone.bluetooth_name()
    print(f"  Bluetooth name: {bt_name or 'unknown'}")

    provider = a.provider or voice.default_provider()
    default_model, default_voice, _ = voice.PROVIDERS.get(provider, ("?", "?", ""))
    print(f"AI voice: {provider} (model {config.MODEL or default_model}, voice {config.VOICE or default_voice})")
    if provider == "openai":
        if config.OPENAI_API_KEY:
            print(f"  OpenAI API key set (paid per minute of audio){'; endpoint ' + config.OPENAI_URL if 'api.openai.com' not in config.OPENAI_URL else ''}")
        else:
            print("  no OpenAI API key: run `claude-tele-connect setup --provider openai`")
            ok = False
    elif provider == "gemini":
        backend = config.gemini_backend()
        if backend == "api_key":
            print("  Gemini API key set. Free tier: no cost, with rate limits (paid tier if billing is on for the key)")
        elif backend == "vertex" and config.ACCESS_TOKEN:
            print(f"  Vertex project {config.GCP_PROJECT}, using CLAUDE_TELE_CONNECT_ACCESS_TOKEN (expires about an hour after it was made)")
        elif backend == "vertex":
            try:
                import google.auth
                google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                print(f"  Vertex project {config.GCP_PROJECT}, credentials found")
            except Exception as e:
                print(f"  no Google credentials ({e.__class__.__name__}); run: gcloud auth application-default login")
                ok = False
        else:
            print("  not configured: run `claude-tele-connect setup` (a free Gemini API key is easiest)")
            ok = False
    elif provider in voice.PIPECAT:
        from . import pipecat_voice
        v = pipecat_voice.PipecatVoice(provider, model="", voice="", instructions="", tools=voice.TOOLS)
        print(f"  via Pipecat: {v.model or '(nothing chosen)'}")
        for p in v.problems():
            print(f"  {p}")
            ok = False
        if provider == "local":
            print("  local LLM: Ollama must be running (https://ollama.com), with the model pulled "
                  f"(`ollama pull {v.parts['llm'].partition(':')[2] or 'llama3.2'}`)")
    else:
        print(f"  {voice.configured(provider)}")
        ok = False
    if not config.OWNER_NAME:
        print("  your name is not set: run `claude-tele-connect setup` (the AI says it calls on behalf of this name)")
        ok = False

    print("Audio")
    system = platform.system()
    if info["interceptable"]:
        print("  Use audio mode 'phone' (the default). No Bluetooth needed.")
    elif system == "Darwin":
        print("  This phone needs Bluetooth mode, which only works with a Windows or Linux computer"
              " (macOS can't be a hands-free device for a phone).")
        ok = False
    else:
        print(f"  This phone needs Bluetooth mode: add CLAUDE_TELE_CONNECT_AUDIO=bluetooth to {config.ENV_FILE}")
    if (a.audio == "bluetooth" or not info["interceptable"]) and system != "Darwin":
        from . import bluetooth
        try:
            print(f"  Bluetooth hands-free device: {bluetooth.preflight(a.bt_device, bt_name)}")
        except phone.PhoneError as e:
            print(f"  Bluetooth hands-free device: {e}")
            ok = False
    print("\nAll good." if ok else "\nFix the items above, then run `claude-tele-connect check` again.")
    return 0 if ok else 1


# ---------------------------------------------------------------- lookup

def lookup(a) -> int:
    phone.ensure_device()
    matches = phone.lookup_contact(a.name)
    if a.json:
        print(json.dumps([{"name": n, "number": num} for n, num in matches], ensure_ascii=False))
    elif not matches:
        print(f"No contact matching '{a.name}'")
    else:
        for n, num in matches:
            print(f"{n}\t{num}")
    return 0 if matches else 1


# ---------------------------------------------------------------- call

async def outbound(a) -> dict:
    from . import agent

    phone.ensure_device()
    phone.push_dex()
    label, number = phone.resolve_target(a.target)
    if phone.is_emergency(number):
        raise phone.PhoneError(f"{number} looks like an emergency number. claude-tele-connect never calls emergency services; "
                               "call them yourself.")

    print(f"Call:    {label} ({number})")
    print(f"Goal:    {a.goal}")
    if a.context:
        print(f"Context: {a.context}")
    provider = a.provider or voice.default_provider()
    default_model, default_voice, _ = voice.PROVIDERS[provider]
    if provider in voice.PIPECAT:
        from . import pipecat_voice
        default_model = pipecat_voice.PipecatVoice(provider, model=a.model, voice=a.voice, instructions="",
                                                   tools=voice.TOOLS).model
    print(f"Voice:   {provider} {a.model or default_model} ({a.voice or default_voice or 'default voice'}) | audio={a.audio} | max {a.max_minutes} min | record={a.record}")
    if not a.yes:
        if not sys.stdin.isatty():
            raise phone.PhoneError("Not placing the call: no terminal to confirm in. Pass --yes once the user "
                                   "has approved this exact call.")
        if input("Place this call? [y/N] ").strip().lower() != "y":
            return {"outcome": None, "ended_by": "cancelled before dialing"}

    if await phone.call_state() not in (phone.CALL_IDLE, -1):
        raise phone.PhoneError("The phone is already in a call; not dialing.")

    s = agent.CallSession(label, number, a.goal, a.context, direction="outbound", owner=a.owner,
                          provider=provider, voice_name=a.voice, model=a.model, max_minutes=a.max_minutes, record=a.record,
                          mute_mic=a.mute_mic, audio=a.audio, bt_device=a.bt_device)
    try:
        print(f"Preparing ({provider} voice + {a.audio} audio bridge) before dialing...")
        await s.prepare()
        await phone.dial(number)
        print("Dialing...")
        if not await s.wait_answered(a.ring_timeout):
            await phone.hang_up()
            print("Not answered.")
            s.result["ended_by"] = "not answered"
        else:
            print("Answered — the AI is on the line. Transcript:")
            await s.converse()
    finally:
        result = await s.close()
    result["transcript"] = [{"t": round(t, 1), "speaker": spk, "text": text} for t, spk, text in s.transcript.lines]
    return result


def apply_pipeline_flags(a) -> None:
    for kind in ("stt", "llm", "tts"):
        if getattr(a, kind, None):
            setattr(config, kind.upper(), getattr(a, kind))


def call(a, parser) -> int:
    apply_pipeline_flags(a)
    missing = voice.configured(a.provider or voice.default_provider())
    if missing:
        parser.error(missing)
    if not a.owner:
        parser.error("your name isn't set: run `claude-tele-connect setup` or pass --owner")
    # In --json mode, progress goes to stderr and stdout carries only the final JSON.
    with contextlib.redirect_stdout(sys.stderr) if a.json else contextlib.nullcontext():
        result = asyncio.run(outbound(a))
    if a.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        print("\nResult:")
        for k in ("outcome", "ended_by", "duration_s", "timing", "transcript_file"):
            if k in result:
                print(f"  {k}: {result.get(k)}")
    return 0 if result.get("ended_by") not in ("not answered", "cancelled before dialing") else 2


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    try:  # Windows consoles: never crash on characters the console encoding can't show
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="claude-tele-connect", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"claude-tele-connect {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="step-by-step setup: AI voice, your name, phone connection, check, test call",
                       description="Without options: an interactive step-by-step setup. With options: save "
                                   "those settings without asking (for scripts).")
    s.add_argument("--step", choices=("you", "voice", "phone", "check", "call"), help="run only this setup step")
    s.add_argument("--provider", choices=tuple(voice.PROVIDERS), help="AI voice provider (default: ask; gemini is free)")
    s.add_argument("--api-key", help="Gemini API key from Google AI Studio")
    s.add_argument("--openai-api-key", help="OpenAI API key")
    s.add_argument("--gcp-project", help="Google Cloud project (Vertex AI) instead of an API key")
    s.add_argument("--owner", help="your name, as the AI should say it")
    s.add_argument("--country-code", help="country code for numbers saved without one, e.g. 91")
    s.add_argument("--pronouns", help="optional, e.g. she/her (empty: the AI uses your name only)")
    s.add_argument("--stt", help="pipeline: speech-to-text service")
    s.add_argument("--llm", help="pipeline: LLM service")
    s.add_argument("--tts", help="pipeline: text-to-speech service")
    s.add_argument("--audio", choices=("phone", "bluetooth"), help="audio mode (check --json finds it)")

    st = sub.add_parser("status", help="what's set up, what's missing, and the next step")
    st.add_argument("--json", action="store_true")

    ky = sub.add_parser("key", help="paste API keys into a local browser form (never through chat)")
    ky.add_argument("names", nargs="*", help="which keys, e.g. gemini, openai, xai, elevenlabs, deepgram, anthropic, "
                                             "groq (default: the ones the current AI voice needs)")
    ky.add_argument("--all", action="store_true", help="ask again even for keys that are already saved")
    ky.add_argument("--stdin", action="store_true", help="read the keys from stdin, one per line (for scripts)")
    ky.add_argument("--no-browser", action="store_true", help="only print the form's address")
    ky.add_argument("--timeout", type=float, default=600, help="seconds to wait for the form (default 600)")

    ph = sub.add_parser("phone", help="connect the phone: pair / connect over Wi-Fi, or show status")
    ph.add_argument("action", choices=("pair", "connect", "status"))
    ph.add_argument("address", nargs="?", help="ip:port from the phone's Wireless debugging screen")
    ph.add_argument("code", nargs="?", help="pair: the 6-digit pairing code")
    ph.add_argument("--json", action="store_true")

    tv = sub.add_parser("test-voice", help="short live test of the AI voice (no phone call)")
    tv.add_argument("--provider", choices=tuple(voice.PROVIDERS))
    tv.add_argument("--json", action="store_true")

    def audio_args(sp):
        sp.add_argument("--provider", choices=tuple(voice.PROVIDERS), default=None,
                        help="AI voice (default from CLAUDE_TELE_CONNECT_PROVIDER, else gemini). grok, pipeline "
                             "and local run through Pipecat (install the pipecat extra)")
        sp.add_argument("--stt", help="pipeline/local: speech-to-text service, e.g. deepgram, whisper")
        sp.add_argument("--llm", help="pipeline/local: LLM, e.g. anthropic:claude-haiku-4-5, ollama:llama3.2")
        sp.add_argument("--tts", help="pipeline/local: text-to-speech, e.g. elevenlabs:<voice id>, kokoro")
        sp.add_argument("--audio", choices=("phone", "bluetooth"), default=config.AUDIO,
                        help="how call audio reaches the computer (default from CLAUDE_TELE_CONNECT_AUDIO, else phone)")
        sp.add_argument("--bt-device", default=config.BT_DEVICE,
                        help="bluetooth mode: part of the audio device name (default: the phone's Bluetooth name)")

    c = sub.add_parser("check", help="check the setup and which audio mode your phone needs")
    audio_args(c)
    c.add_argument("--json", action="store_true", help="machine-readable result; also saves the audio mode")

    l = sub.add_parser("lookup", help="find a contact's number on the phone")
    l.add_argument("name")
    l.add_argument("--json", action="store_true")

    k = sub.add_parser("call", help="place a call")
    k.add_argument("target", help="phone number or contact name (as saved on the phone)")
    k.add_argument("--goal", required=True, help="what the call should achieve")
    k.add_argument("--context", default="", help="extra facts the AI may use (it knows nothing else about you)")
    k.add_argument("--owner", default=config.OWNER_NAME, help="whose behalf the AI calls on")
    k.add_argument("--voice", default=config.VOICE, help="voice name (default: Kore for gemini, marin for openai)")
    k.add_argument("--model", default=config.MODEL, help="model (default: gemini-3.8-live / gpt-realtime-2.1)")
    audio_args(k)
    k.add_argument("--max-minutes", type=float, default=5)
    k.add_argument("--ring-timeout", type=float, default=45)
    k.add_argument("--record", action="store_true", help="also save both sides as WAV files")
    k.add_argument("--mute-mic", action="store_true", help="phone mode: mute the phone's own microphone")
    k.add_argument("--yes", action="store_true", help="don't ask for confirmation (only after the user approved the call)")
    k.add_argument("--json", action="store_true", help="print the result and transcript as JSON (progress goes to stderr)")

    a = p.parse_args(argv)
    try:
        if a.cmd == "setup":
            return setup(a)
        if a.cmd in ("status", "key", "phone", "test-voice"):
            from . import setupcmds
            if a.cmd == "status":
                st = setupcmds.status()
                print(json.dumps(st, ensure_ascii=False)) if a.json else setupcmds.print_status(st)
                return 0 if st["ready"] else 1
            return {"key": setupcmds.cmd_key, "phone": setupcmds.cmd_phone,
                    "test-voice": setupcmds.cmd_test_voice}[a.cmd](a)
        if a.cmd == "check" and a.json:
            from . import setupcmds
            apply_pipeline_flags(a)
            return setupcmds.check_json(a)
        if a.cmd == "check":
            return check(a)
        if a.cmd == "lookup":
            return lookup(a)
        return call(a, k)
    except phone.PhoneError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if a.cmd == "call":
            asyncio.run(phone.hang_up())
            print("\nInterrupted — hung up.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
