"""One phone call with an AI voice (Gemini Live or OpenAI Realtime, see voice.py). Call audio comes from
the phone's CallAudio bridge
(audio mode "phone") or from the computer's Bluetooth hands-free device (audio mode "bluetooth").

Lifecycle (everything slow happens BEFORE the other side picks up):
    s = CallSession(...); await s.prepare()     # voice provider connected + phone bridge running
    await phone.dial(number)                     # (or wait for an incoming call)
    if await s.wait_answered(45): result = await s.converse()
    await s.close()
"""

import asyncio
import math
import statistics
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from . import bluetooth, config, phone, voice
from .voice import OUT_RATE  # AI voice -> phone uplink

IN_RATE = 16000    # phone downlink (both audio modes) -> AI voice, resampled if the provider wants another rate
FRAME_BYTES = OUT_RATE // 50 * 2   # 20 ms of 16-bit mono
LEAD_S = 0.12      # how far ahead of real time we keep the phone's playback queue
VOICE_RMS = 400    # downlink RMS above this counts as the other person speaking (line silence is ~5-50)
GREET_WAIT_S = 1.0 # outbound: if they haven't said "hello" within this long after answering, greet first


def system_prompt(goal: str, context: str, owner: str, direction: str) -> str:
    if config.OWNER_PRONOUNS:
        pronoun_rule = f"When a pronoun is needed for {owner}, use {config.OWNER_PRONOUNS}."
    else:
        pronoun_rule = (f"Never use a gendered pronoun for {owner} in any language (no he/she; in Telugu never "
                        f"ఆయన, ఆవిడ, అతను, ఆమె, అతడు; in Hindi avoid gendered verb endings where you can); "
                        f"repeat the name '{owner}' instead.")
    if direction == "outbound":
        opener = f"You are an AI voice assistant making a phone call on behalf of {owner}."
        start = (f"Your first turn must be short and in one breath: a greeting plus who you are, e.g. "
                 f"'Hello, I'm {owner}'s AI assistant.' Then say why you're calling in the next sentence.")
    else:
        opener = f"You are an AI voice assistant answering {owner}'s phone because {owner} is unavailable."
        start = (f"Answer like a receptionist: 'Hello, this is {owner}'s AI assistant. How can I help?' "
                 f"in one short breath.")
    return f"""{opener}

Goal of this call: {goal}

How to behave:
- {start}
- Talk like a polite, natural person on the phone: short turns (one or two sentences), then let them speak. Don't monologue or list things.
- Reply in the language the other person speaks (English, Hindi, Telugu, etc.). If unsure, use simple English.
- Stay on the goal. Do not agree to payments, share personal details, or make commitments beyond what the goal and context allow. If asked something you don't know, say you'll check with {owner} and get back to them.
- If you reach voicemail, leave a brief message with the purpose and that {owner} will follow up, then end the call.
- If you hit an automated menu that asks for key presses, use the press_keys tool.
- If the person is busy, upset, or asks you to stop, apologise briefly, say goodbye and end the call.
- Refer to {owner} only by name. {pronoun_rule}
- Make sure you actually got a clear answer before wrapping up. If a reply was unclear, garbled, or a question back to you, answer it and ask again (up to twice). Never assume or claim the person agreed unless they clearly said so.
- When the goal is achieved or the conversation is clearly over, say a short goodbye first, then call end_call. Never hang up mid-sentence.
- The outcome you pass to end_call must be strictly factual: what the person actually said or agreed to, in English. If their answer was unclear or they declined, say exactly that.

Extra context from {owner}: {context or "none"}
"""


DTMF = {"1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "4": (770, 1209), "5": (770, 1336),
        "6": (770, 1477), "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "*": (941, 1209),
        "0": (941, 1336), "#": (941, 1477)}


