"""Voice providers built on Pipecat (https://github.com/pipecat-ai/pipecat, BSD-2), an optional extra.

Pipecat already speaks to dozens of AI voice services. This module plugs a Pipecat pipeline into the call
session as one more voice provider, with the same interface as voice.GeminiVoice:

  provider "grok"      Grok Voice (xAI) speech-to-speech
  provider "pipeline"  any speech-to-text + any LLM + any text-to-speech, chosen by name, e.g.
                       stt "deepgram", llm "anthropic:claude-haiku-4-5", tts "elevenlabs:<voice id>"
  provider "local"     the same, all on this computer and free: whisper + ollama + kokoro

Service specs are "name" or "name:value"; the value is the model (STT, LLM) or the voice (TTS).
"""

import asyncio
import dataclasses
import importlib
import json
import os
import platform
import time

from . import config, phone
from .voice import OUT_RATE

IN_RATE = 16000
LEAD_S = 0.12   # keep the phone's playback queue this far ahead of real time, like agent.Uplink

# name: (pipecat.services module, class, API key setting or None, pipecat extra to install)
SERVICES = {
    "stt": {
        "deepgram":   ("deepgram.stt", "DeepgramSTTService", "DEEPGRAM_API_KEY", "deepgram"),
        "elevenlabs": ("elevenlabs.stt", "ElevenLabsRealtimeSTTService", "ELEVENLABS_API_KEY", "elevenlabs"),
        "openai":     ("openai.stt", "OpenAISTTService", "OPENAI_API_KEY", "openai"),
        "groq":       ("groq.stt", "GroqSTTService", "GROQ_API_KEY", "groq"),
        # local, free. On Apple Silicon Pipecat requires (and uses) the faster MLX build.
        "whisper":    (("whisper.stt", "WhisperSTTServiceMLX", None, "whisper,mlx-whisper")
                       if platform.system() == "Darwin" and platform.machine() == "arm64"
                       else ("whisper.stt", "WhisperSTTService", None, "whisper")),
    },
    "llm": {
        "openai":     ("openai.llm", "OpenAILLMService", "OPENAI_API_KEY", "openai"),
        "anthropic":  ("anthropic.llm", "AnthropicLLMService", "ANTHROPIC_API_KEY", "anthropic"),
        "google":     ("google.llm", "GoogleLLMService", "GEMINI_API_KEY", "google"),
        "vertex":     ("google.vertex.llm", "GoogleVertexLLMService", None, "google"),  # uses the GCP project
        "groq":       ("groq.llm", "GroqLLMService", "GROQ_API_KEY", "groq"),
        "grok":       ("xai.llm", "GrokLLMService", "XAI_API_KEY", "xai"),
        "openrouter": ("openrouter.llm", "OpenRouterLLMService", "OPENROUTER_API_KEY", "openrouter"),
        "ollama":     ("ollama.llm", "OLLamaLLMService", None, "ollama"),               # local, free
    },
    "tts": {
        "elevenlabs": ("elevenlabs.tts", "ElevenLabsTTSService", "ELEVENLABS_API_KEY", "elevenlabs"),
        "cartesia":   ("cartesia.tts", "CartesiaTTSService", "CARTESIA_API_KEY", "cartesia"),
        "openai":     ("openai.tts", "OpenAITTSService", "OPENAI_API_KEY", "openai"),
        "deepgram":   ("deepgram.tts", "DeepgramTTSService", "DEEPGRAM_API_KEY", "deepgram"),
        "kokoro":     ("kokoro.tts", "KokoroTTSService", None, "kokoro"),               # local, free
        "piper":      ("piper.tts", "PiperTTSService", None, "piper"),                  # local, free
    },
    "s2s": {
        "grok":       ("xai.realtime.llm", "GrokRealtimeLLMService", "XAI_API_KEY", "xai"),
    },
}

LOCAL = {"stt": "whisper", "llm": "ollama:llama3.2", "tts": "kokoro"}

# Used when a spec has no ":value" and the service has no usable default of its own.
DEFAULTS = {
    ("tts", "kokoro"): "af_heart",
    ("tts", "piper"): "en_US-ryan-high",
    ("tts", "elevenlabs"): "21m00Tcm4TlvDq8ikWAM",   # a premade ElevenLabs voice
    ("llm", "ollama"): "llama3.2",
}
if SERVICES["stt"]["whisper"][1] == "WhisperSTTServiceMLX":   # Pipecat's MLX default is "tiny": too weak for calls
    DEFAULTS[("stt", "whisper")] = "mlx-community/whisper-large-v3-turbo-q4"


