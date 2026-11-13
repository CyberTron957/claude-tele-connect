"""`claude-tele-connect setup`: an interactive, step-by-step onboarding wizard.

Every step saves what it learned to ~/.claude-tele-connect/.env as soon as it finishes, so setup can be
stopped and resumed, and `claude-tele-connect setup --step <name>` reruns a single step.
"""

import asyncio
import getpass
import importlib
import os
import platform
import re
import shutil
import subprocess
import sys
import time

from . import config, phone, voice

STEPS = [("you", "About you"), ("voice", "AI voice"), ("phone", "Connect your phone"),
         ("check", "Phone check"), ("call", "Test call")]

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def ok(s): print(f"  {_c('32', '✓')} {s}")
def bad(s): print(f"  {_c('31', '✗')} {s}")
def note(s): print(f"  {_c('2', s)}")
def say(s=""): print(f"  {s}" if s else "")


class Quit(Exception):
    pass


def ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    try:
        v = input(f"  {prompt}{shown}: ").strip()
    except EOFError:
        raise Quit()
    return v or default


def ask_secret(prompt: str, current: str = "") -> str:
    hint = " (Enter keeps the saved one)" if current else ""
    try:
        v = getpass.getpass(f"  {prompt}{hint}, input hidden: ").strip()
    except EOFError:
        raise Quit()
    return v or current


def yes(prompt: str, default: bool = True) -> bool:
    v = ask(f"{prompt} {'[Y/n]' if default else '[y/N]'}").lower()
    return default if not v else v.startswith("y")


def choose(prompt: str, options: list[tuple[str, str]], default: int = 1) -> str:
    """options: (value, label). Returns the chosen value."""
    for i, (_, label) in enumerate(options, 1):
        say(f"{_c('1', str(i))}) {label}")
    while True:
        v = ask(prompt, str(default))
        if v.isdigit() and 1 <= int(v) <= len(options):
            return options[int(v) - 1][0]
        for value, _ in options:
            if v.lower() == value:
                return value
        say("Type one of the numbers above.")


def save(values: dict) -> None:
    """Write settings to the .env file and apply them to this process right away."""
    values = {k: v for k, v in values.items() if v is not None}
    if not values:
        return
    config.HOME.mkdir(parents=True, exist_ok=True)
    lines = config.ENV_FILE.read_text(encoding="utf-8").splitlines() if config.ENV_FILE.exists() else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in values]
    lines += [f"{k}={v}" for k, v in values.items() if v != ""]
    config.ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        config.ENV_FILE.chmod(0o600)
    for k, v in values.items():
        if v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(config)


def key_name(short: str) -> str:
    return f"CLAUDE_TELE_CONNECT_{short}"


def current(short: str) -> str:
    return os.environ.get(key_name(short), "")


def run(cmd: list[str], **kw) -> int:
    say(_c("2", "$ " + " ".join(cmd)))
    try:
        return subprocess.run(cmd, **kw).returncode
    except FileNotFoundError:
        return 127


# ------------------------------------------------------------------------------------------- AI voice

PROVIDER_MENU = [
    ("gemini", "Gemini Live (Google)    free: a free API key from aistudio.google.com/apikey"),
    ("openai", "OpenAI Realtime         paid per minute, OpenAI API key"),
    ("grok", "Grok Voice (xAI)        paid, xAI API key (installs the Pipecat extra)"),
    ("pipeline", "Mix and match           pick speech-to-text, LLM and voice (ElevenLabs, Deepgram, Claude, ...)"),
    ("local", "Fully local             free, nothing leaves this computer; slower (Whisper + Ollama + Kokoro)"),
]

SERVICE_LABELS = {
    "stt": {"deepgram": "Deepgram", "elevenlabs": "ElevenLabs Scribe", "openai": "OpenAI", "groq": "Groq Whisper",
            "whisper": "Whisper on this computer (free)"},
    "llm": {"openai": "OpenAI (GPT)", "anthropic": "Anthropic (Claude)", "google": "Google Gemini (API key)",
            "vertex": "Google Gemini (Vertex AI project)", "groq": "Groq (fast)", "grok": "xAI Grok",
            "openrouter": "OpenRouter (many models)", "ollama": "Ollama on this computer (free)"},
    "tts": {"elevenlabs": "ElevenLabs", "cartesia": "Cartesia", "openai": "OpenAI", "deepgram": "Deepgram Aura",
            "kokoro": "Kokoro on this computer (free)", "piper": "Piper on this computer (free)"},
}
VALUE_HINT = {"stt": "model", "llm": "model", "tts": "voice"}


