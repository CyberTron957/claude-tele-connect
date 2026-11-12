"""Linux Bluetooth-mode audio test, run inside a container by tests/linux_audio_test.sh.
"""
# Linux PulseAudioIO test. "Phone" input = a real-time 1 kHz sine source named like a Bluetooth node;
# "phone" output = a null sink whose monitor is recorded to a file for analysis afterwards.
import asyncio, subprocess, sys, time
import numpy as np
sys.path.insert(0, "/app/src")
from ai_phone import bluetooth, phone

MAC = "AA:BB:CC:DD:EE:FF"
print("find by MAC hint:", bluetooth.linux_find([MAC]))

def tone(f, rate, secs):
    t = np.arange(int(rate * secs)) / rate
    return (8000 * np.sin(2 * np.pi * f * t)).astype("<i2").tobytes()

def peak(x, rate):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / rate)[spec.argmax()]

async def paced_write(io, pcm):
    t0 = time.time()
    for i in range(0, len(pcm), 960):
        io.write(pcm[i:i + 960]); await io.drain()
        await asyncio.sleep(max(0, t0 + (i + 960) / 48000 - 0.12 - time.time()))

async def main():
    rec = subprocess.Popen(["parec", "--device=bluez_output.AA_BB_CC_DD_EE_FF.1.monitor", "--format=s16le",
                            "--rate=16000", "--channels=1", "--raw", "--latency-msec=20"],
                           stdout=open("/tmp/out.raw", "wb"))
    time.sleep(1.0)  # let the virtual sink settle at low latency before measuring
    io = bluetooth.make_audio_io(MAC, "")
    frames, errors = [], []
    await io.start(lambda p: frames.append((time.time(), p)), errors.append)
    t_start = time.time()
    # A: 2.0 s of the 1 kHz tone (phone -> us) while we play a 700 Hz tone (us -> phone)
    await paced_write(io, tone(700, 24000, 2.0))
    await asyncio.sleep(0.3)
    span = time.time() - t_start
    a = [p for t, p in frames]
    x = np.frombuffer(b"".join(a), "<i2").astype(float)
    gaps = np.diff([t for t, _ in frames])
    print(f"input: {len(a)} frames in {span:.2f} s (expect ~{span*50:.0f}), all 640 B: {all(len(p)==640 for p in a)}, "
          f"peak {peak(x[1600:], 16000):.0f} Hz, max gap between frames {gaps.max()*1000:.0f} ms")
    assert abs(peak(x[1600:], 16000) - 1000) < 15
    # B: barge-in. Queue 3 s of 500 Hz, flush after 0.5 s; then a short 300 Hz tone must still play.
    t_b = time.time()
    w = asyncio.create_task(paced_write(io, tone(500, 24000, 3.0)))   # paced like the agent's Uplink
    await asyncio.sleep(0.5); w.cancel(); io.flush(); t_flush = time.time()
    await asyncio.sleep(1.5)
    await paced_write(io, tone(300, 24000, 0.6)); await asyncio.sleep(0.5)
    await io.close(); rec.terminate(); rec.wait()
    print("errors:", errors)
    y = np.frombuffer(open("/tmp/out.raw", "rb").read(), "<i2").astype(float)
    # find segments by 100 ms windows: dominant frequency per window
    win = 1600; segs = []
    for i in range(0, len(y) - win, win):
        w = y[i:i + win]; r = np.sqrt(np.mean(w ** 2))
        segs.append(int(round(peak(w, 16000), -2)) if r > 1000 else 0)
    print("output, dominant Hz per 100 ms:", segs)
    n700, n500, n300 = segs.count(700), segs.count(500), segs.count(300)
    print(f"700 Hz for {n700/10:.1f} s (sent 2.0), 500 Hz for {n500/10:.1f} s before flush (sent 0.5 then flushed 2.5 s), "
          f"300 Hz for {n300/10:.1f} s (sent 0.6)")
    assert 17 <= n700 <= 22 and n500 <= 9 and n300 >= 4
    print("LINUX TEST OK")
asyncio.run(main())
