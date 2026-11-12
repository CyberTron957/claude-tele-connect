"""Hardware-free tests: resampler accuracy and the Bluetooth bridge logic with a simulated phone.

  python tests/test_bluetooth.py
"""
import asyncio, math, platform, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ai_phone import bluetooth, phone
from ai_phone.bluetooth import Resampler

def tone(f, rate, secs, amp=8000):
    t = np.arange(int(rate*secs)) / rate
    return (amp*np.sin(2*np.pi*f*t)).astype("<i2").tobytes()

def dominant(pcm, rate):
    x = np.frombuffer(pcm, "<i2").astype(float); x = x[len(x)//4:]
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1/rate)[spec.argmax()], spec

# 1. rate conversion accuracy + streaming in 20 ms chunks == expected length
for src, dst, f in [(24000, 16000, 1000), (24000, 8000, 1000), (16000, 48000, 1000), (8000, 16000, 700), (44100, 16000, 1200), (16000, 16000, 500)]:
    r = Resampler(src, dst); inp = tone(f, src, 1.0); out = b""
    step = src // 50 * 2
    for i in range(0, len(inp), step): out += r.process(inp[i:i+step])
    n_exp = int(len(inp)/2 * dst/src)
    fd, _ = dominant(out, dst)
    # discontinuity check: max sample-to-sample jump vs pure tone
    y = np.frombuffer(out, "<i2").astype(float)
    jump = np.abs(np.diff(y[100:])).max(); ideal = 8000*2*math.pi*f/dst
    print(f"{src}->{dst}: samples {len(out)//2} (expected ~{n_exp}), peak {fd:.0f} Hz, max jump {jump:.0f} (ideal {ideal:.0f})")
    assert abs(len(out)//2 - n_exp) <= 2 and abs(fd - f) < 15 and jump < ideal*1.3

# 2. anti-aliasing: 10 kHz tone at 24k -> 16k should be strongly attenuated (would alias to 6 kHz)
r = Resampler(24000, 16000); out = r.process(tone(10000, 24000, 1.0))
lvl = np.sqrt(np.mean(np.frombuffer(out, "<i2").astype(float)[2000:]**2))
print(f"10 kHz tone after 24k->16k: rms {lvl:.0f} (input rms ~5657)"); assert lvl < 300

# 3. BluetoothBridge with a simulated phone event stream + fake audio device
class FakeEvents:
    def __init__(self): self.q = asyncio.Queue(); self.closed = False
    async def read_frame(self): return await self.q.get()
    async def close(self): self.closed = True
class FakeIO:
    name = "Fake Hands-Free"
    def __init__(self): self.written = bytearray(); self.flushed = 0; self.started = 0; self.fail = 2
    async def start(self, on_frame, on_error):
        if self.fail: self.fail -= 1; raise phone.PhoneError("not yet")   # device appears after a retry
        self.started += 1; self.on_frame = on_frame
    def write(self, pcm): self.written += pcm
    def flush(self): self.flushed += 1
    async def drain(self): pass
    async def close(self): self.closed = True

async def bridge_test():
    ev, io = FakeEvents(), FakeIO()
    b = bluetooth.BluetoothBridge(ev, io, log=print)
    ev.q.put_nowait((phone.T_EVENT, b"LISTENER ok"))
    ev.q.put_nowait((phone.T_EVENT, b"EVENTS_ONLY"))
    ev.q.put_nowait((phone.T_EVENT, b"CALL fg=0 ring=0 bg=0"))
    got = [await b.read_frame(), await b.read_frame()]
    print("events:", got); assert got[1][1].startswith(b"CALL fg=0") and b.opener is None
    b.send_audio(b"\x00\x00" * 10); assert not io.written      # dropped before READY
    ev.q.put_nowait((phone.T_EVENT, b"CALL fg=3 ring=0 bg=0"))  # dialing -> open audio
    print("event:", await b.read_frame())
    t, p = await asyncio.wait_for(b.read_frame(), 5); print("event:", p); assert p.startswith(b"READY bluetooth")
    io.on_frame(b"\x01\x00" * 320)
    t, p = await b.read_frame(); assert t == phone.T_AUDIO and len(p) == 640
    b.send_audio(b"\x02\x00" * 480); b.flush(); await b.drain()
    assert len(io.written) == 960 and io.flushed == 1
    b.mute_mic(True)
    await ev.q.put((phone.T_EVENT, b"CALL fg=1 ring=0 bg=0")); assert (await b.read_frame())[1].startswith(b"CALL fg=1")
    assert io.started == 1  # not reopened
    # phone stream dies -> ERROR event
    async def boom(): raise asyncio.IncompleteReadError(b"", 3)
    ev.read_frame = boom
    b.pump.cancel(); b.pump = asyncio.create_task(b._pump_events())
    t, p = await b.read_frame(); print("event:", p); assert p.startswith(b"ERROR")
    await b.close(); assert ev.closed and io.closed
    print("bridge test OK")
asyncio.run(bridge_test())

# 4. macOS: bluetooth mode refuses clearly
if platform.system() == "Darwin":
    try:
        bluetooth.make_audio_io("", "Galaxy"); raise SystemExit("FAIL: no error on macOS")
    except phone.PhoneError as e: print("macOS error:", e)
print("ALL OK")