def step_voice() -> bool:
    prov = voice.default_provider() if (current("PROVIDER") or config.gemini_backend() or config.OPENAI_API_KEY) else ""
    if prov:
        say(f"Current: {_c('1', prov)}")
    default = [p for p, _ in PROVIDER_MENU].index(prov) + 1 if prov in voice.PROVIDERS else 1
    provider = choose("Which AI voice should talk on your calls?", PROVIDER_MENU, default)
    values = {key_name("PROVIDER"): provider}

    if provider == "gemini":
        say()
        say("Gemini works with a free API key (https://aistudio.google.com/apikey), or a Google Cloud project")
        say("with Vertex AI. On the free tier, Google may use what is said on calls to improve its products;")
        say("turn on billing for the key if that isn't acceptable.")
        if yes("Use an API key? (No = Vertex AI project)", True):
            values[key_name("GEMINI_API_KEY")] = ask_secret("Gemini API key", config.GEMINI_API_KEY)
        else:
            values[key_name("GEMINI_API_KEY")] = ""   # an API key would take precedence over Vertex
            values[key_name("GCP_PROJECT")] = ask("Google Cloud project ID", config.GCP_PROJECT)
            note("Vertex uses your gcloud login: gcloud auth application-default login")
    elif provider == "openai":
        values[key_name("OPENAI_API_KEY")] = ask_secret("OpenAI API key (platform.openai.com/api-keys)",
                                                        config.OPENAI_API_KEY)
    else:
        from . import pipecat_voice as pv
        if sys.version_info < (3, 11):
            bad(f"{provider} runs through Pipecat, which needs Python 3.11+ (this is {platform.python_version()}).")
            say("Reinstall claude-tele-connect with a newer Python, e.g. uv tool install --python 3.12 ...")
            return False
        parts = {}
        if provider == "grok":
            parts["s2s"] = "grok"
        elif provider == "local":
            parts = dict(pv.LOCAL)
            say()
            say("Local mode runs Whisper (hearing), Ollama (thinking) and Kokoro (voice) on this computer.")
            say("You also need Ollama: https://ollama.com, then `ollama pull llama3.2`. Replies take a few seconds.")
        else:
            for kind, title in (("stt", "Speech-to-text (hearing the other person)"),
                                ("llm", "LLM (deciding what to say)"), ("tts", "Voice (speaking)")):
                say()
                say(_c("1", title))
                cur = getattr(config, kind.upper()).partition(":")[0]
                names = list(pv.SERVICES[kind])
                name = choose("Choose", [(n, SERVICE_LABELS[kind].get(n, n)) for n in names],
                              names.index(cur) + 1 if cur in names else 1)
                val = ask(f"{VALUE_HINT[kind].capitalize()} (Enter for the default)",
                          getattr(config, kind.upper()).partition(":")[2] if cur == name else "")
                parts[kind] = f"{name}:{val}" if val else name
                values[key_name(kind.upper())] = parts[kind]
        # API keys for the chosen services
        for kind, spec in parts.items():
            name = spec.partition(":")[0]
            key = pv.SERVICES[kind][name][2]
            if key == "GEMINI_API_KEY":
                values[key_name(key)] = ask_secret(f"Gemini API key for {name}", config.GEMINI_API_KEY)
            elif key == "OPENAI_API_KEY":
                values[key_name(key)] = ask_secret(f"OpenAI API key for {name}", config.OPENAI_API_KEY)
            elif key and key_name(key) not in values:
                values[key_name(key)] = ask_secret(f"{key.replace('_API_KEY', '').title()} API key",
                                                   pv._key(key))
            if name == "vertex" and not config.GCP_PROJECT:
                values[key_name("GCP_PROJECT")] = ask("Google Cloud project ID (Vertex AI)")
        save(values)
        values = {}
        if not install_pipecat(provider):
            return False

    save(values)
    problem = voice.configured(provider)
    if problem:
        bad(problem)
        return False
    if yes("Quick voice test? (connects and has the AI say one sentence; no phone call)", True):
        if not test_voice(provider):
            return False
    ok(f"AI voice: {provider} (saved)")
    return True


