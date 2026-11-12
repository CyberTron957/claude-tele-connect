"""Control the Android phone over adb: dial, hang up, call state, contacts, and the CallAudio bridge."""

import asyncio
import re
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

DEX_LOCAL = Path(__file__).resolve().parent / "callaudio.dex"  # prebuilt; android/build.py rebuilds it
DEX_REMOTE = "/data/local/tmp/callaudio.dex"
BRIDGE_PORT = 27184

# PreciseCallState foreground values
CALL_IDLE, CALL_ACTIVE, CALL_HOLDING, CALL_DIALING, CALL_ALERTING = 0, 1, 2, 3, 4
CALL_STATE_NAMES = {-1: "none", 0: "idle", 1: "active", 2: "holding", 3: "dialing", 4: "ringing",
                    5: "incoming", 6: "waiting", 7: "disconnected", 8: "disconnecting"}

T_STOP, T_AUDIO, T_FLUSH, T_EVENT, T_MUTE = 0, 1, 2, 3, 4


class PhoneError(RuntimeError):
    pass


_selected = ""  # set by ensure_device(): the adb serial every later command is pinned to


def _target() -> list[str]:
    """Extra adb arguments that pin every command to one device."""
    dev = _selected or config.ADB_DEVICE
    return ["-s", dev] if dev else []


def _host(*args: str, timeout: float = 15) -> str:
    """Run an adb host command (devices/mdns/connect) that must not be pinned to a device."""
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout


def _online() -> list[str]:
    rows = [l.split() for l in _host("devices").splitlines()[1:]]
    return [r[0] for r in rows if len(r) > 1 and r[1] == "device"]


# adb errors that mean the connection to the phone dropped (common over Wi-Fi), not that the command failed
_TRANSIENT = ("connect failed", "closed", "offline", "not found", "no devices", "protocol fault",
              "connection reset", "broken pipe", "timed out")


def _transient(msg: str) -> bool:
    return any(t in msg.lower() for t in _TRANSIENT)


def _reselect() -> None:
    """After a dropped connection: re-pick the phone (it may be back under a new address or port)."""
    time.sleep(0.5)
    ensure_device()


def _fail(args, rc: int, out: str, err: str) -> str:
    return f"adb {' '.join(args)} failed: {err.strip() or out.strip() or f'exit code {rc}'}"


