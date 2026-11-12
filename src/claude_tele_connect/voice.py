"""Speech-to-speech voice providers. Each one turns phone audio into the AI's spoken replies.

Every provider has the same small interface, so the call logic in agent.py doesn't care which one runs:

    v = make_voice(provider, model=..., voice=..., instructions=..., tools=TOOLS)
    await v.connect()
    await v.send_audio(pcm)              # 16-bit mono at v.in_rate (the call session resamples)
    await v.send_note("(System: ...)")   # a nudge the caller doesn't hear, e.g. "greet them now"
    async for ev in v.events(): ...      # see below
    await v.tool_result(call_id, name, {"result": ...}, silent=False)
    await v.close()

Events are tuples:
    ("audio", pcm)          AI speech, 16-bit mono at OUT_RATE (24 kHz)
    ("interrupted",)        the other person started talking: drop queued AI audio
    ("them_text", text)     transcript of the other person
    ("ai_text", text)       transcript of the AI
    ("turn_complete",)      the AI finished a turn
    ("tool", id, name, args)
    ("info", text)          something worth logging
"""

import asyncio
import base64
import json

from . import config

OUT_RATE = 24000   # every provider below speaks 24 kHz 16-bit mono PCM

PROVIDERS = {
    # name: (default model, default voice, what it needs). "" = chosen by the service / pipeline.
    "gemini": ("gemini-3.8-live", "Kore", "a Gemini API key (free tier available) or a Vertex AI project"),
    "openai": ("gpt-realtime-2.1", "marin", "an OpenAI API key (paid)"),
    # Through Pipecat (pipecat_voice.py, optional extra):
    "grok": ("", "", "an xAI API key (CLAUDE_TELE_CONNECT_XAI_API_KEY)"),
    "pipeline": ("", "", "speech-to-text, LLM and text-to-speech services of your choice"),
    "local": ("", "", "nothing paid: Whisper, Ollama and Kokoro running on this computer"),
}
PIPECAT = ("grok", "pipeline", "local")

# Tools the AI can use on the call, as plain JSON schema (each provider converts them).
TOOLS = [
    {"name": "end_call",
     "description": "Hang up the phone. Call this only after you have said goodbye.",
     "parameters": {"type": "object", "required": ["outcome"], "properties": {
         "outcome": {"type": "string", "description": "Factual one-line summary of what happened / was agreed."}}}},
    {"name": "press_keys",
     "description": "Send keypad tones (DTMF) into the call, e.g. to navigate an automated phone menu.",
     "parameters": {"type": "object", "required": ["digits"], "properties": {
         "digits": {"type": "string", "description": "Keys to press: 0-9, * or #."}}}},
]


def default_provider() -> str:
    if config.PROVIDER:
        return config.PROVIDER
    if not config.gemini_backend() and config.OPENAI_API_KEY:
        return "openai"
    return "gemini"


def configured(provider: str) -> str:
    """'' if the provider has what it needs, else a sentence saying what's missing."""
    if provider == "gemini":
        return "" if config.gemini_backend() else "Gemini isn't configured: run `claude-tele-connect setup`"
    if provider == "openai":
        return "" if config.OPENAI_API_KEY else "OpenAI isn't configured: run `claude-tele-connect setup --provider openai`"
    if provider in PIPECAT:
        from . import pipecat_voice
        probs = pipecat_voice.PipecatVoice(provider, model="", voice="", instructions="", tools=TOOLS).problems()
        return "; ".join(probs)
    return f"unknown voice provider '{provider}' (choose from: {', '.join(PROVIDERS)})"


def make_voice(provider: str, *, model: str, voice: str, instructions: str, tools=TOOLS, stt="", llm="", tts=""):
    if provider in PIPECAT:
        from .pipecat_voice import PipecatVoice
        return PipecatVoice(provider, model=model, voice=voice, instructions=instructions, tools=tools,
                            stt=stt, llm=llm, tts=tts)
    cls = {"gemini": GeminiVoice, "openai": OpenAIVoice}[provider]
    default_model, default_voice, _ = PROVIDERS[provider]
    return cls(model or default_model, voice or default_voice, instructions, tools)


# ------------------------------------------------------------------------------------------- Gemini Live

