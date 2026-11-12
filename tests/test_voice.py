"""Voice provider tests without a phone or real API keys.

  python tests/test_voice.py

1. OpenAIVoice against a local fake Realtime server: session settings, audio, events, tool results.
2. GeminiVoice builds a valid Live config.
3. A whole CallSession with a fake voice and a fake phone: answer, AI speech reaches the phone,
   transcript, end_call hangs up after the goodbye.
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ["CLAUDE_TELE_CONNECT_HOME"] = tempfile.mkdtemp()
os.environ["CLAUDE_TELE_CONNECT_OPENAI_API_KEY"] = "test-key"

import numpy as np
import websockets

from claude_tele_connect import agent, config, phone, voice


# ------------------------------------------------------------------ 1. OpenAI Realtime protocol

async def test_openai():
    got = {"appended": 0}

    async def server(ws):
        got["auth"] = ws.request.headers.get("Authorization")
        got["path"] = ws.request.path
        upd = json.loads(await ws.recv())
        got["session"] = upd["session"]
        await ws.send(json.dumps({"type": "session.updated", "session": upd["session"]}))
        async for raw in ws:
            ev = json.loads(raw)
            if ev["type"] == "input_audio_buffer.append":
                got["appended"] += len(base64.b64decode(ev["audio"]))
                if got["appended"] >= 4800:   # 0.1 s at 24 kHz: pretend they spoke, then reply
                    for e in ({"type": "input_audio_buffer.speech_started"},
                              {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Hello?"},
                              {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x00" * 480).decode()},
                              {"type": "response.output_audio_transcript.delta", "delta": "Hi, I'm Sam's AI assistant."},
                              {"type": "error", "error": {"code": "conversation_already_has_active_response", "message": "x"}},
                              {"type": "response.function_call_arguments.done", "call_id": "c1", "name": "end_call",
                               "arguments": json.dumps({"outcome": "said hello"})},
                              {"type": "response.done"}):
                        await ws.send(json.dumps(e))
                    got["appended"] = -10**9
            elif ev["type"] == "conversation.item.create":
                got.setdefault("items", []).append(ev["item"])
            elif ev["type"] == "response.create":
                got["response_create"] = got.get("response_create", 0) + 1

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        config.OPENAI_URL = f"ws://127.0.0.1:{port}/v1/realtime"
        v = voice.make_voice("openai", model="", voice="", instructions="be nice")
        await v.connect()
        assert got["auth"] == "Bearer test-key", got["auth"]
        assert got["path"] == "/v1/realtime?model=gpt-realtime-2.1", got["path"]
        s = got["session"]
        assert s["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert s["audio"]["output"]["voice"] == "marin"
        assert [t["name"] for t in s["tools"]] == ["end_call", "press_keys"] and s["tools"][0]["type"] == "function"

        await v.send_audio(b"\x00\x00" * 2400)
        await v.send_audio(b"\x00\x00" * 2400)
        events = []
        async def collect():
            async for ev in v.events():
                events.append(ev)
                if ev[0] == "turn_complete":
                    return
        await asyncio.wait_for(collect(), 5)
        kinds = [e[0] for e in events]
        assert kinds == ["interrupted", "them_text", "audio", "ai_text", "tool", "turn_complete"], kinds
        assert events[4] == ("tool", "c1", "end_call", {"outcome": "said hello"})

        await v.tool_result("c1", "end_call", {"result": "hanging up"}, silent=True)
        await v.tool_result("c2", "press_keys", {"result": "pressed 1"})
        await v.send_note("(System: greet them)")
        await asyncio.sleep(0.2)
        items = got["items"]
        assert items[0] == {"type": "function_call_output", "call_id": "c1", "output": '{"result": "hanging up"}'}
        assert items[2]["role"] == "user" and items[2]["content"][0]["text"] == "(System: greet them)"
        assert got["response_create"] == 2, got   # press_keys + note, not the silent end_call
        await v.close()
    print("ok  openai realtime protocol")


async def test_openai_rejects_bad_settings():
    async def server(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "error", "error": {"code": "invalid_value", "message": "bad voice"}}))
        await asyncio.sleep(1)
    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        config.OPENAI_URL = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/v1/realtime"
        v = voice.make_voice("openai", model="", voice="nope", instructions="")
        try:
            await v.connect()
        except RuntimeError as e:
            assert "bad voice" in str(e)
        else:
            raise AssertionError("expected an error")
        await v.close()
    print("ok  openai bad settings fail before dialing")


# ------------------------------------------------------------------ 2. Gemini config

def test_gemini_config():
    v = voice.make_voice("gemini", model="", voice="", instructions="be nice")
    c = v._config()
    assert v.model == "gemini-3.8-live" and v.voice == "Kore"
    names = [d.name for d in c.tools[0].function_declarations]
    assert names == ["end_call", "press_keys"], names
    assert c.tools[0].function_declarations[0].parameters.required == ["outcome"]
    print("ok  gemini live config")


# ------------------------------------------------------------------ 3. CallSession end to end

class FakeVoice:
    name, model, in_rate = "Fake", "fake-1", 24000

    def __init__(self):
        self.q = asyncio.Queue()
        self.audio_in = 0
        self.tool_results = []

    async def connect(self): pass
    async def close(self): pass

    async def send_audio(self, pcm):
        self.audio_in += len(pcm)

    async def send_note(self, text):
        if "connected" in text:   # greet nudge -> speak, then hang up
            for ev in (("audio", b"\x10\x00" * 2400), ("ai_text", "Hello, I'm Sam's AI assistant. Bye!"),
                       ("tool", "t1", "end_call", {"outcome": "test ok"}), ("turn_complete",)):
                await self.q.put(ev)

    async def events(self):
        while True:
            yield await self.q.get()

    async def tool_result(self, call_id, name, result, silent=False):
        self.tool_results.append((call_id, name, result, silent))


class FakeBridge:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = 0
        for f in ((phone.T_EVENT, b"LISTENER ok"), (phone.T_EVENT, b"CALL fg=3 ring=0"),
                  (phone.T_EVENT, b"CALL fg=3 ring=0")):
            self.frames.put_nowait(f)

    async def read_frame(self): return await self.frames.get()
    def send_audio(self, pcm): self.sent += len(pcm)
    async def drain(self): pass
    def flush(self): pass
    def mute_mic(self, on): pass
    async def close(self): pass


async def test_call_session():
    hung = []
    bridge = FakeBridge()

    async def start_bridge(*a, **k): return bridge
    async def hang_up(): hung.append(1); bridge.frames.put_nowait((phone.T_EVENT, b"CALL fg=0 ring=0"))
    async def call_state(): return phone.CALL_IDLE if hung else phone.CALL_ACTIVE
    phone.start_bridge, phone.hang_up, phone.call_state = start_bridge, hang_up, call_state

    s = agent.CallSession("Test", "+10000000000", "say hello", owner="Sam", log=lambda *a: None)
    fv = FakeVoice()
    s.voice = fv
    s.to_voice = agent.bluetooth.Resampler(agent.IN_RATE, fv.in_rate)
    await s.prepare()
    bridge.frames.put_nowait((phone.T_EVENT, f"CALL fg={phone.CALL_ACTIVE} ring=0".encode()))
    bridge.frames.put_nowait((phone.T_EVENT, b"READY"))
    for _ in range(10):  # 400 ms of quiet line audio, 16 kHz
        bridge.frames.put_nowait((phone.T_AUDIO, np.zeros(640, "<i2").tobytes()))
    assert await s.wait_answered(3)
    r = await asyncio.wait_for(s.converse(), 15)
    r = await s.close()
    assert r["ended_by"] == "agent" and r["outcome"] == "test ok", r
    assert hung, "never hung up"
    assert bridge.sent >= 4800, bridge.sent                     # AI audio reached the phone
    assert 0 < fv.audio_in and fv.audio_in % 2 == 0             # their audio reached the voice, resampled
    assert fv.tool_results == [("t1", "end_call", {"result": "hanging up"}, True)]
    assert s.transcript.lines and s.transcript.lines[0][1] == "AI"
    assert "- Voice: " in Path(r["transcript_file"]).read_text()
    print("ok  call session end to end (fake voice + fake phone)")


async def main():
    await test_openai()
    await test_openai_rejects_bad_settings()
    test_gemini_config()
    await test_call_session()


if __name__ == "__main__":
    asyncio.run(main())
