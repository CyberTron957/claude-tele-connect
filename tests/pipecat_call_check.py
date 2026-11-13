"""A simulated call through a real Pipecat pipeline, without a phone: a synthesized "bakery" answers,
the AI asks about sourdough, gets the answer, says goodbye and hangs up.

  python tests/pipecat_call_check.py [stt] [llm] [tts]      # default: whisper vertex kokoro

Needs the chosen services installed and configured, and macOS `say` + `afconvert` to make the caller audio.
Prints the transcript, outcome and response timing.
"""
import asyncio, os, subprocess, sys, time, wave, tempfile
from pathlib import Path
tmp = Path(tempfile.mkdtemp())
os.environ["CLAUDE_TELE_CONNECT_CALLS_DIR"] = str(tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from claude_tele_connect import agent, phone, config
from test_voice import FakeBridge
config.STT, config.LLM, config.TTS = (sys.argv[1:4] + ["whisper", "vertex", "kokoro"][len(sys.argv[1:4]):])
for name, text in (("caller", "Hello, this is Sunrise Bakery. How can I help you?"),
                   ("c2", "Yes, we have sourdough today. We close at six. Goodbye!")):
    subprocess.run(["say", "-o", str(tmp / f"{name}.aiff"), text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(tmp / f"{name}.aiff"),
                    str(tmp / f"{name}.wav")], check=True)

async def main():
    hung = []; bridge = FakeBridge()
    async def start_bridge(*a, **k): return bridge
    async def hang_up(): hung.append(time.time()); bridge.frames.put_nowait((phone.T_EVENT, b"CALL fg=0 ring=0"))
    async def call_state(): return phone.CALL_IDLE if hung else phone.CALL_ACTIVE
    phone.start_bridge, phone.hang_up, phone.call_state = start_bridge, hang_up, call_state
    s = agent.CallSession("Bakery", "+10000000000", "Ask if they have sourdough bread today, then say goodbye and end the call.",
                          owner="Sam", provider="pipeline", max_minutes=2)
    await s.prepare()
    bridge.frames.put_nowait((phone.T_EVENT, f"CALL fg={phone.CALL_ACTIVE} ring=0".encode()))
    bridge.frames.put_nowait((phone.T_EVENT, b"READY"))
    async def say(path):
        pcm = wave.open(str(path)).readframes(10**9)
        for i in range(0, len(pcm), 640):
            bridge.frames.put_nowait((phone.T_AUDIO, pcm[i:i+640])); await asyncio.sleep(0.02)
    async def caller():
        await asyncio.sleep(0.3); await say(tmp / "caller.wav")
        # wait until the AI has spoken and gone quiet, then answer
        while not s.transcript.lines or s.transcript.lines[-1][1] != "AI" or time.time() - s.uplink.last_audio < 1.0:
            bridge.frames.put_nowait((phone.T_AUDIO, b"\0" * 640)); await asyncio.sleep(0.02)
        print(f"  [caller turn 2 at {time.time()-s.t_answer:.1f}s]")
        await say(tmp / "c2.wav")
        while True:
            bridge.frames.put_nowait((phone.T_AUDIO, b"\0" * 640)); await asyncio.sleep(0.02)
    assert await s.wait_answered(3)
    t = asyncio.create_task(caller())
    await asyncio.wait_for(s.converse(), 120)
    t.cancel()
    r = await s.close()
    for k in ("voice", "outcome", "ended_by", "duration_s", "timing"): print(k, "=", r.get(k))
asyncio.run(main())