def install_pipecat(provider: str) -> bool:
    from . import pipecat_voice as pv
    v = pv.PipecatVoice(provider, model="", voice="", instructions="", tools=voice.TOOLS)
    extras = []
    for kind, spec in v.parts.items():
        if spec and spec.partition(":")[0] in pv.SERVICES[kind]:
            e = pv.needs_install(kind, spec)
            if e:
                extras += e.split(",")
    if not extras:
        return True
    pkg = f"pipecat-ai[silero,{','.join(sorted(set(extras)))}]>=1.16,<2"
    say()
    say(f"These services need extra packages: {pkg}")
    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--python", sys.executable, pkg]
    else:
        cmd = [sys.executable, "-m", "pip", "install", pkg]
    if not yes("Install them now?", True):
        say(f"Install later with: {' '.join(cmd)}")
        return False
    if run(cmd) != 0:
        bad("Install failed. Run the command above yourself, then `claude-tele-connect setup --step voice`.")
        return False
    importlib.invalidate_caches()
    still = [kind for kind, spec in v.parts.items() if spec and pv.needs_install(kind, spec)]
    if still:
        bad(f"Installed, but {', '.join(still)} still can't load. Restart setup: claude-tele-connect setup --step voice")
        return False
    ok("Packages installed")
    return True


def run_voice_test(provider: str):
    """Connect the AI voice, have it say one sentence, return (total seconds, audio seconds, text)."""
    from .agent import system_prompt

    async def go():
        v = voice.make_voice(provider, model=config.MODEL, voice=config.VOICE,
                             instructions=system_prompt("This is a setup test. Say one short friendly sentence.",
                                                        "", config.OWNER_NAME or "the user", "outbound"))
        t0 = time.time()
        await v.connect()
        await v.send_note("(System: say one short sentence to confirm you can be heard.)")
        audio, text = 0, ""
        async def collect():
            nonlocal audio, text
            async for ev in v.events():
                if ev[0] == "audio":
                    audio += len(ev[1])
                elif ev[0] == "ai_text":
                    text += ev[1]
                elif ev[0] == "turn_complete" and audio:
                    return
                elif ev[0] == "fatal":
                    raise RuntimeError(ev[1])
        try:
            await asyncio.wait_for(collect(), 60)
        finally:
            await v.close()
        return time.time() - t0, audio / 48000, text.strip()

    return asyncio.run(go())


def test_voice(provider: str) -> bool:
    say("Testing...")
    try:
        secs, audio_s, text = run_voice_test(provider)
    except Exception as e:
        bad(f"Voice test failed: {str(e)[:300]}")
        say("Check the key and model, then run: claude-tele-connect setup --step voice")
        return False
    ok(f"The AI spoke {audio_s:.1f}s of audio ({secs:.1f}s total){': ' + repr(text[:100]) if text else ''}")
    return True


# ------------------------------------------------------------------------------------------- about you

def step_you() -> bool:
    say("The AI introduces itself as your assistant, e.g. \"Hello, I'm Sam's AI assistant.\"")
    name = ""
    while not name:
        name = ask("Your name, as the AI should say it", config.OWNER_NAME)
    pron = ask("Your pronouns, optional (e.g. she/her). Empty = the AI uses your name only", config.OWNER_PRONOUNS)
    say("Contacts saved without a country code get this one, e.g. 91 for India or 1 for the US.")
    cc = ask("Country code (optional)", config.COUNTRY_CODE).lstrip("+")
    save({key_name("OWNER"): name, key_name("OWNER_PRONOUNS"): pron, key_name("COUNTRY_CODE"): cc})
    ok(f"Calls will be made on behalf of {name}")
    return True


# ------------------------------------------------------------------------------------------- step 3

def install_adb() -> bool:
    system = platform.system()
    if system == "Darwin" and shutil.which("brew"):
        cmd = ["brew", "install", "android-platform-tools"]
    elif system == "Windows" and shutil.which("winget"):
        cmd = ["winget", "install", "--id=Google.PlatformTools", "-e"]
    elif system == "Linux" and shutil.which("apt"):
        cmd = ["sudo", "apt", "install", "-y", "adb"]
    else:
        cmd = None
    bad("adb (Android platform-tools) isn't installed.")
    if cmd and yes(f"Install it now with `{' '.join(cmd)}`?", True) and run(cmd) == 0 and shutil.which("adb"):
        ok("adb installed")
        return True
    say("Download SDK Platform-Tools from https://developer.android.com/tools/releases/platform-tools,")
    say("unzip it, add the folder to PATH, open a new terminal and run: claude-tele-connect setup --step phone")
    if system == "Windows" and cmd:
        note("(winget installs may need a new terminal before adb is found)")
    return False