class SetupError(phone.PhoneError):
    """A missing package or key, with a message that says how to fix it."""


def _key(name: str) -> str:
    if name == "GEMINI_API_KEY":
        return config.GEMINI_API_KEY
    if name == "OPENAI_API_KEY":
        return config.OPENAI_API_KEY
    return os.environ.get(f"CLAUDE_TELE_CONNECT_{name}") or os.environ.get(name, "")


def install_hint(extras) -> str:
    extras = ",".join(sorted(set(extras)))
    return (f'uv tool install --reinstall "claude-tele-connect[pipecat]" --with "pipecat-ai[{extras}]"   '
            f'(pip: pip install "pipecat-ai[{extras}]")')


def missing(kind: str, spec: str) -> str:
    """'' if the service can be built, else what's missing."""
    name = spec.partition(":")[0]
    if name not in SERVICES[kind]:
        return f"unknown {kind} '{name}' (choose from: {', '.join(SERVICES[kind])})"
    mod, _, key, extra = SERVICES[kind][name]
    if key and not _key(key):
        return f"{kind} '{name}' needs CLAUDE_TELE_CONNECT_{key} (or {key}) in {config.ENV_FILE}"
    if name == "vertex" and not config.GCP_PROJECT:
        return "llm 'vertex' needs CLAUDE_TELE_CONNECT_GCP_PROJECT"
    if needs_install(kind, spec):
        return f"{kind} '{name}' needs extra packages: {install_hint([extra])}"
    return ""


def needs_install(kind: str, spec: str) -> str:
    """The pipecat extra to install if this service's packages are missing, else ''."""
    name = spec.partition(":")[0]
    mod, _, _, extra = SERVICES[kind][name]
    try:
        _silence_pipecat_logs()
        importlib.import_module(f"pipecat.services.{mod}")
        return ""
    except Exception:
        return extra


def _silence_pipecat_logs():
    try:
        from loguru import logger
        logger.remove()
        level = os.environ.get("CLAUDE_TELE_CONNECT_PIPECAT_LOG", "ERROR")
        import sys
        logger.add(sys.stderr, level=level)
    except Exception:
        pass


def build(kind: str, spec: str, instructions: str = ""):
    err = missing(kind, spec)
    if err:
        raise SetupError(err)
    name, _, value = spec.partition(":")
    value = value or DEFAULTS.get((kind, name), "")
    mod, cls_name, key, _ = SERVICES[kind][name]
    cls = getattr(importlib.import_module(f"pipecat.services.{mod}"), cls_name)
    kw = {}
    if key:
        kw["api_key"] = _key(key)
    if name == "vertex":
        # Gemini text models are served from the global endpoint; CLAUDE_TELE_CONNECT_LLM_LOCATION overrides.
        kw.update(project_id=config.GCP_PROJECT, location=os.environ.get("CLAUDE_TELE_CONNECT_LLM_LOCATION", "global"))
    S = getattr(cls, "Settings", None)
    fields = {f.name for f in dataclasses.fields(S)} if S and dataclasses.is_dataclass(S) else set()
    s = {}
    if value:
        s["voice" if kind == "tts" else "model"] = value
    if instructions and "system_instruction" in fields:
        s["system_instruction"] = instructions
    if name in ("google", "vertex") and "thinking" in fields and hasattr(cls, "ThinkingConfig"):
        s["thinking"] = cls.ThinkingConfig(thinking_level="low")   # phone turns need a fast first word
    if s:
        kw["settings"] = S(**{k: v for k, v in s.items() if k in fields})
    return cls(**kw)