def dtmf_pcm(digits: str) -> bytes:
    out = bytearray()
    for d in digits:
        if d not in DTMF:
            continue
        f1, f2 = DTMF[d]
        for i in range(int(OUT_RATE * 0.2)):
            v = int(8000 * (math.sin(2 * math.pi * f1 * i / OUT_RATE) + math.sin(2 * math.pi * f2 * i / OUT_RATE)) / 2)
            out += v.to_bytes(2, "little", signed=True)
        out += bytes(int(OUT_RATE * 0.15) * 2)
    return bytes(out)


@dataclass
class Transcript:
    lines: list = field(default_factory=list)   # (t, speaker, text)
    start: float = field(default_factory=time.time)
    _speaker: str = ""
    _buf: str = ""
    _t: float = 0.0

    def add(self, speaker: str, text: str) -> None:
        if speaker != self._speaker:
            self.flush()
            self._speaker, self._t = speaker, time.time() - self.start
        self._buf += text

    def flush(self) -> None:
        text = " ".join(self._buf.split())
        if self._speaker and text:
            self.lines.append((self._t, self._speaker, text))
            print(f"  {self._t:5.1f}s  {self._speaker:>4}: {text}", flush=True)
        self._speaker, self._buf = "", ""


class Uplink:
    """Paces AI audio into the call so the playback queue stays short (fast barge-in)."""

    def __init__(self, bridge, recorder=None):
        self.bridge, self.recorder = bridge, recorder
        self.buf = bytearray()
        self.wake = asyncio.Event()
        self.reset = False
        self.last_audio = 0.0

    def add(self, pcm: bytes) -> None:
        self.buf += pcm
        self.last_audio = time.time()
        self.wake.set()

    def interrupt(self) -> None:
        self.buf.clear()
        self.bridge.flush()
        self.reset = True

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        t0, sent = None, 0.0
        while True:
            if self.reset:
                t0, self.reset = None, False
            if len(self.buf) < 2:
                t0 = None
                self.wake.clear()
                await self.wake.wait()
                continue
            now = loop.time()
            if t0 is None:
                t0, sent = now, 0.0
            ahead = sent - (now - t0)
            if ahead > LEAD_S:
                await asyncio.sleep(ahead - LEAD_S)
                continue
            n = min(FRAME_BYTES, len(self.buf) // 2 * 2)
            chunk = bytes(self.buf[:n])
            del self.buf[:n]
            self.bridge.send_audio(chunk)
            if self.recorder:
                self.recorder.writeframes(chunk)
            await self.bridge.drain()
            sent += n / (2 * OUT_RATE)

    async def drain(self, timeout: float = 20) -> None:
        end = time.time() + timeout
        while self.buf and time.time() < end:
            await asyncio.sleep(0.05)
        await asyncio.sleep(LEAD_S + 0.4)  # let the phone play out its last queued audio


class CallSession:
    def __init__(self, label: str, number: str, goal: str, context: str = "", *, direction: str = "outbound",
                 owner: str = config.OWNER_NAME, provider: str = "", voice_name: str = config.VOICE, model: str = config.MODEL,
                 max_minutes: float = 5, record: bool = False, mute_mic: bool = False,
                 audio: str = config.AUDIO, bt_device: str = config.BT_DEVICE, log=print):
        self.label, self.number, self.goal, self.context = label, number, goal, context
        self.direction, self.owner = direction, owner
        self.provider = provider or voice.default_provider()
        self.voice = voice.make_voice(self.provider, model=model, voice=voice_name,
                                      instructions=system_prompt(goal, context, owner, direction))
        self.max_minutes, self.record, self.mute_mic, self.log = max_minutes, record, mute_mic, log
        self.audio, self.bt_device = audio, bt_device
        config.CALLS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() else "_" for c in label)[:40]
        self.base = config.CALLS_DIR / f"{stamp}-{safe}"
        self.transcript = Transcript()
        self.result = {"label": label, "number": number, "goal": goal, "direction": direction, "audio": audio,
                       "voice": f"{self.provider}/{self.voice.model}",
                       "outcome": None,
                       "ended_by": None, "started": datetime.now().isoformat(timespec="seconds")}
        # call state, from the phone's listener
        self.fg, self.ring = -1, -1
        self.listener_ok = False
        self.seen_call = False
        self.answered, self.ended, self.ready, self.done = (asyncio.Event() for _ in range(4))
        self.them_voice = asyncio.Event()
        self.live = False
        # timing
        self.t_answer = None
        self.t = {}                 # named milestones (seconds after answer)
        self.last_them_voice = 0.0
        self.ai_turn_open = False
        self.closing = self.closing_turn_done = self.goodbye_spoken = False
        self.latencies = []         # their end-of-speech -> first AI audio, seconds
        self.down_wav = self.up_wav = None
        self.bridge = self.uplink = None
        self.connected = False
        self.to_voice = bluetooth.Resampler(IN_RATE, self.voice.in_rate)
        self._tasks = []

    # ---------- setup ----------

    async def prepare(self) -> None:
        """Connect the voice provider and start the audio bridge (in parallel) before the call connects."""
        if self.record:
            self.down_wav = wave.open(str(self.base) + "-them.wav", "wb")
            self.down_wav.setnchannels(1); self.down_wav.setsampwidth(2); self.down_wav.setframerate(IN_RATE)
            self.up_wav = wave.open(str(self.base) + "-ai.wav", "wb")
            self.up_wav.setnchannels(1); self.up_wav.setsampwidth(2); self.up_wav.setframerate(OUT_RATE)
        t0 = time.time()
        if self.audio == "bluetooth":
            bridge = bluetooth.start_bridge(self.bt_device, log=self.log)
        else:
            bridge = phone.start_bridge(OUT_RATE, log=lambda s: None)
        _, self.bridge = await asyncio.gather(self.voice.connect(), bridge)
        self.connected = True
        self.uplink = Uplink(self.bridge, self.up_wav)
        self._tasks.append(asyncio.create_task(self._read_phone()))
        # give the listener a moment to report the current call state
        for _ in range(20):
            if self.listener_ok or self.fg != -1:
                break
            await asyncio.sleep(0.05)
        self.log(f"Ready in {time.time() - t0:.1f}s ({self.voice.name} {self.voice.model} connected, {self.audio} audio bridge up, "
                 f"call-state listener {'ok' if self.listener_ok else 'unavailable -> polling'})")

    def _ms(self, name: str) -> None:
        if name not in self.t and self.t_answer:
            self.t[name] = time.time() - self.t_answer

    def _on_call_state(self, fg: int, ring: int) -> None:
        self.fg, self.ring = fg, ring
        if fg in (phone.CALL_DIALING, phone.CALL_ALERTING, phone.CALL_ACTIVE) or ring in (5, 6):
            self.seen_call = True
        if fg == phone.CALL_ACTIVE and not self.answered.is_set():
            self.t_answer = time.time()
            self.transcript.start = self.t_answer
            self.live = True              # forward their audio to the AI from the first frame
            self.answered.set()
        idle = lambda v: v in (phone.CALL_IDLE, 7, 8, -1)
        if self.seen_call and idle(fg) and idle(ring):
            self.ended.set()

    async def _read_phone(self) -> None:
        batch = bytearray()
        while True:
            t, payload = await self.bridge.read_frame()
            if t == phone.T_AUDIO:
                if self.down_wav:
                    self.down_wav.writeframes(payload)
                x = np.frombuffer(payload, dtype="<i2").astype(np.float32)
                if x.size and math.sqrt(float(np.mean(x * x))) > VOICE_RMS:
                    self.last_them_voice = time.time()
                    if self.live and not self.them_voice.is_set():
                        self._ms("them_first_voice")
                        self.them_voice.set()
                if self.live:
                    batch += payload
                    if len(batch) >= IN_RATE // 25 * 2:  # 40 ms
                        await self.voice.send_audio(self.to_voice.process(bytes(batch)))
                        batch.clear()
            elif t == phone.T_EVENT:
                text = payload.decode(errors="replace")
                if text.startswith("CALL "):
                    kv = dict(p.split("=") for p in text[5:].split())
                    self._on_call_state(int(kv.get("fg", -1)), int(kv.get("ring", -1)))
                elif text.startswith("LISTENER"):
                    self.listener_ok = text.endswith("ok")
                elif text.startswith("READY"):
                    self._ms("audio_ready")
                    self.ready.set()
                elif text.startswith("ERROR"):
                    self.log(f"  [phone] {text}")
                    self.result["ended_by"] = self.result["ended_by"] or f"bridge {text}"
                    self.ended.set()
                else:
                    self.log(f"  [phone] {text}")

    async def _poll_state_fallback(self, interval: float) -> None:
        """Used only if the phone's call-state listener is unavailable (or as a slow safety net)."""
        while True:
            try:
                st = await phone.call_state()
                ring = await phone.ringing_state() if not self.listener_ok else self.ring
                if not self.listener_ok or (self.seen_call and st != self.fg):
                    self._on_call_state(st, ring)
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def wait_answered(self, timeout: float) -> bool:
        poll = None if self.listener_ok else asyncio.create_task(self._poll_state_fallback(0.3))
        try:
            done, _ = await asyncio.wait(
                [asyncio.create_task(self.answered.wait()), asyncio.create_task(self.ended.wait())],
                timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if poll:
                poll.cancel()
        return self.answered.is_set()

    # ---------- the conversation ----------

    async def converse(self) -> dict:
        try:
            await asyncio.wait_for(self.ready.wait(), 8)
        except asyncio.TimeoutError:
            self.result["ended_by"] = "phone audio never became ready"
            return self.result
        if self.mute_mic:
            self.bridge.mute_mic(True)

        tasks = [asyncio.create_task(c()) for c in
                 (self._voice_to_phone, self.uplink.run, self._time_limit, self._greet_if_silent)]
        tasks.append(asyncio.create_task(self._poll_state_fallback(0.4 if not self.listener_ok else 3.0)))
        watched = [self._tasks[0], tasks[0], tasks[1]]  # phone reader, voice reader, uplink
        stop = [asyncio.create_task(self.ended.wait()), asyncio.create_task(self.done.wait())]
        self._tasks += tasks + stop
        finished, _ = await asyncio.wait(watched + stop, return_when=asyncio.FIRST_COMPLETED)
        for t in finished:
            if t in watched and not t.cancelled() and t.exception():
                self.log(f"  [agent] task failed: {t.exception()!r}")
                self.result["ended_by"] = self.result["ended_by"] or f"error: {t.exception()!r}"
        if self.ended.is_set() and not self.result["ended_by"]:
            self.result["ended_by"] = "other side hung up"
        return self.result

    async def _voice_to_phone(self) -> None:
        async for ev in self.voice.events():
            kind = ev[0]
            # After end_call + goodbye, ignore any further AI turns (e.g. "I've ended the call").
            drop = self.closing and self.closing_turn_done and self.goodbye_spoken
            if kind == "interrupted":
                self.uplink.interrupt()
                self.ai_turn_open = False
            elif kind == "audio" and not drop:
                self._on_ai_audio()
                self.uplink.add(ev[1])
            elif kind == "them_text" and ev[1]:
                self.transcript.add("Them", ev[1])
            elif kind == "ai_text" and ev[1] and not drop:
                self.transcript.add("AI", ev[1])
            elif kind == "turn_complete":
                self.transcript.flush()
                self.ai_turn_open = False
                if self.closing:
                    self.closing_turn_done = True
            elif kind == "tool":
                await self._handle_tool(*ev[1:])
            elif kind == "info":
                self.log(f"  [{self.provider}] {ev[1]}")

    def _on_ai_audio(self) -> None:
        if self.ai_turn_open:
            return
        self.ai_turn_open = True
        self._ms("ai_first_audio")
        now = time.time()
        if self.last_them_voice and now - self.last_them_voice < 6:
            self.latencies.append(now - self.last_them_voice)

    async def _handle_tool(self, call_id, name, args: dict) -> None:
        if name == "end_call":
            self.result["outcome"] = args.get("outcome")
            self.log(f"  [agent] end_call: {self.result['outcome']}")
            self.closing = True
            self.goodbye_spoken = self.ai_turn_open or time.time() - self.uplink.last_audio < 3
            await self.voice.tool_result(call_id, name, {"result": "hanging up"}, silent=True)
            self._tasks.append(asyncio.create_task(self._hang_up_after_goodbye()))
        elif name == "press_keys":
            digits = str(args.get("digits", ""))
            self.log(f"  [agent] press_keys: {digits}")
            self.uplink.add(dtmf_pcm(digits))
            await self.voice.tool_result(call_id, name, {"result": f"pressed {digits}"})
        else:
            await self.voice.tool_result(call_id, name, {"error": "unknown tool"})

    async def _hang_up_after_goodbye(self) -> None:
        # end_call can arrive before the goodbye audio has streamed in: wait for the AI to go quiet.
        end = time.time() + 15
        await asyncio.sleep(0.3)
        while time.time() < end and (self.uplink.buf or time.time() - self.uplink.last_audio < 1.2):
            await asyncio.sleep(0.1)
        await self.uplink.drain()
        await phone.hang_up()
        self.result["ended_by"] = self.result["ended_by"] or "agent"
        self.done.set()

    async def _time_limit(self) -> None:
        await asyncio.sleep(max(self.max_minutes * 60 - 25, 0))
        await self.voice.send_note(
            "(System: the call time limit is almost reached. Wrap up politely now, say goodbye, and call end_call.)")
        await asyncio.sleep(25)
        self.log("  [agent] time limit reached, hanging up")
        await self.uplink.drain(timeout=3)
        await phone.hang_up()
        self.result["ended_by"] = self.result["ended_by"] or "time limit"
        self.done.set()

    async def _greet_if_silent(self) -> None:
        if self.direction == "outbound":
            try:  # most people say "hello" first; let the AI answer that naturally
                await asyncio.wait_for(self.them_voice.wait(), GREET_WAIT_S)
                return
            except asyncio.TimeoutError:
                pass
        self._ms("greet_nudge")
        await self.voice.send_note(
            "(System: the call is connected. Greet the person and introduce yourself now.)")

    # ---------- teardown ----------

    async def close(self) -> dict:
        for t in self._tasks:
            t.cancel()
        self.transcript.flush()
        try:
            if await phone.call_state() in (phone.CALL_ACTIVE, phone.CALL_HOLDING, phone.CALL_DIALING, phone.CALL_ALERTING):
                await phone.hang_up()
        except Exception:
            pass
        if self.bridge:
            await self.bridge.close()
        if self.connected:
            try:
                await self.voice.close()
            except Exception:
                pass
        for w in (self.down_wav, self.up_wav):
            if w:
                w.close()
        if self.t_answer:
            self.result["duration_s"] = round(time.time() - self.t_answer, 1)
            self.result["timing"] = {k: round(v, 2) for k, v in self.t.items()}
            if self.latencies:
                self.result["timing"]["response_median_s"] = round(statistics.median(self.latencies), 2)
                self.result["timing"]["response_max_s"] = round(max(self.latencies), 2)
            self.result["transcript_file"] = str(self.base) + ".md"
            write_transcript(Path(self.result["transcript_file"]), self.result, self.transcript)
        return self.result


def write_transcript(path: Path, result: dict, transcript: Transcript) -> None:
    lines = [f"# Call: {result['label']} ({result['number']})", "",
             f"- Direction: {result['direction']}", f"- Audio: {result.get('audio')}", f"- Voice: {result.get('voice')}", f"- Started: {result['started']}",
             f"- Duration: {result.get('duration_s')} s", f"- Goal: {result['goal']}",
             f"- Outcome: {result.get('outcome') or '(not reported)'}", f"- Ended by: {result.get('ended_by')}",
             f"- Timing (s after answer): {result.get('timing')}", "", "## Transcript", ""]
    lines += [f"- **{spk}** ({t:.0f}s): {text}" for t, spk, text in transcript.lines] or ["(no speech transcribed)"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