def devices() -> dict:
    out = phone._host("devices")
    return {r[0]: r[1] for r in (l.split() for l in out.splitlines()[1:]) if len(r) > 1}


def wait_for_phone(seconds: float) -> str:
    end = time.time() + seconds
    shown = set()
    while time.time() < end:
        devs = devices()
        online = [d for d, s in devs.items() if s == "device"]
        if online:
            try:
                return phone.ensure_device(wait=2)
            except phone.PhoneError:
                pass
        for d, s in devs.items():
            if s == "unauthorized" and d not in shown:
                say("The phone is connected but not authorized yet: tap Allow on the phone "
                    "(tick \"Always allow from this computer\").")
                shown.add(d)
        time.sleep(1)
    return ""


def phone_model() -> str:
    try:
        return phone.adb("shell", "getprop", "ro.product.model").strip()
    except phone.PhoneError:
        return ""


def step_phone() -> bool:
    if not shutil.which("adb") and not install_adb():
        return False
    try:
        serial = phone.ensure_device(wait=3)
        ok(f"Phone connected: {phone_model() or serial} ({serial})")
        return True
    except phone.PhoneError:
        pass
    say("First, turn on Developer options on the phone (once):")
    say("  Settings -> About phone -> (Software information) -> tap Build number 7 times, enter your PIN.")
    say()
    how = choose("How should the computer reach the phone?", [
        ("usb", "USB cable      most reliable, simplest the first time"),
        ("wifi", "Wi-Fi          wireless debugging, same Wi-Fi network as this computer")])
    if how == "usb":
        say("On the phone: Settings -> Developer options -> turn on USB debugging.")
        say("Plug the phone into this computer and tap Allow on the phone.")
        ask("Press Enter when it's plugged in")
        serial = wait_for_phone(60)
    else:
        say("On the phone: Settings -> Developer options -> Wireless debugging -> turn it on,")
        say("then tap \"Pair device with pairing code\". Keep that screen open.")
        serial = ""
        for _ in range(3):
            addr = ask("IP address & port shown under the pairing code (e.g. 192.168.1.20:37099)")
            code = ask("6-digit pairing code")
            if not re.fullmatch(r"[\w.\-]+:\d+", addr) or not re.fullmatch(r"\d{6}", code):
                bad("That doesn't look right: the address is like 192.168.1.20:37099 and the code has 6 digits.")
                continue
            r = subprocess.run(["adb", "pair", addr, code], capture_output=True, text=True, timeout=30)
            out = (r.stdout + r.stderr).strip()
            if "Successfully paired" not in out:
                bad(f"Pairing failed: {out[:200] or 'no answer'}. The code changes each time; try again.")
                continue
            ok("Paired")
            say("Looking for the phone on the network...")
            serial = wait_for_phone(15)
            if not serial:
                say("Not found automatically (some networks and firewalls block discovery).")
                main = ask("Address at the top of the Wireless debugging screen (IP address & port)")
                if main:
                    phone._host("connect", main, timeout=10)
                    serial = wait_for_phone(10)
                    if serial:
                        save({key_name("ADB_DEVICE"): main})
                        note("Saved this address. It changes when wireless debugging restarts; "
                             "rerun `claude-tele-connect setup --step phone` if the phone isn't found later.")
            break
    if not serial:
        bad("The phone didn't show up.")
        say("Check the cable or Wi-Fi, that debugging is on and you tapped Allow, then rerun:")
        say("  claude-tele-connect setup --step phone")
        return False
    ok(f"Phone connected: {phone_model() or serial} ({serial})")
    if how == "wifi":
        note("Wireless debugging turns itself off when the phone changes Wi-Fi or restarts; turn it back on then.")
    return True


# ------------------------------------------------------------------------------------------- step 4

