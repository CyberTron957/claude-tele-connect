"""Non-interactive setup commands, so an AI agent (or a script) can do every onboarding step:

  claude-tele-connect status --json              what's done, what's missing, and the next command to run
  claude-tele-connect setup --owner ... --provider ... [--stt --llm --tts --audio ...]   non-secret settings
  claude-tele-connect key [names]                the user pastes API keys into a local browser form
  claude-tele-connect phone pair <ip:port> <code> / phone connect <ip:port> / phone status
  claude-tele-connect test-voice --json          a short live test of the AI voice (no phone call)
  claude-tele-connect check --json               phone check: model, Android version, audio mode

API keys never go through the agent: `key` opens a form served only on 127.0.0.1, and only writes what the
user typed into ~/.claude-tele-connect/.env.
"""

import html
import http.server
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

from . import config, onboard, phone, voice

# setting name (after CLAUDE_TELE_CONNECT_) -> (label, where to get one)
KEYS = {
    "GEMINI_API_KEY": ("Gemini API key", "https://aistudio.google.com/apikey"),
    "OPENAI_API_KEY": ("OpenAI API key", "https://platform.openai.com/api-keys"),
    "XAI_API_KEY": ("xAI (Grok) API key", "https://console.x.ai"),
    "ELEVENLABS_API_KEY": ("ElevenLabs API key", "https://elevenlabs.io/app/settings/api-keys"),
    "DEEPGRAM_API_KEY": ("Deepgram API key", "https://console.deepgram.com"),
    "ANTHROPIC_API_KEY": ("Anthropic API key", "https://console.anthropic.com/settings/keys"),
    "GROQ_API_KEY": ("Groq API key", "https://console.groq.com/keys"),
    "CARTESIA_API_KEY": ("Cartesia API key", "https://play.cartesia.ai/keys"),
    "OPENROUTER_API_KEY": ("OpenRouter API key", "https://openrouter.ai/keys"),
}
ALIASES = {"gemini": "GEMINI_API_KEY", "google": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "grok": "XAI_API_KEY",
           "xai": "XAI_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY", "deepgram": "DEEPGRAM_API_KEY",
           "anthropic": "ANTHROPIC_API_KEY", "claude": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY",
           "cartesia": "CARTESIA_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


def key_value(short: str) -> str:
    if short == "GEMINI_API_KEY":
        return config.GEMINI_API_KEY
    if short == "OPENAI_API_KEY":
        return config.OPENAI_API_KEY
    from .pipecat_voice import _key
    return _key(short)


def needed_keys(provider: str) -> list[str]:
    """API key settings the provider needs (whether or not they're set)."""
    if provider == "gemini":
        return [] if config.GCP_PROJECT and not config.GEMINI_API_KEY else ["GEMINI_API_KEY"]
    if provider == "openai":
        return ["OPENAI_API_KEY"]
    if provider in voice.PIPECAT:
        from .pipecat_voice import SERVICES, PipecatVoice
        parts = PipecatVoice(provider, model="", voice="", instructions="", tools=voice.TOOLS).parts
        out = []
        for kind, spec in parts.items():
            name = spec.partition(":")[0]
            key = SERVICES[kind].get(name, (None, None, None))[2] if spec else None
            if key and key not in out:
                out.append(key)
        return out
    return []


# ------------------------------------------------------------------------------------------- status

def status() -> dict:
    provider = voice.default_provider()
    st = {"settings_file": str(config.ENV_FILE)}
    st["you"] = {"done": bool(config.OWNER_NAME), "owner": config.OWNER_NAME, "pronouns": config.OWNER_PRONOUNS,
                 "country_code": config.COUNTRY_CODE}
    missing_keys = [k for k in needed_keys(provider) if not key_value(k)]
    problems = [p for p in [voice.configured(provider)] if p]
    st["voice"] = {"done": not problems, "provider": provider, "chosen": bool(config.PROVIDER),
                   "missing_keys": missing_keys, "problems": problems}
    if provider == "pipeline":
        st["voice"].update(stt=config.STT, llm=config.LLM, tts=config.TTS)
    adb_ok = bool(shutil.which("adb"))
    st["adb"] = {"done": adb_ok}
    st["phone"] = {"done": False}
    if adb_ok:
        try:
            serial = phone.ensure_device(wait=2)
            st["phone"] = {"done": True, "device": serial, "model": onboard.phone_model()}
        except phone.PhoneError as e:
            devs = onboard.devices()
            st["phone"] = {"done": False, "error": str(e),
                           "unauthorized": [d for d, s in devs.items() if s == "unauthorized"]}
    st["audio"] = {"mode": config.AUDIO, "confirmed": bool(onboard.current("AUDIO"))}
    st["next"] = next_step(st)
    st["ready"] = st["next"]["step"] == "done"
    return st


def next_step(st: dict) -> dict:
    if not st["you"]["done"]:
        return {"step": "you", "do": 'claude-tele-connect setup --owner "<name>" [--country-code 91] [--pronouns ...]',
                "why": "the name the AI says it calls for"}
    v = st["voice"]
    if not v["chosen"] and not v["done"]:
        return {"step": "voice", "do": "claude-tele-connect setup --provider <gemini|openai|grok|pipeline|local>",
                "why": "choose the AI voice (gemini has a free tier)"}
    if v["missing_keys"]:
        return {"step": "keys", "do": "claude-tele-connect key",
                "why": f"the user pastes {', '.join(v['missing_keys'])} into a local browser form"}
    if v["problems"]:
        return {"step": "voice", "do": "fix: " + "; ".join(v["problems"]), "why": "the AI voice isn't usable yet"}
    if not st["adb"]["done"]:
        return {"step": "adb", "do": "install Android platform-tools (macOS: brew install android-platform-tools; "
                                     "Windows: winget install --id=Google.PlatformTools -e; Debian/Ubuntu: sudo apt install adb)",
                "why": "adb controls the phone"}
    if not st["phone"]["done"]:
        if st["phone"].get("unauthorized"):
            return {"step": "phone", "do": "ask the user to tap Allow on the phone (Always allow from this computer)",
                    "why": "the phone is connected but not authorized"}
        return {"step": "phone", "do": "USB: user turns on USB debugging, plugs in, taps Allow. Wi-Fi: user opens "
                                       "Wireless debugging > Pair device with pairing code and reads you the IP:port and "
                                       "code, then: claude-tele-connect phone pair <ip:port> <code>",
                "why": "no phone connected over adb"}
    if not st["audio"]["confirmed"]:
        return {"step": "check", "do": "claude-tele-connect check --json", "why": "find the phone's audio mode"}
    return {"step": "done", "do": "optional: claude-tele-connect test-voice --json, then a test call the user approves",
            "why": "setup is complete"}


def print_status(st: dict) -> None:
    mark = lambda d: "✓" if d else "✗"
    print(f"{mark(st['you']['done'])} You: {st['you']['owner'] or 'name not set'}")
    v = st["voice"]
    extra = f" ({v.get('stt')} + {v.get('llm')} + {v.get('tts')})" if v["provider"] == "pipeline" else ""
    print(f"{mark(v['done'])} AI voice: {v['provider']}{extra}" + (f"; missing {', '.join(v['missing_keys'])}" if v["missing_keys"] else ""))
    for p in v["problems"]:
        if not v["missing_keys"]:
            print(f"    {p}")
    print(f"{mark(st['adb']['done'])} adb installed")
    ph = st["phone"]
    print(f"{mark(ph['done'])} Phone: " + (f"{ph.get('model') or ph.get('device')}" if ph["done"] else "not connected"))
    print(f"{mark(st['audio']['confirmed'])} Audio mode: {st['audio']['mode']}" + ("" if st["audio"]["confirmed"] else " (not checked yet)"))
    n = st["next"]
    print("\nReady to call." if st["ready"] else f"\nNext ({n['step']}): {n['do']}")


# ------------------------------------------------------------------------------------------- keys

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>claude-tele-connect keys</title><style>
:root{{color-scheme:light dark;--bg:#fafafa;--fg:#1a1a1a;--mut:#666;--card:#fff;--line:#ddd;--acc:#2563eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#eee;--mut:#aaa;--card:#1f1f1f;--line:#333;--acc:#60a5fa}}}}
body{{font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:24px 16px}}
main{{max-width:520px;margin:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px}}
h1{{font-size:20px;margin:0 0 8px}} p{{color:var(--mut);margin:0 0 20px}} label{{display:block;font-weight:600;margin-top:16px}}
input{{width:100%;box-sizing:border-box;padding:10px;margin-top:6px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}}
small a{{color:var(--acc)}} button{{margin-top:24px;width:100%;padding:12px;border:0;border-radius:8px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}}
</style></head><body><main><h1>{title}</h1><p>{intro}</p>{body}</main></body></html>"""


def _form(shorts, token) -> str:
    fields = ""
    for s in shorts:
        label, url = KEYS.get(s, (s, ""))
        saved = " (one is saved; leave empty to keep it)" if key_value(s) else ""
        fields += (f'<label for="{s}">{html.escape(label)}</label><small>Get one at '
                   f'<a href="{url}" target="_blank" rel="noopener">{html.escape(url)}</a>{saved}</small>'
                   f'<input type="password" id="{s}" name="{s}" autocomplete="off" spellcheck="false">')
    body = f'<form method="post" action="/{token}">{fields}<button type="submit">Save</button></form>'
    return PAGE.format(title="API keys for claude-tele-connect",
                       intro=f"Saved only on this computer, in {html.escape(str(config.ENV_FILE))}. "
                             "They aren't shown to Claude or sent anywhere else.", body=body)


def key_form(shorts: list[str], timeout: float = 600, open_browser: bool = True) -> list[str]:
    """Serve a one-time form on 127.0.0.1; return the setting names the user saved."""
    token = secrets.token_urlsafe(16)
    saved: list[str] = []
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok_host(self):
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, code, body):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._ok_host() or self.path != f"/{token}":
                return self._send(404, "Not found")
            self._send(200, _form(shorts, token))

        def do_POST(self):
            if not self._ok_host() or self.path != f"/{token}":
                return self._send(404, "Not found")
            n = int(self.headers.get("Content-Length", 0) or 0)
            form = urllib.parse.parse_qs(self.rfile.read(min(n, 100_000)).decode(errors="replace"))
            values = {}
            for s in shorts:
                v = (form.get(s) or [""])[0].strip()
                if v:
                    values[onboard.key_name(s)] = v
                    saved.append(s)
            onboard.save(values)
            self._send(200, PAGE.format(title="Saved", intro="You can close this tab and go back to Claude.",
                                        body="" if saved else "<p>Nothing was entered, so nothing changed.</p>"))
            done.set()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/{token}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Opening a form in your browser to paste: {', '.join(KEYS.get(s, (s,))[0] for s in shorts)}")
    print(f"If it didn't open, visit: {url}")
    print(f"Waiting up to {f'{timeout / 60:.0f} minutes' if timeout >= 120 else f'{timeout:.0f} seconds'}...", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    done.wait(timeout)
    time.sleep(0.3)
    srv.shutdown()
    return saved


def resolve_key_names(names: list[str]) -> list[str]:
    out = []
    for n in names:
        s = ALIASES.get(n.lower()) or (n.upper() if n.upper() in KEYS else None)
        if not s:
            raise phone.PhoneError(f"unknown key '{n}' (choose from: {', '.join(sorted(set(ALIASES)))})")
        out.append(s)
    return out


def cmd_key(a) -> int:
    shorts = resolve_key_names(a.names) if a.names else needed_keys(voice.default_provider())
    if not a.names and not a.all:
        shorts = [s for s in shorts if not key_value(s)] or shorts
    if not shorts:
        print("The current AI voice doesn't need an API key.")
        return 0
    if a.stdin:  # scripts: one key per line, in the order given
        lines = [l.strip() for l in sys.stdin.read().splitlines() if l.strip()]
        values = {onboard.key_name(s): v for s, v in zip(shorts, lines)}
        onboard.save(values)
        saved = [s for s, v in zip(shorts, lines) if v]
    else:
        saved = key_form(shorts, timeout=a.timeout, open_browser=not a.no_browser)
    if saved:
        print(f"Saved: {', '.join(saved)}")
        return 0
    print("No key was saved (the form timed out or was empty).")
    return 1


# ------------------------------------------------------------------------------------------- phone

def cmd_phone(a) -> int:
    out = {}
    if a.action == "pair":
        if not re.fullmatch(r"[\w.\-]+:\d+", a.address) or not re.fullmatch(r"\d{6}", a.code or ""):
            raise phone.PhoneError("usage: claude-tele-connect phone pair <ip:port> <6-digit code> "
                                   "(both from Wireless debugging > Pair device with pairing code)")
        r = subprocess.run(["adb", "pair", a.address, a.code], capture_output=True, text=True, timeout=30)
        msg = (r.stdout + r.stderr).strip()
        out["paired"] = "Successfully paired" in msg
        if not out["paired"]:
            out["error"] = msg[:300] or "no answer"
        else:
            out["device"] = onboard.wait_for_phone(15)
            if not out["device"]:
                out["hint"] = ("paired, but the phone wasn't found automatically: ask for the address at the top of "
                               "the Wireless debugging screen and run: claude-tele-connect phone connect <ip:port>")
    elif a.action == "connect":
        if not re.fullmatch(r"[\w.\-]+:\d+", a.address or ""):
            raise phone.PhoneError("usage: claude-tele-connect phone connect <ip:port>")
        phone._host("connect", a.address, timeout=10)
        out["device"] = onboard.wait_for_phone(10)
        if out["device"]:
            onboard.save({onboard.key_name("ADB_DEVICE"): a.address})
            out["saved_adb_device"] = a.address
    else:  # status
        try:
            out["device"] = phone.ensure_device(wait=3)
        except phone.PhoneError as e:
            out["device"], out["error"] = "", str(e)
        out["devices"] = onboard.devices()
    if out.get("device"):
        out["model"] = onboard.phone_model()
    out["connected"] = bool(out.get("device"))
    if a.json:
        print(json.dumps(out))
    else:
        for k, v in out.items():
            print(f"{k}: {v}")
    return 0 if out["connected"] else 1


# ------------------------------------------------------------------------------------------- voice test / check

def cmd_test_voice(a) -> int:
    provider = a.provider or voice.default_provider()
    try:
        secs, audio_s, text = onboard.run_voice_test(provider)
        out = {"ok": audio_s > 0, "provider": provider, "audio_seconds": round(audio_s, 1),
               "total_seconds": round(secs, 1), "said": text}
    except Exception as e:
        out = {"ok": False, "provider": provider, "error": str(e)[:400]}
    if a.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"OK: the AI spoke {out['audio_seconds']}s: {out['said']!r}" if out["ok"] else f"Failed: {out.get('error')}")
    return 0 if out["ok"] else 1


def check_json(a) -> int:
    import platform
    out = {"ok": True, "problems": []}
    try:
        out["device"] = phone.ensure_device()
        phone.push_dex()
        info = phone.probe()
        out.update(model=info["model"], android_sdk=info["sdk"], interceptable=info["interceptable"],
                   bluetooth_name=phone.bluetooth_name())
        if info["sdk"] and info["sdk"] < 33:
            out["problems"].append("needs Android 13 or newer")
        if info["interceptable"]:
            out["audio_mode"] = "phone"
        elif platform.system() == "Darwin":
            out["audio_mode"] = None
            out["problems"].append("this phone needs Bluetooth audio mode, which needs a Windows or Linux computer")
        else:
            out["audio_mode"] = "bluetooth"
    except phone.PhoneError as e:
        out["problems"].append(str(e))
    if out.get("audio_mode"):
        onboard.save({onboard.key_name("AUDIO"): out["audio_mode"]})
        out["saved_audio_mode"] = out["audio_mode"]
    provider = a.provider or voice.default_provider()
    p = voice.configured(provider)
    out["voice"] = {"provider": provider, "ok": not p}
    if p:
        out["problems"].append(p)
    if not config.OWNER_NAME:
        out["problems"].append("owner name not set: claude-tele-connect setup --owner \"<name>\"")
    out["ok"] = not out["problems"]
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1