class GeminiVoice:
    name = "Gemini"
    in_rate = 16000

    def __init__(self, model, voice, instructions, tools):
        self.model, self.voice, self.instructions, self.tools = model, voice, instructions, tools
        self.session = self._cm = None

    def _client(self):
        from google import genai
        if config.gemini_backend() == "api_key":  # Google AI Studio key
            return genai.Client(api_key=config.GEMINI_API_KEY, http_options={"api_version": "v1beta"})
        import google.auth
        if config.ACCESS_TOKEN:  # short-lived token (e.g. from `gcloud auth print-access-token`), expires in ~1 h
            import google.oauth2.credentials
            creds = google.oauth2.credentials.Credentials(config.ACCESS_TOKEN, quota_project_id=config.GCP_PROJECT)
        else:
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds = creds.with_quota_project(config.GCP_PROJECT)
        return genai.Client(vertexai=True, project=config.GCP_PROJECT, location=config.GCP_LOCATION, credentials=creds)

    def _config(self):
        from google.genai import types
        decls = [types.FunctionDeclaration(
            name=t["name"], description=t["description"],
            parameters=types.Schema(type="OBJECT", required=t["parameters"]["required"], properties={
                k: types.Schema(type=p["type"].upper(), description=p.get("description"))
                for k, p in t["parameters"]["properties"].items()}))
            for t in self.tools]
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=self.instructions,
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice))),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Snappier turn-taking than the default: respond after ~0.5 s of silence.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=500, prefix_padding_ms=100)),
            tools=[types.Tool(function_declarations=decls)],
        )

    async def connect(self):
        self._cm = self._client().aio.live.connect(model=self.model, config=self._config())
        self.session = await self._cm.__aenter__()

    async def send_audio(self, pcm: bytes):
        from google.genai import types
        await self.session.send_realtime_input(audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={self.in_rate}"))

    async def send_note(self, text: str):
        await self.session.send_realtime_input(text=text)

    async def events(self):
        while True:
            async for msg in self.session.receive():
                sc = msg.server_content
                if sc:
                    if sc.interrupted:
                        yield ("interrupted",)
                    if sc.model_turn:
                        for p in sc.model_turn.parts:
                            if p.inline_data and p.inline_data.data:
                                yield ("audio", p.inline_data.data)
                    if sc.input_transcription and sc.input_transcription.text:
                        yield ("them_text", sc.input_transcription.text)
                    if sc.output_transcription and sc.output_transcription.text:
                        yield ("ai_text", sc.output_transcription.text)
                    if sc.turn_complete:
                        yield ("turn_complete",)
                if msg.tool_call:
                    for fc in msg.tool_call.function_calls:
                        yield ("tool", fc.id, fc.name, dict(fc.args or {}))
                if msg.go_away:
                    yield ("info", f"go_away: {msg.go_away.time_left}")

    async def tool_result(self, call_id, name, result: dict, silent=False):
        from google.genai import types
        r = types.FunctionResponse(id=call_id, name=name, response=result)
        if silent:
            r.scheduling = types.FunctionResponseScheduling.SILENT
        await self.session.send_tool_response(function_responses=[r])

    async def close(self):
        if self._cm and self.session:
            await self._cm.__aexit__(None, None, None)


# ----------------------------------------------------------------------------------- OpenAI Realtime

class OpenAIVoice:
    """OpenAI Realtime API over a WebSocket. CLAUDE_TELE_CONNECT_OPENAI_URL can point at another endpoint that
    speaks the same protocol (e.g. Azure OpenAI)."""

    name = "OpenAI"
    in_rate = 24000
    # Harmless errors: a nudge arriving while a reply is already being generated, or a cancel with none running.
    BENIGN = {"conversation_already_has_active_response", "response_cancel_not_active"}

    def __init__(self, model, voice, instructions, tools):
        self.model, self.voice, self.instructions, self.tools = model, voice, instructions, tools
        self.ws = None

    async def _send(self, event: dict):
        await self.ws.send(json.dumps(event))

    async def connect(self):
        import websockets
        url = config.OPENAI_URL
        url += ("&" if "?" in url else "?") + f"model={self.model}"
        headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
        if "azure" in url:
            headers["api-key"] = config.OPENAI_API_KEY
        self.ws = await websockets.connect(url, additional_headers=headers, max_size=None)
        await self._send({"type": "session.update", "session": {
            "type": "realtime",
            "instructions": self.instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": self.in_rate},
                    "transcription": {"model": config.OPENAI_TRANSCRIBE_MODEL},
                    "turn_detection": {"type": "server_vad", "silence_duration_ms": 500, "prefix_padding_ms": 300,
                                       "create_response": True, "interrupt_response": True},
                },
                "output": {"format": {"type": "audio/pcm", "rate": OUT_RATE}, "voice": self.voice},
            },
            "tools": [{"type": "function", **t} for t in self.tools],
            "tool_choice": "auto",
        }})
        # Wait for the server to accept the settings, so a bad model/voice/key fails before dialing.
        async def accepted():
            while True:
                ev = json.loads(await self.ws.recv())
                if ev["type"] == "session.updated":
                    return
                if ev["type"] == "error":
                    raise RuntimeError(f"OpenAI Realtime: {ev['error'].get('message')}")
        await asyncio.wait_for(accepted(), 15)

    async def send_audio(self, pcm: bytes):
        await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})

    async def send_note(self, text: str):
        await self._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        await self._send({"type": "response.create"})

    async def events(self):
        async for raw in self.ws:
            ev = json.loads(raw)
            t = ev.get("type", "")
            if t == "response.output_audio.delta":
                yield ("audio", base64.b64decode(ev["delta"]))
            elif t == "input_audio_buffer.speech_started":
                yield ("interrupted",)
            elif t == "conversation.item.input_audio_transcription.completed":
                yield ("them_text", ev.get("transcript", ""))
            elif t == "response.output_audio_transcript.delta":
                yield ("ai_text", ev.get("delta", ""))
            elif t == "response.done":
                yield ("turn_complete",)
            elif t == "response.function_call_arguments.done":
                try:
                    args = json.loads(ev.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield ("tool", ev.get("call_id"), ev.get("name"), args)
            elif t == "error":
                err = ev.get("error", {})
                if err.get("code") not in self.BENIGN:
                    yield ("info", f"error: {err.get('message')}")

    async def tool_result(self, call_id, name, result: dict, silent=False):
        await self._send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": json.dumps(result)}})
        if not silent:
            await self._send({"type": "response.create"})

    async def close(self):
        if self.ws:
            await self.ws.close()