def adb(*args: str, timeout: float = 15, retry: bool = True) -> str:
    """Run an adb command on the phone. Retries once if the connection dropped (set retry=False for
    commands that must never run twice)."""
    try:
        r = subprocess.run(["adb", *_target(), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = -1, "", f"timed out after {timeout:.0f}s"
    if rc != 0:
        msg = _fail(args, rc, out, err)
        if retry and _transient(msg):
            _reselect()
            return adb(*args, timeout=timeout, retry=False)
        raise PhoneError(msg)
    return out


async def adb_async(*args: str, timeout: float = 10, retry: bool = True) -> str:
    p = await asyncio.create_subprocess_exec("adb", *_target(), *args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
        rc = p.returncode
    except asyncio.TimeoutError:
        p.kill()
        out, err, rc = b"", f"timed out after {timeout:.0f}s".encode(), -1
    if rc != 0:
        msg = _fail(args, rc, out.decode(errors="replace"), err.decode(errors="replace"))
        if retry and _transient(msg):
            await asyncio.to_thread(_reselect)
            return await adb_async(*args, timeout=timeout, retry=False)
        raise PhoneError(msg)
    return out.decode(errors="replace")


def ensure_device(wait: float = 8) -> str:
    """Pick the phone to use and pin all adb commands to it. If nothing is online, keep trying for
    `wait` seconds to reconnect wireless-debugging devices found via mDNS (Wi-Fi links drop briefly
    when the phone's Wi-Fi power saving kicks in, and the port changes after a restart)."""
    global _selected
    end = time.time() + wait
    online = _online()
    while not online and time.time() < end:
        if re.fullmatch(r"[\w.\-]+:\d+", config.ADB_DEVICE):  # a fixed ip:port works even where mDNS is blocked
            _host("connect", config.ADB_DEVICE, timeout=5)
        for line in _host("mdns", "services").splitlines():
            if "_adb-tls-connect._tcp" in line and line.split():
                _host("connect", line.split()[-1], timeout=5)
        online = _online()
        if not online:
            time.sleep(1.0)
            online = _online()
    if not online:
        raise PhoneError("No authorized phone found over adb (USB debugging or Wireless debugging on? "
                         "same Wi-Fi? paired once with 'adb pair'?)")
    if config.ADB_DEVICE in online:
        _selected = config.ADB_DEVICE
    else:  # prefer plain serials / ip:port over duplicate mDNS-named entries for the same phone
        _selected = next((d for d in online if "_adb-tls" not in d), online[0])
    return _selected


def push_dex() -> None:
    if not DEX_LOCAL.exists():
        raise PhoneError(f"{DEX_LOCAL} missing; reinstall claude-tele-connect or run python android/build.py")
    adb("push", str(DEX_LOCAL), DEX_REMOTE)


def probe() -> dict:
    """Run CallAudio's compatibility probe. interceptable=True means the adb-only audio path works."""
    out = adb("shell", f"CLASSPATH={DEX_REMOTE}", "app_process", "/", "CallAudio", "probe", timeout=30)
    m = re.search(r"isPstnCallAudioInterceptable=(\w+)", out)
    sdk = re.search(r"SDK=(\d+) model=(.*?) uid=", out)
    return {"interceptable": bool(m and m.group(1) == "true"), "sdk": int(sdk.group(1)) if sdk else 0,
            "model": sdk.group(2) if sdk else "", "raw": out}


def bluetooth_name() -> str:
    """The phone's Bluetooth name, i.e. what the computer shows for it once paired."""
    for args in (("settings", "get", "secure", "bluetooth_name"), ("settings", "get", "global", "device_name")):
        try:
            v = adb("shell", *args).strip()
        except PhoneError:
            continue
        if v and v != "null":
            return v
    return ""


def normalize_number(n: str) -> str:
    n = re.sub(r"[^\d+]", "", n)
    if n.startswith("00"):
        n = "+" + n[2:]
    if not n.startswith("+") and len(n) == 10 and config.COUNTRY_CODE:  # national number, add country code
        n = "+" + config.COUNTRY_CODE.lstrip("+") + n
    return n


# Emergency and police/fire/ambulance short codes in common countries. The tool refuses to dial these.
EMERGENCY = {"100", "101", "102", "108", "112", "911", "999", "000", "110", "119", "15", "17", "18", "1122", "10111"}


def is_emergency(number: str) -> bool:
    return re.sub(r"\D", "", number) in EMERGENCY


def lookup_contact(name: str) -> list[tuple[str, str]]:
    """Return [(display_name, number)] for contacts whose name matches exactly (case-insensitive),
    falling back to substring matches. Numbers are de-duplicated."""
    out = adb("shell", "content", "query", "--uri", "content://com.android.contacts/data/phones",
              "--projection", "display_name:data1")
    rows = []
    for line in out.splitlines():
        m = re.search(r"display_name=(.*?), data1=(.*)$", line)
        if m:
            rows.append((m.group(1).strip(), normalize_number(m.group(2))))
    exact = [r for r in rows if r[0].lower() == name.lower()]
    hits = exact or [r for r in rows if name.lower() in r[0].lower()]
    seen, uniq = set(), []
    for disp, num in hits:
        if num not in seen:
            seen.add(num)
            uniq.append((disp, num))
    return uniq


def resolve_target(target: str) -> tuple[str, str]:
    """Accept a phone number or a contact name; return (label, number)."""
    if re.fullmatch(r"[\d\s+()\-*#]+", target) and re.search(r"\d", target):  # incl. short codes like 121
        return target, normalize_number(target)
    matches = lookup_contact(target)
    if not matches:
        raise PhoneError(f"No contact named '{target}'")
    if len(matches) > 1:
        listing = ", ".join(f"{d} {n}" for d, n in matches)
        raise PhoneError(f"'{target}' is ambiguous: {listing}. Pass the number instead.")
    return matches[0]


async def call_state() -> int:
    out = await adb_async("shell", "dumpsys", "telephony.registry")
    m = re.search(r"Foreground call state: (-?\d+)", out)
    return int(m.group(1)) if m else -1


async def ringing_state() -> int:
    out = await adb_async("shell", "dumpsys", "telephony.registry")
    m = re.search(r"Ringing call state: (-?\d+)", out)
    return int(m.group(1)) if m else -1


async def incoming_number() -> str:
    out = await adb_async("shell", "dumpsys", "telephony.registry")
    m = re.search(r"mCallIncomingNumber=(\S*)", out)
    return m.group(1) if m else ""


async def dial(number: str) -> None:
    # never retried: if the command reached the phone before the connection dropped, a retry would dial twice
    await adb_async("shell", "am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{number}", retry=False)


async def answer() -> None:
    await adb_async("shell", "input", "keyevent", "KEYCODE_CALL")


async def hang_up() -> None:
    await adb_async("shell", "input", "keyevent", "KEYCODE_ENDCALL")


@dataclass
class Bridge:
    proc: asyncio.subprocess.Process
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    log_task: asyncio.Task
    events: asyncio.Queue

    async def read_frame(self) -> tuple[int, bytes]:
        hdr = await self.reader.readexactly(3)
        t, n = hdr[0], struct.unpack(">H", hdr[1:])[0]
        return t, (await self.reader.readexactly(n) if n else b"")

    def send(self, t: int, payload: bytes = b"") -> None:
        for i in range(0, max(len(payload), 1), 65535):
            chunk = payload[i:i + 65535]
            self.writer.write(bytes([t]) + struct.pack(">H", len(chunk)) + chunk)

    def send_audio(self, pcm: bytes) -> None:
        self.send(T_AUDIO, pcm)

    def flush(self) -> None:
        self.send(T_FLUSH)

    def mute_mic(self, mute: bool) -> None:
        self.send(T_MUTE, b"\x01" if mute else b"\x00")

    async def drain(self) -> None:
        await self.writer.drain()

    async def close(self) -> None:
        try:
            self.send(T_STOP)
            await self.writer.drain()
            self.writer.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), 3)
        except asyncio.TimeoutError:
            self.proc.kill()
        self.log_task.cancel()
        try:
            await adb_async("forward", "--remove", f"tcp:{BRIDGE_PORT}")
        except Exception:
            pass


async def start_bridge(uplink_rate: int = 24000, mode: str = "audio", log=print) -> Bridge:
    """Launch the CallAudio bridge on the phone (before the call) and connect to it.
    mode "audio": call audio via interception; "events": call state only (audio goes over Bluetooth)."""
    try:
        await adb_async("shell", "pkill", "-f", "CallAudio")
    except PhoneError:
        pass  # nothing running
    proc = await asyncio.create_subprocess_exec(
        "adb", *_target(), "shell", f"CLASSPATH={DEX_REMOTE}", "app_process", "/", "CallAudio", "bridge", str(uplink_rate), mode,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    listening = asyncio.Event()

    async def pump_logs():
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if "LISTENING" in text:
                listening.set()
            log(f"  [phone] {text}")

    log_task = asyncio.create_task(pump_logs())
    try:
        await asyncio.wait_for(listening.wait(), 10)
    except asyncio.TimeoutError:
        proc.kill()
        raise PhoneError("CallAudio bridge did not start on the phone")
    await adb_async("forward", f"tcp:{BRIDGE_PORT}", "localabstract:callaudio")
    reader, writer = await asyncio.open_connection("127.0.0.1", BRIDGE_PORT)
    return Bridge(proc, reader, writer, log_task, asyncio.Queue())
