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
    flags = (a.provider, a.api_key, a.openai_api_key, a.gcp_project, a.owner, a.country_code)
    if a.step or (sys.stdin.isatty() and all(f is None for f in flags)):
        from . import onboard
        return onboard.wizard(a.step or "")
    values = {}
    interactive = sys.stdin.isatty()
    provider = a.provider
    if provider is None and interactive and not (a.api_key or a.gcp_project or a.openai_api_key):
        print("Which AI voice should talk on your calls?")
        print("  1) Gemini Live (Google)  - free: just needs a free API key from aistudio.google.com/apikey")
        print("  2) OpenAI Realtime       - paid: needs an OpenAI API key with billing")
        provider = {"2": "openai", "openai": "openai"}.get(input("Choose 1 or 2 [1]: ").strip().lower(), "gemini")
    provider = provider or ("openai" if a.openai_api_key else "gemini")
    values["CLAUDE_TELE_CONNECT_PROVIDER"] = provider

    if provider == "openai":
        key = a.openai_api_key
        if key is None and interactive:
            print("OpenAI API key from https://platform.openai.com/api-keys")
            key = getpass.getpass("OpenAI API key (hidden): ").strip()
        if key:
            values["CLAUDE_TELE_CONNECT_OPENAI_API_KEY"] = key
        elif not config.OPENAI_API_KEY:
            print("No OpenAI key given; calls won't work until you add one (run setup again).")
    else:
        key = a.api_key
        if key is None and a.gcp_project is None and interactive:
            print("Gemini access. Easiest: a free API key from https://aistudio.google.com/apikey")
            print("(Leave empty to use a Google Cloud project with Vertex AI instead.)")
            key = getpass.getpass("Gemini API key (hidden): ").strip()
        if key:
            values["CLAUDE_TELE_CONNECT_GEMINI_API_KEY"] = key
            print(FREE_TIER_NOTE)
        project = a.gcp_project
        if not key and project is None and interactive:
            project = input("Google Cloud project ID (Vertex AI, uses `gcloud auth application-default login`): ").strip()
        if project:
            values["CLAUDE_TELE_CONNECT_GCP_PROJECT"] = project
    owner = a.owner
    if owner is None and interactive:
        owner = input("Your name (the AI says it's calling on behalf of this name): ").strip()
    if owner:
        values["CLAUDE_TELE_CONNECT_OWNER"] = owner
    cc = a.country_code
    if cc is None and interactive:
        cc = input("Country code for numbers saved without one, e.g. 91 or 1 (optional): ").strip()
    if cc:
        values["CLAUDE_TELE_CONNECT_COUNTRY_CODE"] = cc.lstrip("+")
    if len(values) == 1 and not interactive and a.provider is None:
        print("Nothing to save. Run in a terminal, or pass --api-key / --openai-api-key / --gcp-project / --owner.")
        return 1

    config.HOME.mkdir(parents=True, exist_ok=True)
    lines = config.ENV_FILE.read_text(encoding="utf-8").splitlines() if config.ENV_FILE.exists() else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    config.ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        config.ENV_FILE.chmod(0o600)
    print(f"Saved {', '.join(values)} to {config.ENV_FILE}. Next: claude-tele-connect check")
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