class PipecatVoice:
    """A Pipecat pipeline as a voice provider. Phone audio goes in through a small custom transport, the
    AI's audio comes out of it paced in real time, and an observer turns Pipecat frames into our events."""

    in_rate = IN_RATE

    def __init__(self, provider, *, model, voice, instructions, tools, stt="", llm="", tts=""):
        self.provider, self.instructions, self.tools = provider, instructions, tools
        if provider == "local":
            stt, llm, tts = stt or LOCAL["stt"], llm or LOCAL["llm"], tts or LOCAL["tts"]
        if provider == "grok":
            self.parts = {"s2s": "grok" + (f":{model}" if model else "")}
            self.voice_name = voice
        else:
            self.parts = {"stt": stt or config.STT, "llm": llm or config.LLM, "tts": tts or config.TTS}
            if voice and ":" not in self.parts["tts"]:
                self.parts["tts"] += f":{voice}"
            if model and ":" not in self.parts["llm"]:
                self.parts["llm"] += f":{model}"
        self.name = "Pipecat " + " + ".join(self.parts.values())
        self.model = " + ".join(self.parts.values())
        self.q = asyncio.Queue()
        self._pending = {}   # tool call id -> result_callback
        self._worker = self._runner = self._run_task = None

    def problems(self) -> list[str]:
        out, extras = [], []
        for kind, spec in self.parts.items():
            name = spec.partition(":")[0]
            if not spec:
                out.append(f"no {kind} chosen: set CLAUDE_TELE_CONNECT_{kind.upper()} or pass --{kind}")
            elif name in SERVICES[kind] and needs_install(kind, spec):
                extras += needs_install(kind, spec).split(",")
            else:
                m = missing(kind, spec)
                if m:
                    out.append(m)
        if extras:
            hint = ('uv tool install --reinstall "claude-tele-connect[local]"   (pip: pip install "claude-tele-connect[local]")'
                    if self.provider == "local" else install_hint(extras))
            out.insert(0, f"install the Pipecat packages for {self.model}: {hint}")
        return out

    async def connect(self):
        probs = self.problems()
        if probs:
            raise SetupError("; ".join(probs))
        _silence_pipecat_logs()
        from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
        from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams
        from pipecat.frames.frames import FunctionCallResultProperties, InputAudioRawFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair, LLMUserAggregatorParams)
        from pipecat.transports.base_input import BaseInputTransport
        from pipecat.transports.base_output import BaseOutputTransport
        from pipecat.transports.base_transport import TransportParams
        from pipecat.workers.runner import WorkerRunner

        self._InputAudioRawFrame = InputAudioRawFrame
        self._ResultProps = FunctionCallResultProperties
        q = self.q

        class PhoneIn(BaseInputTransport):
            async def start(self, frame):
                await super().start(frame)
                await self.set_transport_ready(frame)

        class PhoneOut(BaseOutputTransport):
            next_t = 0.0

            async def start(self, frame):
                await super().start(frame)
                await self.set_transport_ready(frame)

            async def write_audio_frame(self, frame):
                q.put_nowait(("audio", frame.audio))
                now = time.monotonic()
                self.next_t = max(self.next_t, now) + len(frame.audio) / (2 * frame.sample_rate)
                if self.next_t - now > LEAD_S:
                    await asyncio.sleep(self.next_t - now - LEAD_S)
                return True

        params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=IN_RATE,
                                 audio_out_enabled=True, audio_out_sample_rate=OUT_RATE)
        self._in, out = PhoneIn(params), PhoneOut(params)

        schema = ToolsSchema(standard_tools=[
            FunctionSchema(name=t["name"], description=t["description"], properties=t["parameters"]["properties"],
                           required=t["parameters"]["required"]) for t in self.tools])
        realtime = "s2s" in self.parts
        llm = build("s2s" if realtime else "llm", self.parts.get("s2s") or self.parts["llm"], self.instructions)
        if realtime and self.voice_name and getattr(llm, "_settings", None) is not None:
            try:
                llm._settings.session_properties.voice = self.voice_name
            except Exception:
                pass
        for t in self.tools:
            llm.register_function(t["name"], self._on_tool, cancel_on_interruption=False)

        retries = {"n": 0}

        @llm.event_handler("on_completion_timeout")
        async def _llm_timeout(service):
            # A slow first reply would otherwise leave the call silent: ask again (twice at most per call).
            if retries["n"] < 2:
                retries["n"] += 1
                from pipecat.frames.frames import LLMRunFrame
                q.put_nowait(("info", "LLM reply timed out; retrying"))
                await self._worker.queue_frames([LLMRunFrame()])

        from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
        from pipecat.turns.user_turn_strategies import UserTurnStrategies

        context = LLMContext(tools=schema)
        # A plain silence timeout ends the caller's turn: snappier on phone audio than Pipecat's default
        # smart-turn model, and close to the native providers' 0.5 s.
        user_agg, bot_agg = LLMContextAggregatorPair(
            context, user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.3)),
                user_turn_strategies=UserTurnStrategies(
                    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.4)])))
        if realtime:
            stages = [self._in, user_agg, llm, out, bot_agg]
        else:
            stages = [self._in, build("stt", self.parts["stt"]), user_agg, llm,
                      build("tts", self.parts["tts"]), out, bot_agg]

        self._worker = PipelineWorker(
            Pipeline(stages), params=PipelineParams(audio_in_sample_rate=IN_RATE, audio_out_sample_rate=OUT_RATE),
            observers=[_Observer(q, out)], idle_timeout_secs=None, enable_rtvi=False)
        started = asyncio.Event()

        @self._worker.event_handler("on_pipeline_started")
        async def _started(worker, frame):
            started.set()

        @self._worker.event_handler("on_pipeline_error")
        async def _error(worker, frame):
            proc = getattr(frame, "processor", None)
            dead = getattr(frame, "fatal", False) or (proc is not None and not getattr(proc, "is_usable", True))
            msg = str(getattr(frame, "error", frame))[:300]
            q.put_nowait(("fatal" if dead else "info", f"{proc or 'pipeline'}: {msg}"))

        self._runner = WorkerRunner(handle_sigint=False)
        await self._runner.add_workers(self._worker)
        self._run_task = asyncio.create_task(self._runner.run())
        done, _ = await asyncio.wait([asyncio.create_task(started.wait()), self._run_task], timeout=60,
                                     return_when=asyncio.FIRST_COMPLETED)
        if not started.is_set():
            if self._run_task in done and self._run_task.exception():
                raise SetupError(f"Pipecat failed to start: {self._run_task.exception()!r}")
            raise SetupError("Pipecat pipeline didn't start within 60 s")
        # Local models (whisper, kokoro) load on start; give errors a moment to surface before dialing.
        await asyncio.sleep(0.5)

    async def _on_tool(self, params):
        self._pending[params.tool_call_id] = params.result_callback
        args = params.arguments if isinstance(params.arguments, dict) else json.loads(params.arguments or "{}")
        self.q.put_nowait(("tool", params.tool_call_id, params.function_name, dict(args)))

    async def send_audio(self, pcm: bytes):
        await self._in.push_audio_frame(self._InputAudioRawFrame(audio=pcm, sample_rate=IN_RATE, num_channels=1))

    async def send_note(self, text: str):
        from pipecat.frames.frames import LLMMessagesAppendFrame
        await self._worker.queue_frames([LLMMessagesAppendFrame(messages=[{"role": "user", "content": text}],
                                                                run_llm=True)])

    async def events(self):
        while True:
            yield await self.q.get()

    async def tool_result(self, call_id, name, result: dict, silent=False):
        cb = self._pending.pop(call_id, None)
        if cb:
            await cb(result, properties=self._ResultProps(run_llm=not silent))

    async def close(self):
        if self._worker:
            await self._worker.cancel()
        if self._run_task:
            try:
                await asyncio.wait_for(self._run_task, 5)
            except Exception:
                pass


def _observer_base():
    from pipecat.observers.base_observer import BaseObserver
    return BaseObserver


class _Observer:
    """Created lazily as a real Pipecat observer (see __new__), so importing this module doesn't need Pipecat."""

    def __new__(cls, q, out):
        from pipecat.frames.frames import (BotStoppedSpeakingFrame, InterruptionFrame, TranscriptionFrame,
                                           TTSTextFrame)

        class Observer(_observer_base()):
            async def on_push_frame(self, data):
                if not getattr(data, "first_push", True):
                    return
                f = data.frame
                if isinstance(f, InterruptionFrame):
                    out.next_t = 0.0
                    q.put_nowait(("interrupted",))
                elif isinstance(f, TranscriptionFrame) and f.text:
                    q.put_nowait(("them_text", f.text + " "))
                elif isinstance(f, TTSTextFrame) and f.text:
                    q.put_nowait(("ai_text", f.text + " "))
                elif isinstance(f, BotStoppedSpeakingFrame):
                    q.put_nowait(("turn_complete",))

        return Observer()
