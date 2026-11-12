"""Bluetooth audio, for Android phones that don't let adb reach call audio.

The computer is paired with the phone as a Bluetooth hands-free device (like a car kit), so the phone
sends the call audio to the computer. adb still dials, hangs up and reports call state (CallAudio runs
in "events" mode); this module reads and writes the computer's hands-free audio device.

  Linux:   PipeWire or PulseAudio. Devices via `pactl`, audio via `parec` / `pacat`.
  Windows: WASAPI through the `sounddevice` package (pip install sounddevice).
  macOS:   not possible. macOS can't act as a hands-free device for a phone.

BluetoothBridge has the same interface as phone.Bridge, so the agent doesn't care which one it gets.
"""

import asyncio
import os
import platform
import re
import subprocess
import threading
import time

import numpy as np

from . import phone

IN_RATE = 16000                   # call audio -> Gemini
OUT_RATE = 24000                  # Gemini -> call
FRAME_BYTES = IN_RATE // 50 * 2   # 20 ms at 16 kHz, 16-bit mono
OPEN_TIMEOUT_S = 12               # keep retrying the audio device this long after the call starts


class Resampler:
    """Streaming 16-bit mono resampler: windowed-sinc low-pass (when downsampling) + linear interpolation."""

    def __init__(self, src: int, dst: int):
        self.src, self.dst, self.step = src, dst, src / dst
        self.pos = 0.0                         # read position into self.tail + next input
        self.tail = np.zeros(0, np.float32)
        self.taps = None
        if dst < src:
            n = 63
            fc = 0.45 * dst / src              # cutoff just below the new Nyquist, in cycles/sample
            k = np.arange(n) - (n - 1) / 2
            h = 2 * fc * np.sinc(2 * fc * k) * np.hamming(n)
            self.taps = (h / h.sum()).astype(np.float32)
            self.hist = np.zeros(n - 1, np.float32)

    def process(self, pcm: bytes) -> bytes:
        if self.src == self.dst or not pcm:
            return pcm
        x = np.frombuffer(pcm, "<i2").astype(np.float32)
        if self.taps is not None:
            buf = np.concatenate([self.hist, x])
            self.hist = buf[len(buf) - len(self.hist):]
            x = np.convolve(buf, self.taps, mode="valid")
        x = np.concatenate([self.tail, x])
        last = len(x) - 1
        if last < self.pos:
            self.tail = x
            return b""
        n_out = int((last - self.pos) // self.step) + 1
        idx = self.pos + self.step * np.arange(n_out)
        i0 = idx.astype(np.int64)
        frac = (idx - i0).astype(np.float32)
        y = x[i0] * (1 - frac) + x[np.minimum(i0 + 1, last)] * frac
        nxt = self.pos + self.step * n_out
        keep = int(nxt)
        self.tail = x[keep:] if keep <= last else np.zeros(0, np.float32)
        self.pos = nxt - min(keep, len(x))
        return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()


def _matches(text: str, keys: list[str]) -> bool:
    t = text.lower().replace(":", "_")
    return any(k and k.lower().replace(":", "_") in t for k in keys)


# ---------------------------------------------------------------- Linux (PipeWire / PulseAudio)

def _pactl(*args: str) -> str:
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=10, env=dict(os.environ, LC_ALL="C"))
    except FileNotFoundError:
        raise phone.PhoneError("pactl not found. Install pulseaudio-utils (works with PipeWire too).")
    if r.returncode != 0:
        raise phone.PhoneError(f"pactl {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def _pa_list(kind: str) -> list[dict]:
    """Parse `pactl list sources|sinks|cards`."""
    items, cur, section = [], None, None
    for line in _pactl("list", kind).splitlines():
        if re.match(r"^\S.*#\d+\s*$", line):  # "Source #12"
            cur = {"name": "", "description": "", "profiles": [], "active_profile": ""}
            items.append(cur)
            section = None
            continue
        if cur is None or not line.strip():
            continue
        s = line.strip()
        depth = len(line) - len(line.lstrip("\t"))
        if depth == 1:
            section = None
            key, _, val = s.partition(":")
            if key == "Name":
                cur["name"] = val.strip()
            elif key == "Description":
                cur["description"] = val.strip()
            elif key == "Active Profile":
                cur["active_profile"] = val.strip()
            elif key in ("Profiles", "Properties"):
                section = key
        elif depth == 2 and section == "Profiles":
            cur["profiles"].append(s.split(":", 1)[0])
        elif depth == 2 and section == "Properties" and s.startswith("device.description = ") and not cur["description"]:
            cur["description"] = s.split("=", 1)[1].strip().strip('"')
    return items


def linux_find(keys: list[str], log=print) -> tuple[str, str]:
    """Return (source, sink) for the phone's hands-free link, switching the Bluetooth card to its
    audio-gateway profile (computer = hands-free, phone = gateway) if needed."""
    def nodes():
        def ok(d):
            return "bluez" in d["name"] and not d["name"].endswith(".monitor") and \
                (not keys or _matches(d["name"] + " " + d["description"], keys))
        return [d["name"] for d in _pa_list("sources") if ok(d)], [d["name"] for d in _pa_list("sinks") if ok(d)]

    cards = [c for c in _pa_list("cards") if c["name"].startswith("bluez_card")
             and (not keys or _matches(c["name"] + " " + c["description"], keys))]
    for c in cards:
        gw = next((p for p in c["profiles"] if re.search(r"audio.gateway", p)), None)
        if gw and c["active_profile"] != gw:
            log(f"Switching {c['description'] or c['name']} to the hands-free profile ({gw})")
            _pactl("set-card-profile", c["name"], gw)
            time.sleep(0.5)
    srcs, sinks = nodes()
    if srcs and sinks:
        return srcs[0], sinks[0]
    seen = ", ".join(f"{c['description'] or c['name']} (profiles: {', '.join(c['profiles'])})" for c in cards)
    raise phone.PhoneError(
        "No Bluetooth hands-free audio for the phone. Pair and connect the phone to this computer, and make "
        "sure PipeWire/PulseAudio has the hands-free role enabled (PipeWire: bluez5.roles includes hfp_hf). "
        + (f"Matching Bluetooth cards: {seen}" if seen else "No matching Bluetooth card found.")
        + (f" Looked for: {', '.join(k for k in keys if k)}." if keys else ""))


class PulseAudioIO:
    """Hands-free audio through `parec` (phone -> us) and `pacat` (us -> phone)."""

    def __init__(self, keys: list[str], log=print):
        self.keys, self.log = keys, log
        self.rec = self.play = None
        self.task = None
        self.name = ""

    async def _spawn_play(self) -> None:
        self.play = await asyncio.create_subprocess_exec(
            "pacat", "--playback", f"--device={self.sink}", "--format=s16le", f"--rate={OUT_RATE}", "--channels=1",
            "--raw", "--latency-msec=40", "--client-name=claude-tele-connect", stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)

    async def start(self, on_frame, on_error) -> None:
        self.source, self.sink = await asyncio.to_thread(linux_find, self.keys, self.log)
        self.name = self.source
        self.rec = await asyncio.create_subprocess_exec(
            "parec", f"--device={self.source}", "--format=s16le", f"--rate={IN_RATE}", "--channels=1", "--raw",
            "--latency-msec=20", "--client-name=claude-tele-connect", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        await self._spawn_play()

        async def pump():
            try:
                while True:
                    on_frame(await self.rec.stdout.readexactly(FRAME_BYTES))
            except asyncio.IncompleteReadError:
                on_error("parec stopped")
        self.task = asyncio.create_task(pump())

    def write(self, pcm: bytes) -> None:
        if self.play and self.play.returncode is None:
            self.play.stdin.write(pcm)

    def flush(self) -> None:
        # pacat can't drop what it has queued, so restart it (takes ~50 ms).
        old, self.play = self.play, None
        if old and old.returncode is None:
            old.stdin.transport.abort()  # drop unsent audio without a BrokenPipe error later
            old.kill()
        self._respawn = asyncio.get_running_loop().create_task(self._spawn_play())

    async def drain(self) -> None:
        if self.play and self.play.returncode is None:
            try:
                await self.play.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
        for p in (self.rec, self.play):
            if p and p.returncode is None:
                p.kill()
                await p.wait()


# ---------------------------------------------------------------- Windows (WASAPI via sounddevice)

def _sd():
    try:
        import sounddevice
    except ImportError:
        raise phone.PhoneError("Bluetooth mode needs the sounddevice package: pip install sounddevice")
    return sounddevice


def windows_find(keys: list[str], hint_only: bool, rescan: bool = True) -> tuple[int, int]:
    """Return (input, output) device indexes for the phone's hands-free endpoints.
    Windows names them like "Microphone (<phone> Hands-Free HF Audio)" and only activates them while
    the phone sends call audio, so PortAudio is re-scanned each time."""
    sd = _sd()
    if rescan:
        try:  # PortAudio only enumerates devices at init
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
    apis, devs = sd.query_hostapis(), sd.query_devices()

    def ok(d):
        n = d["name"].lower()
        if hint_only:
            return _matches(n, keys)
        handsfree = "hands-free" in n or "hf audio" in n
        return handsfree and (_matches(n, keys) if keys else "hf audio" in n)  # headsets are "AG Audio"

    def pick(channels_key):
        found = [i for i, d in enumerate(devs) if d[channels_key] > 0 and ok(d)]
        found.sort(key=lambda i: 0 if "WASAPI" in apis[devs[i]["hostapi"]]["name"] else 1)  # full names, low latency
        return found[0] if found else None

    i, o = pick("max_input_channels"), pick("max_output_channels")
    if i is None or o is None:
        names = sorted({d["name"] for d in devs if "hands" in d["name"].lower() or _matches(d["name"], keys)})
        raise phone.PhoneError(
            "No Bluetooth hands-free audio device for the phone. Pair the phone in Settings > Bluetooth and keep "
            "it connected (it should say 'Connected voice'). Windows may only show the device during a call. "
            + (f"Similar devices: {', '.join(names)}. " if names else "")
            + (f"Looked for: {', '.join(k for k in keys if k)}." if keys else ""))
    return i, o


class SoundDeviceIO:
    """Hands-free audio through PortAudio (WASAPI on Windows)."""

    def __init__(self, keys: list[str], log=print, hint_only: bool = False):
        self.keys, self.log, self.hint_only = keys, log, hint_only
        self.buf, self.lock = bytearray(), threading.Lock()
        self.ins = self.outs = None
        self.name = ""

    async def start(self, on_frame, on_error) -> None:
        sd = _sd()
        i, o = await asyncio.to_thread(windows_find, self.keys, self.hint_only)
        devs, apis = sd.query_devices(), sd.query_hostapis()
        self.name = devs[i]["name"]
        in_rate, out_rate = int(devs[i]["default_samplerate"]), int(devs[o]["default_samplerate"])
        down, self.up = Resampler(in_rate, IN_RATE), Resampler(OUT_RATE, out_rate)
        loop = asyncio.get_running_loop()
        acc = bytearray()

        def deliver(pcm: bytes) -> None:
            acc.extend(pcm)
            while len(acc) >= FRAME_BYTES:
                on_frame(bytes(acc[:FRAME_BYTES]))
                del acc[:FRAME_BYTES]

        def in_cb(indata, frames, t, status):
            loop.call_soon_threadsafe(deliver, down.process(bytes(indata)))

        def out_cb(outdata, frames, t, status):
            n = len(outdata)
            with self.lock:
                k = min(n, len(self.buf))
                outdata[:k] = bytes(self.buf[:k])
                del self.buf[:k]
            if k < n:
                outdata[k:] = bytes(n - k)

        def extra(idx):
            return sd.WasapiSettings(auto_convert=True) if "WASAPI" in apis[devs[idx]["hostapi"]]["name"] else None

        self.ins = sd.RawInputStream(device=i, samplerate=in_rate, channels=1, dtype="int16", blocksize=in_rate // 50,
                                     latency="low", callback=in_cb, extra_settings=extra(i))
        try:
            self.outs = sd.RawOutputStream(device=o, samplerate=out_rate, channels=1, dtype="int16",
                                           blocksize=out_rate // 50, latency="low", callback=out_cb,
                                           extra_settings=extra(o))
        except Exception:
            self.ins.close()
            raise
        self.ins.start()
        self.outs.start()

    def write(self, pcm: bytes) -> None:
        out = self.up.process(pcm)
        with self.lock:
            self.buf += out

    def flush(self) -> None:
        with self.lock:
            self.buf.clear()

    async def drain(self) -> None:
        pass

    async def close(self) -> None:
        for s in (self.ins, self.outs):
            if s:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass


# ---------------------------------------------------------------- the bridge

def make_audio_io(hint: str = "", phone_name: str = "", log=print):
    system = platform.system()
    keys = [hint] if hint else ([phone_name] if phone_name else [])
    if system == "Linux":
        return PulseAudioIO(keys, log)
    if system == "Windows":
        return SoundDeviceIO(keys, log, hint_only=bool(hint))
    system = {"Darwin": "macOS"}.get(system, system)
    raise phone.PhoneError(f"Bluetooth audio isn't supported on {system}: it can't act as a hands-free device "
                           "for a phone. Use audio mode 'phone' (if your phone supports it) or a Windows/Linux computer.")


def preflight(hint: str = "", phone_name: str = "", log=print) -> str:
    """Check the hands-free audio device before dialing. Returns a description; raises PhoneError on Linux
    if it's missing. On Windows a missing device is only a warning (it may appear once the call starts)."""
    system = platform.system()
    keys = [hint] if hint else ([phone_name] if phone_name else [])
    if system == "Linux":
        src, sink = linux_find(keys, log)
        return f"{src} / {sink}"
    if system == "Windows":
        try:
            sd = _sd()
            i, o = windows_find(keys, hint_only=bool(hint))
            return f"{sd.query_devices(i)['name']} / {sd.query_devices(o)['name']}"
        except phone.PhoneError as e:
            return f"not visible yet ({e})"
    make_audio_io(hint, phone_name, log)  # raises the "not supported" error
    return ""


class BluetoothBridge:
    """Call state from the phone (CallAudio "events" mode) + call audio from the hands-free device."""

    def __init__(self, events: phone.Bridge, io, log=print):
        self.events, self.io, self.log = events, io, log
        self.q: asyncio.Queue = asyncio.Queue()
        self.audio_on = False
        self.opener = None
        self.pump = asyncio.create_task(self._pump_events())

    async def _pump_events(self) -> None:
        try:
            while True:
                t, payload = await self.events.read_frame()
                if t == phone.T_EVENT and payload.startswith(b"CALL ") and self.opener is None:
                    fg = int(re.search(rb"fg=(-?\d+)", payload).group(1))
                    if fg in (phone.CALL_DIALING, phone.CALL_ALERTING, phone.CALL_ACTIVE):
                        # Android routes call audio to Bluetooth from dialing on: open it early.
                        self.opener = asyncio.create_task(self._open_audio())
                if payload != b"EVENTS_ONLY":
                    self.q.put_nowait((t, payload))
        except (asyncio.IncompleteReadError, ConnectionError) as e:
            self.q.put_nowait((phone.T_EVENT, f"ERROR phone event stream closed ({e!r})".encode()))

    async def _open_audio(self) -> None:
        t0, err = time.time(), None
        while time.time() - t0 < OPEN_TIMEOUT_S:
            try:
                await self.io.start(lambda pcm: self.q.put_nowait((phone.T_AUDIO, pcm)), self._on_io_error)
                self.audio_on = True
                ms = int((time.time() - t0) * 1000)
                self.q.put_nowait((phone.T_EVENT, f"READY bluetooth device={self.io.name!r} openMs={ms}".encode()))
                return
            except Exception as e:
                err = e
                await asyncio.sleep(0.5)
        self.q.put_nowait((phone.T_EVENT, f"ERROR bluetooth audio: {err}".encode()))

    def _on_io_error(self, why: str) -> None:
        self.q.put_nowait((phone.T_EVENT, f"ERROR bluetooth audio stopped: {why}".encode()))

    async def read_frame(self) -> tuple[int, bytes]:
        return await self.q.get()

    def send_audio(self, pcm: bytes) -> None:
        if self.audio_on:  # like the phone bridge, audio sent before READY is dropped
            self.io.write(pcm)

    def flush(self) -> None:
        if self.audio_on:
            self.io.flush()

    def mute_mic(self, mute: bool) -> None:
        if mute:
            self.log("  [bluetooth] --mute-mic not needed: the phone's microphone isn't used while audio is on Bluetooth")

    async def drain(self) -> None:
        if self.audio_on:
            await self.io.drain()

    async def close(self) -> None:
        for t in (self.pump, self.opener):
            if t:
                t.cancel()
        try:
            await self.io.close()
        finally:
            await self.events.close()


async def start_bridge(hint: str = "", log=print) -> BluetoothBridge:
    """Check the hands-free device, then start CallAudio in events mode on the phone."""
    name = await asyncio.to_thread(phone.bluetooth_name)
    io = make_audio_io(hint, name, log)
    desc = await asyncio.to_thread(preflight, hint, name, log)
    log(f"Bluetooth audio: {desc}")
    events = await phone.start_bridge(OUT_RATE, mode="events", log=lambda s: None)
    return BluetoothBridge(events, io, log)