def step_check() -> bool:
    phone.ensure_device()
    say("Copying the helper program to the phone and checking what it supports...")
    phone.push_dex()
    info = phone.probe()
    ok(f"{info['model'] or 'Phone'}, Android SDK {info['sdk']}")
    if info["sdk"] and info["sdk"] < 33:
        bad("claude-tele-connect needs Android 13 or newer.")
        return False
    if info["interceptable"]:
        ok("Call audio works directly over adb (audio mode: phone). No Bluetooth needed.")
        save({key_name("AUDIO"): "phone"})
        return True
    bad("This phone doesn't let adb reach call audio, so audio has to go over Bluetooth.")
    if platform.system() == "Darwin":
        say("Bluetooth mode needs a Windows or Linux computer (a Mac can't act as a phone's hands-free kit).")
        return False
    save({key_name("AUDIO"): "bluetooth"})
    say("Saved audio mode: bluetooth. Pair the phone with this computer in Bluetooth settings and keep")
    say("\"Calls\" allowed for it on the phone. See the README section \"Bluetooth mode\".")
    try:
        from . import bluetooth
        ok(f"Bluetooth hands-free device: {bluetooth.preflight(config.BT_DEVICE, phone.bluetooth_name())}")
    except Exception as e:
        bad(f"Bluetooth hands-free device not found yet: {e}")
        say("Pair the phone, then rerun: claude-tele-connect setup --step check")
        return False
    return True


# ------------------------------------------------------------------------------------------- step 5

def step_call() -> bool:
    say("A short test call shows everything working. Call a number you control (another phone of yours)")
    say("or an automated service line, not a stranger. The AI will say it's an AI assistant.")
    if not yes("Place a test call now?", False):
        say("Skipped. Later: claude-tele-connect call <number> --goal \"Say hello and explain this is a test.\"")
        return True
    target = ask("Number or contact name to call")
    if not target:
        return True
    from .cli import outbound
    from argparse import Namespace
    a = Namespace(target=target, goal="Say hello, explain this is a test of an AI calling assistant, and say goodbye.",
                  context="", owner=config.OWNER_NAME, voice=config.VOICE, model=config.MODEL, provider=None,
                  audio=config.AUDIO, bt_device=config.BT_DEVICE, max_minutes=2, ring_timeout=45, record=False,
                  mute_mic=False, yes=False, json=False)
    result = asyncio.run(outbound(a))
    say()
    if result.get("ended_by") in ("agent", "other side hung up"):
        ok(f"Test call done: {result.get('outcome') or result.get('ended_by')}")
    else:
        bad(f"Test call ended: {result.get('ended_by')}")
    return True


# ------------------------------------------------------------------------------------------- wizard

FUNCS = {"voice": step_voice, "you": step_you, "phone": step_phone, "check": step_check, "call": step_call}


def wizard(only: str = "") -> int:
    steps = [(k, t) for k, t in STEPS if not only or k == only]
    print()
    print(_c("1", "  claude-tele-connect setup"))
    say("Let an AI make phone calls from your Android phone. Press Ctrl+C any time; finished steps are saved.")
    results = {}
    try:
        for i, (key, title) in enumerate(steps, 1):
            print()
            print(_c("1;36", f"  Step {i} of {len(steps)} · {title}"))
            while True:
                try:
                    done = FUNCS[key]()
                except phone.PhoneError as e:
                    bad(str(e))
                    done = False
                results[key] = done
                if done or not sys.stdin.isatty():
                    break
                v = ask("Try this step again, skip it, or quit? [r/s/q]", "r").lower()
                if v.startswith("q"):
                    raise Quit()
                if v.startswith("s"):
                    break
    except (KeyboardInterrupt, Quit):
        print()
        say("Setup paused. Finished steps are saved; run `claude-tele-connect setup` to continue.")
        return 130
    print()
    if all(results.get(k) for k, _ in steps):
        print(_c("1;32", "  You're set up."))
    else:
        failed = [t for k, t in steps if not results.get(k)]
        print(_c("1;33", f"  Almost there. Still to do: {', '.join(failed)}"))
        say("Rerun one step with: claude-tele-connect setup --step <you|voice|phone|check|call>")
    say(f"Settings: {config.ENV_FILE}")
    say("Make a call:   claude-tele-connect call \"<contact or number>\" --goal \"...\"")
    say("From Claude:   /plugin marketplace add CyberTron957/claude-tele-connect")
    say("               /plugin install tele-connect@claude-tele-connect")
    return 0 if all(results.get(k) for k, _ in steps) else 1
