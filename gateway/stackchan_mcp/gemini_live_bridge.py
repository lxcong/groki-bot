# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Gemini Live API bridge for StackChan voice control.

Independent from the xiaozhi voice link: this bridge opens a separate Gemini
Live WebSocket and exposes three physical-control tools (move_head,
set_avatar, set_all_leds) via function calling. Audio in goes to Gemini, audio
out comes back as PCM, and any tool call is dispatched to the same ESP32
manager the rest of the gateway uses.

What this module covers:
- Configurable system instruction (overridable by callers, plus an optional
  personality file, see STACKCHAN_PERSONALITY_FILE).
- Three function declarations matched to ESP32 MCP tools.
- Tool-call dispatch and tool-response loop.
- send_audio() entry point so an external mic source can stream PCM.
- start()/stop() lifecycle for an asyncio context.

Not covered here:
- 15-min session auto-resume.
- Built-in microphone capture (callers supply 16 kHz PCM).
- TTS-out → device speaker pipeline (callers receive the 24 kHz PCM stream).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

from .debug_status import DebugStatus, get_debug_status
from .gbot_brain import tool_bot
from .personality import personality_path


from .voice_output_queue import VoiceOutputQueue
from .voice_tools import (  # noqa: F401 - retain the established declarations/constants API
    VoiceToolExecutor,
    ToolMetadata,
    TOOL_METADATA,
    TOOL_MAP,
    EMOTION_SYSTEM_INSTRUCTION,
    VOICE_ONLY_SYSTEM_INSTRUCTION,
    grokbot_instruction,
    AGENT_TOOLS_INSTRUCTION,
    ASK_CLAUDE_INSTRUCTION,
    agent_tools_instruction,
    MAC_CONTROL_INSTRUCTION,
    WEB_SEARCH_RULE,
    WEB_SEARCH_RULE_WITH_GROKBOT,
    mac_control_instruction,
    AVATAR_FACE_ENUM,
    EMOTION_MOOD_ENUM,
    EMOTION_INTENSITY_ENUM,
    MEDIA_ACTION_ENUM,
    _env_flag,
    device_tools_enabled,
    mac_control_enabled,
    ask_claude_enabled,
    build_function_declarations as shared_function_declarations,
)

logger = logging.getLogger(__name__)


# Used when the device does not run the face/LED/head tools (for example the
# Groki Bot firmware, whose XiaoZhi client announces features.mcp=false). In
# that case those tools are hidden from Gemini: calling them first would stall
# the reply until the tool call times out.

# Every rule above and below is written in English, but the robot talks in
# whatever language the user speaks; this rule says so explicitly so the
# English prompt does not pull replies towards English.
LANGUAGE_INSTRUCTION = """## Language

Always reply in the language the user is speaking (for example, Chinese when the user speaks Chinese). These instructions are written in English only for convenience. When a tool result or system notice gives you a sentence to say, say it in the user's language."""


# Only added when ask_claude is declared (see ask_claude_enabled()).


# With ask_grokbot available, questions that only need an answer go to the
# Grok Bot agent; web_search is kept for "open a browser and search".


def _personality_path() -> Path:
    """Optional personality file: STACKCHAN_PERSONALITY_FILE, else
    ``personality.md`` in the gateway directory (shared with Grok voice)."""
    return personality_path()


# Short and neutral on purpose: a personality file appended under
# "# Personality" can change the name, language and style.
BUILTIN_PERSONALITY = (
    "You are Groki, a small robot that lives on the user's desk. "
    "Reply in the language the user speaks, in one or two short sentences, "
    "like a friend, without a customer-service tone. "
    "If you are asked which AI model you run on, answer truthfully. "
    "Only call end_conversation when the user clearly says goodbye. "
    'If a "# Personality" section follows, it overrides this paragraph '
    "wherever the two differ."
)


def _load_personality() -> str:
    """Built-in persona at the top of the instructions.

    The personality file is not read here: build_system_instruction()
    appends it once below the rules, as the docs describe. Reading it here
    as well put the file into the prompt twice.
    """
    return BUILTIN_PERSONALITY


def default_system_instruction(
    *,
    device_tools: bool | None = None,
    mac_control: bool | None = None,
    grokbot: str | None = None,
    ask_claude: bool | None = None,
) -> str:
    """Operational rules for Gemini, matching the tools that are declared.

    ``grokbot`` is the Grok Bot agent name; ``None`` reads STACKCHAN_TOOL_BOT
    and an empty string leaves the ask_grokbot rules out. ``ask_claude``
    ``None`` follows ask_claude_enabled().
    """
    if device_tools is None:
        device_tools = device_tools_enabled()
    if mac_control is None:
        mac_control = mac_control_enabled()
    if grokbot is None:
        grokbot = tool_bot()
    if ask_claude is None:
        ask_claude = ask_claude_enabled()
    parts = [
        _load_personality(),
        LANGUAGE_INSTRUCTION,
        EMOTION_SYSTEM_INSTRUCTION if device_tools else VOICE_ONLY_SYSTEM_INSTRUCTION,
        agent_tools_instruction(ask_claude=ask_claude),
    ]
    if grokbot:
        parts.append(grokbot_instruction(grokbot, mac_control=mac_control))
    if mac_control:
        parts.append(mac_control_instruction(grokbot=bool(grokbot)).strip())
    return "\n\n".join(part.strip() for part in parts if part.strip())


# Full rule set (device tools and Mac control on). Kept for callers that want
# every rule; runtime sessions use default_system_instruction().
DEFAULT_SYSTEM_INSTRUCTION = default_system_instruction(
    device_tools=True, mac_control=True, ask_claude=True
)

# Gemini 3.1 Flash Live Preview (launched 2026-03-26) is Google's current
# flagship realtime audio model. Verified against client.models.list() on
# 2026-05-12. Why this and not 2.5-flash-native-audio-latest:
#   - ComplexFuncBench Audio 90.8%; sharper at "抬头" → move_head than 2.5,
#     which is the whole point for the StackChan use case.
#   - Lower latency than 2.5 Native Audio; better at filtering background
#     noise (kitchen / coding desk reality).
#   - Tool use *during* a live turn, not just at turn boundaries.
# Caveats noted in the model card:
#   - No affective_dialog or proactive_audio (we don't set these).
#   - Function calling is synchronous (matches what we already do).
# 2026-09-23: default moved to Gemini 3.8 Live (GA 2026-09-15, model code
# `gemini-3.8-live`, https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live).
# Differences that matter here: TEXT response modality is rejected (1007), so
# build_live_config remaps it like 3.1; a turn_complete arrives right after a
# tool call, before the spoken answer; thinking_config, proactive_audio=false
# and enable_affective_dialog are rejected (we set none of them).
# Set STACKCHAN_GEMINI_MODEL=gemini-3.1-flash-live-preview to go back.
# STACKCHAN_GEMINI_MODEL also overrides for bandwidth-constrained networks: 2.x
# Live models accept a true TEXT response modality (text-only downstream,
# ~1000x lighter than 24 kHz PCM), which pairs with STACKCHAN_GEMINI_TTS=edge.
DEFAULT_MODEL = os.getenv("STACKCHAN_GEMINI_MODEL", "gemini-3.8-live")
DEFAULT_VOICE = os.getenv("STACKCHAN_GEMINI_VOICE", "Kore")
CONVERSATION_IDLE_TIMEOUT_S = 8.0
DEFAULT_CTX_TRIGGER_TOKENS = 25_000
DEFAULT_CTX_TARGET_TOKENS = 8_000
DEFAULT_VAD_SILENCE_MS = 650
# While DORMANT the server may stay silent for a long time, but it is known to close with 1008
# after about 270 s (the close is itself a message). No server message for over 300 s means a half-open connection: reconnect.
DEFAULT_RECEIVE_STALL_S = 300.0
DEFAULT_MANUAL_VAD = True
DEFAULT_RECONNECT_AUDIO_TTL_S = 5.0
DEFAULT_RECONNECT_AUDIO_MAX_BYTES = 16_000 * 2 * 3


def _env_enabled(name: str, *, default: str = "1") -> bool:
    value = os.getenv(name, default).strip().lower()
    return value not in {"0", "false", "no", "off"}


def manual_vad_enabled() -> bool:
    return _env_enabled(
        "STACKCHAN_MANUAL_VAD", default="1" if DEFAULT_MANUAL_VAD else "0"
    )


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("invalid %s=%r; using %d", name, raw, default)
        return default
    return value


def _log_session_dead_task(task: asyncio.Task[None]) -> None:
    """Retrieve session-dead task exceptions so the event loop does not warn."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Gemini Live session-dead task failed: %s", exc, exc_info=exc)


def _close_code(exc: BaseException | None) -> int | None:
    """Extract the close code from a websocket exception; without the attribute, look for 1008 in the text."""
    if exc is None:
        return None
    code = getattr(getattr(exc, "rcvd", None), "code", None)
    if isinstance(code, int):
        return code
    return 1008 if "1008" in str(exc) else None


def _is_dead_resumption_handle(exc: BaseException) -> bool:
    """True when the server has rejected the resumption handle.

    Matches known rejection wording. A plain network drop is not a match;
    the handle may still work, and keeping it is what preserves conversation
    context across a blip.
    """
    text = str(exc).lower()
    return (
        "session expired" in text
        or "invalid argument" in text
        or "requested entity was not found" in text
    )


PersonalityLoader = Callable[[], str]


def _default_personality_loader() -> str:
    """Read the personality file if it exists, otherwise return ''."""
    try:
        return _personality_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def build_system_instruction(
    base: str | None = None,
    personality_loader: PersonalityLoader = _default_personality_loader,
) -> str:
    """Merge the base operational rules with the optional personality file."""
    if base is None:
        base = default_system_instruction()
    extra = personality_loader().strip()
    if not extra:
        return base
    return f"{base}\n\n# Personality\n\n{extra}"


# Mapping from Gemini function name to the (esp32_tool_name, arg_transform).
# Kept as a module-level constant so unit tests can introspect it without
# constructing a bridge.


def build_function_declarations() -> list[Any]:
    from google.genai import types

    return shared_function_declarations(types=types)


def build_live_config(
    *,
    system_instruction: str | None = None,
    voice: str = DEFAULT_VOICE,
    response_modality: str = "AUDIO",
    session_resumption_handle: str | None = None,
    model: str = DEFAULT_MODEL,
) -> Any:
    """Construct the LiveConnectConfig used for session setup.

    response_modality:
      - "AUDIO" (default): Gemini emits 24 kHz PCM. Caller supplies on_audio
        to drain it (e.g. play through Mac speakers or Opus-encode to the
        StackChan).
      - "TEXT": Gemini emits text. The CLI voice entry uses TEXT and routes
        the response into stackchan_mcp.tts so the device speaker says it
        through the existing TTS pipeline.
    """
    from google.genai import types  # noqa: PLC0415

    instruction = system_instruction or build_system_instruction()

    live_modality = response_modality
    if response_modality == "TEXT" and model.startswith("gemini-3"):
        # Gemini 3.x Live models reject TEXT as a direct response modality
        # (3.1 closes with 1011, 3.8 with 1007).  Keep the public bridge contract
        # as "text to caller", but ask Live for audio plus output transcription:
        # the native audio is discarded and the transcript feeds Edge TTS.
        # 2.x Live models accept TEXT directly, so they skip this remap and
        # nothing audio-sized crosses the wire.
        live_modality = "AUDIO"

    context_trigger_tokens = _positive_int_from_env(
        "STACKCHAN_CTX_TRIGGER_TOKENS",
        DEFAULT_CTX_TRIGGER_TOKENS,
    )
    context_target_tokens = _positive_int_from_env(
        "STACKCHAN_CTX_TARGET_TOKENS",
        DEFAULT_CTX_TARGET_TOKENS,
    )
    use_manual_vad = manual_vad_enabled()
    vad_silence_ms = _positive_int_from_env(
        "STACKCHAN_VAD_SILENCE_MS",
        DEFAULT_VAD_SILENCE_MS,
    )
    if use_manual_vad:
        automatic_vad = types.AutomaticActivityDetection(disabled=True)
    else:
        automatic_vad = types.AutomaticActivityDetection(
            silence_duration_ms=vad_silence_ms,
        )

    config = types.LiveConnectConfig(
        response_modalities=[live_modality],
        system_instruction=types.Content(
            parts=[types.Part(text=instruction)],
        ),
        tools=[
            types.Tool(google_search=types.GoogleSearch()),
            types.Tool(function_declarations=build_function_declarations()),
        ],
        session_resumption=types.SessionResumptionConfig(
            handle=session_resumption_handle,
        ),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=automatic_vad,
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=context_trigger_tokens,
            sliding_window=types.SlidingWindow(
                target_tokens=context_target_tokens,
            ),
        ),
    )
    # speech_config / voice only applies to AUDIO responses.
    if live_modality == "AUDIO":
        config.speech_config = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice,
                ),
            ),
        )
    if response_modality == "TEXT":
        config.output_audio_transcription = types.AudioTranscriptionConfig()
    return config


AudioCallback = Callable[[bytes], Awaitable[None]]
TextCallback = Callable[[str], Awaitable[None]]
TurnCompleteCallback = Callable[[], Awaitable[None]]
ActivityCallback = Callable[[], None]


def _pop_text_segments(buffer: list[str], *, final: bool) -> list[str]:
    """Pop speakable short text segments from a Gemini text buffer."""
    text = "".join(buffer)
    if not text:
        return []
    segments: list[str] = []
    start = 0
    strong = "。！？!?；;\n"
    soft = "，,"
    for idx, ch in enumerate(text):
        cut = ch in strong or (ch in soft and idx - start >= 12)
        if not cut:
            continue
        segment = text[start : idx + 1].strip()
        if segment:
            segments.append(segment)
        start = idx + 1
    remainder = text[start:].lstrip()
    if final and remainder.strip():
        segments.append(remainder.strip())
        remainder = ""
    buffer.clear()
    if remainder:
        buffer.append(remainder)
    return segments


class GeminiLiveBridge(VoiceToolExecutor):
    """Single-session Gemini Live bridge bound to one ESP32 manager."""

    def __init__(
        self,
        esp32: Any,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        response_modality: str = "AUDIO",
        system_instruction: str | None = None,
        on_audio: AudioCallback | None = None,
        on_text: TextCallback | None = None,
        on_turn_complete: TurnCompleteCallback | None = None,
        on_interrupt: TurnCompleteCallback | None = None,
        on_end_conversation: TurnCompleteCallback | None = None,
        snap_to_face: Callable[[], Awaitable[None]] | None = None,
        usb_transport: Any | None = None,
        on_head_command: ActivityCallback | None = None,
        wake_gate_state_getter: Callable[[], str] | None = None,
        set_face_tracking: Callable[[bool], Awaitable[dict[str, Any]]] | None = None,
        reconnect_on_close: bool = True,
        reconnect_initial_backoff_s: float = 1.0,
        reconnect_max_backoff_s: float = 30.0,
        conversation_idle_timeout_s: float = CONVERSATION_IDLE_TIMEOUT_S,
        debug_status: DebugStatus | None = None,
        on_session_dead: TurnCompleteCallback | None = None,
        reconnect_audio_ttl_s: float | None = None,
        reconnect_audio_max_bytes: int = DEFAULT_RECONNECT_AUDIO_MAX_BYTES,
        resumption_max_failures: int = 3,
    ) -> None:
        self._status = debug_status or get_debug_status()
        self._esp32 = esp32
        self._usb_transport = usb_transport
        self._api_key = (
            api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        )
        self._model = model
        self._voice = voice
        self._response_modality = response_modality
        self._system_instruction = system_instruction or build_system_instruction()
        self._on_audio = on_audio
        self._on_text = on_text
        self._on_turn_complete = on_turn_complete
        self._on_interrupt = on_interrupt
        self._output = VoiceOutputQueue()
        self._on_end_conversation = on_end_conversation
        self._snap_to_face = snap_to_face
        self._session: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._client: Any | None = None
        self._on_head_command = on_head_command
        self._wake_gate_state_getter = wake_gate_state_getter
        self._set_face_tracking = set_face_tracking
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        # Buffer text fragments until the model marks the turn complete so
        # we send one TTS request per utterance instead of one per token.
        self._text_buf: list[str] = []
        # Gemini Live sessions cap at ~15 min. When True, _run rebuilds the
        # session whenever the receive loop exits (either ``GoAway`` from
        # Gemini or any transport error) so callers stay connected end-to-end.
        self._reconnect_on_close = reconnect_on_close
        self._reconnect_initial_backoff_s = reconnect_initial_backoff_s
        self._reconnect_max_backoff_s = reconnect_max_backoff_s
        self._conversation_idle_timeout_s = conversation_idle_timeout_s
        self._silence_timeout_task: asyncio.Task[None] | None = None
        self._silence_generation = 0
        # Lifecycle counter; increments each time a fresh Gemini Live socket
        # comes up. Used by tests + by snap_to_face guard so we only "wake up"
        # on the very first connect, not after silent reconnects.
        self._session_count = 0
        self._current_turn_emotion_face: str | None = None
        self._emotion_tasks: set[asyncio.Task[None]] = set()
        self._resumption_handle: str | None = None
        self._resumption_max_failures = resumption_max_failures
        self._resumption_failure_count = 0
        self._go_away_received = False
        self._receive_stall_s = float(
            os.getenv("STACKCHAN_GEMINI_RECEIVE_STALL_S", str(DEFAULT_RECEIVE_STALL_S))
        )
        self._last_server_message_at: float | None = None
        self._receive_watchdog_task: asyncio.Task[None] | None = None
        self._receive_stall_triggered = False
        self._mono_clock: Callable[[], float] = time.monotonic
        # Manual VAD: whether an activity is open. send_audio opens one automatically
        # (activity_start) when none is; send_activity_end and a new session reset it.
        self._activity_open = False
        self._cancelled_tool_call_ids: set[str] = set()
        self._tool_call_task_ids: dict[str, int] = {}
        self._status_generation: int | None = None
        self._on_session_dead = on_session_dead
        ttl_env = os.getenv("STACKCHAN_RECONNECT_AUDIO_TTL_S")
        self._reconnect_audio_ttl_s = (
            reconnect_audio_ttl_s
            if reconnect_audio_ttl_s is not None
            else float(ttl_env)
            if ttl_env
            else DEFAULT_RECONNECT_AUDIO_TTL_S
        )
        self._reconnect_audio_max_bytes = max(1, int(reconnect_audio_max_bytes))
        self._pending_audio: deque[bytes] = deque()
        self._pending_audio_bytes = 0
        self._pending_audio_since: float | None = None
        self._reconnect_audio_timeout_task: asyncio.Task[None] | None = None
        self._session_dead_notified = False
        self._audio_out_lock = asyncio.Lock()
        VoiceToolExecutor.__init__(
            self,
            esp32,
            usb_transport=usb_transport,
            on_head_command=on_head_command,
            wake_gate_state_getter=wake_gate_state_getter,
            set_face_tracking=set_face_tracking,
            on_end_conversation=on_end_conversation,
            announce_result=self.inject_user_text,
            debug_status=self._status,
            cancel_idle=self._cancel_silence_timeout,
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_turn_emotion_face(self) -> str | None:
        return self._current_turn_emotion_face

    def clear_turn_emotion_face(self) -> None:
        self._current_turn_emotion_face = None

    @property
    def activity_open(self) -> bool:
        return self._activity_open

    def _reset_manual_vad_activity(self) -> None:
        """Reset the activity state when a new Live session starts, so an open activity is not carried over."""
        self._activity_open = False

    async def _ensure_manual_vad_activity_open(self) -> None:
        if not manual_vad_enabled() or self._activity_open:
            return
        await self.send_activity_start()

    async def start(self) -> None:
        """Open the Gemini Live session and start the receive loop."""
        if self.running:
            return
        if not self._api_key:
            raise RuntimeError(
                "GeminiLiveBridge: GEMINI_API_KEY / GOOGLE_API_KEY is not set"
            )
        from google import genai  # noqa: PLC0415
        from google.genai import types  # noqa: PLC0415

        self._client = genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )
        self._stop_event.clear()
        self._connected_event.clear()
        self._status_generation = self._status.next_gemini_generation()
        self._status.on_gemini_started(generation=self._status_generation)
        self._task = asyncio.create_task(self._run(), name="gemini-live-bridge")

    async def _run(self) -> None:
        """Lifecycle loop with optional auto-reconnect across session boundaries.

        Gemini Live sessions terminate after ~15 minutes (preview models)
        even on a fully healthy connection. With ``reconnect_on_close``
        the loop rebuilds the session as soon as the inner receive loop
        exits, keeping the voice path alive without the caller noticing.
        ``stop()`` flips ``_stop_event`` to break out cleanly.
        """
        try:
            await self._run_sessions()
        finally:
            self._status.on_gemini_stopped(generation=self._status_generation)

    async def _run_sessions(self) -> None:
        backoff = self._reconnect_initial_backoff_s
        while not self._stop_event.is_set():
            used_handle = False
            handle_used = self._resumption_handle
            try:
                config = build_live_config(
                    system_instruction=self._system_instruction,
                    voice=self._voice,
                    response_modality=self._response_modality,
                    session_resumption_handle=handle_used,
                    model=self._model,
                )
                used_handle = handle_used is not None
                async with self._client.aio.live.connect(
                    model=self._model,
                    config=config,
                ) as session:
                    self._session = session
                    self._go_away_received = False
                    self._receive_stall_triggered = False
                    self._reset_manual_vad_activity()
                    self._session_count += 1
                    self._resumption_failure_count = 0
                    self._connected_event.set()
                    self._status.on_gemini_connected(
                        self._session_count,
                        used_resumption_handle=used_handle,
                        generation=self._status_generation,
                    )
                    if not used_handle:
                        self._status.on_resumption_handle_cleared()
                    backoff = self._reconnect_initial_backoff_s
                    is_first_connect = self._session_count == 1
                    logger.info(
                        "Gemini Live session %s: count=%d model=%s voice=%s",
                        "connected" if is_first_connect else "reconnected",
                        self._session_count,
                        self._model,
                        self._voice,
                    )
                    # snap_to_face is a wake-up cue; only fire it on the
                    # very first connect, not on every silent reconnect.
                    if is_first_connect and self._snap_to_face is not None:
                        try:
                            await self._snap_to_face()
                        except Exception:
                            logger.exception("snap_to_face hook failed")

                    await self._flush_reconnect_audio()
                    await self._receive_loop()
                # Inner receive_loop returned naturally → session ended.
                logger.info("Gemini Live session %d ended", self._session_count)
                self._note_session_end(None)
                backoff = self._reconnect_initial_backoff_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Gemini Live session %d errored (%s); backoff=%.1fs",
                    self._session_count,
                    exc,
                    backoff,
                )
                self._note_session_end(exc)
                # Drop a handle the server has rejected (expired / invalid /
                # not found). Otherwise every reconnect retries the same dead
                # handle and the robot stays silent until the process restarts.
                # Fallback: drop the same handle after too many consecutive
                # failures, so a new rejection phrase cannot stall us again.
                # A failure that belongs to an older connection does not count
                # against a handle issued mid-session.
                if handle_used is not None and self._resumption_handle == handle_used:
                    self._resumption_failure_count += 1
                    dead = _is_dead_resumption_handle(exc)
                    over_limit = (
                        self._resumption_failure_count >= self._resumption_max_failures
                    )
                    if dead or over_limit:
                        failures = self._resumption_failure_count
                        self._resumption_handle = None
                        self._resumption_failure_count = 0
                        self._status.on_resumption_handle_cleared()
                        if dead:
                            logger.warning(
                                "Gemini Live resumption handle rejected by "
                                "server (%s); dropped it, next connect starts "
                                "a fresh session",
                                exc,
                            )
                        else:
                            logger.warning(
                                "Gemini Live resumption handle failed %d "
                                "times in a row (%s); dropped it, next "
                                "connect starts a fresh session",
                                failures,
                                exc,
                            )
            finally:
                self._session = None
                self._connected_event.clear()

            if not self._reconnect_on_close or self._stop_event.is_set():
                return

            # Wait `backoff` seconds, but break early if stop() comes in.
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(
                backoff * 2.0,
                self._reconnect_max_backoff_s,
            )

    def _note_session_end(self, exc: BaseException | None) -> None:
        """Record the end of a session; a drop during an active conversation (LISTENING or TTS playing) is logged as ERROR."""
        was_connected = self._session is not None
        active = self._status.on_gemini_disconnected(
            error=str(exc) if exc is not None else None,
            code=_close_code(exc),
            was_connected=was_connected,
            generation=self._status_generation,
        )
        if active:
            logger.error(
                "Gemini Live session dropped during an active conversation (the robot goes silent mid-conversation): %s",
                exc if exc is not None else "session ended normally",
            )

    async def _receive_loop(self) -> None:
        assert self._session is not None
        self._last_server_message_at = self._mono_clock()
        self._receive_watchdog_task = asyncio.create_task(
            self._receive_liveness_watchdog(),
            name="gemini-receive-liveness",
        )
        try:
            # google-genai's receive() yields one complete model turn and then
            # returns after turn_complete. Keep the same Live socket open for the
            # next user turn; only an empty receive means the socket actually ended.
            while not self._stop_event.is_set():
                if self._receive_stall_triggered:
                    break
                received_any = False
                async for response in self._session.receive():
                    received_any = True
                    self._last_server_message_at = self._mono_clock()
                    self._record_usage_metadata(response)
                    if self._handle_session_lifecycle_message(response):
                        break
                    if self._stop_event.is_set() or self._receive_stall_triggered:
                        break
                    await self._handle_message(response)
                if (
                    self._go_away_received
                    or not received_any
                    or self._receive_stall_triggered
                ):
                    break
        finally:
            self._cancel_silence_timeout()
            # stop() owns output cancellation during deliberate shutdown. An
            # output callback may itself be awaiting stop()/this receiver task.
            if not self._stop_event.is_set():
                await self._output.cancel()
                if self._on_interrupt is not None:
                    await self._on_interrupt()
            task = self._receive_watchdog_task
            self._receive_watchdog_task = None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _receive_liveness_watchdog(self) -> None:
        """Application-level receive-stall fallback: on a half-open connection receive() can hang forever."""
        poll_s = min(5.0, max(1.0, self._receive_stall_s / 10.0))
        while not self._stop_event.is_set():
            await asyncio.sleep(poll_s)
            if self._session is None or self._last_server_message_at is None:
                continue
            elapsed = self._mono_clock() - self._last_server_message_at
            if elapsed < self._receive_stall_s:
                continue
            logger.warning(
                "Gemini Live receive stall: no server message for %.0fs "
                "(threshold=%.0fs); forcing session close to trigger reconnect",
                elapsed,
                self._receive_stall_s,
            )
            self._status.on_gemini_receive_stall_reconnect()
            self._receive_stall_triggered = True
            try:
                await self._session.close()
            except Exception as exc:
                logger.debug("receive stall session close ignored: %s", exc)
            return

    def _handle_session_lifecycle_message(self, response: Any) -> bool:
        update = getattr(response, "session_resumption_update", None)
        if update is not None:
            new_handle = getattr(update, "new_handle", None)
            if getattr(update, "resumable", False) and new_handle:
                self._resumption_handle = str(new_handle)
                self._resumption_failure_count = 0
                self._status.on_resumption_handle_updated()

        go_away = getattr(response, "go_away", None)
        if go_away is None:
            return False
        self._go_away_received = True
        logger.info(
            "Gemini Live GoAway received; reconnecting before close time_left=%s",
            getattr(go_away, "time_left", None),
        )
        return True

    def _record_usage_metadata(self, response: Any) -> None:
        usage_metadata = getattr(response, "usage_metadata", None)
        if usage_metadata is None and isinstance(response, dict):
            usage_metadata = response.get("usage_metadata")
        if usage_metadata is None:
            return
        self._status.record_usage_metadata(usage_metadata)

    async def _handle_message(self, response: Any) -> None:
        cancellation = getattr(response, "tool_call_cancellation", None)
        if cancellation is not None:
            await self._handle_tool_call_cancellation(cancellation)
            return

        tool_call = getattr(response, "tool_call", None)
        if tool_call is not None and getattr(tool_call, "function_calls", None):
            await self._dispatch_tool_calls(tool_call.function_calls)
            return

        server_content = getattr(response, "server_content", None)
        if server_content is None:
            return

        if getattr(server_content, "interrupted", False):
            await self.interrupt()
            return

        # Model output means a reply is in progress. Gemini 3.8 Live sends a
        # turn_complete right after a tool call, before the spoken answer, so
        # a silence timer armed by that early turn_complete must not end the
        # conversation mid-answer; the answer's own turn_complete re-arms it.
        if self._has_model_output(server_content):
            self._cancel_silence_timeout()

        # Forward any audio chunks if the caller wired up a sink. We do not
        # play them automatically; the StackChan TTS pipeline is the caller's
        # responsibility because it needs to wrap PCM in the Opus protocol.
        if self._on_audio is not None:
            audio = self._extract_audio_chunk(server_content)
            if audio:
                self._output.submit(lambda: self._on_audio(audio))
                await asyncio.sleep(0)

        # TEXT-mode utterances: buffer fragments, flush on turn_complete so a
        # single "好的, 我抬一下头" doesn't trigger six separate TTS calls.
        if self._on_text is not None:
            chunk = self._extract_text_chunk(server_content)
            if chunk:
                self._text_buf.append(chunk)
            segments = _pop_text_segments(
                self._text_buf,
                final=self._is_text_turn_complete(server_content),
            )
            for segment in segments:
                try:
                    await self._on_text(segment)
                except Exception:
                    logger.exception("on_text callback failed")

        # Voice backends need to know when Gemini finished speaking so they
        # can close out their tts.stop envelope. Fire even when text/audio
        # callbacks are off so the GeminiVoiceProxy works with audio only.
        if getattr(server_content, "turn_complete", False):
            silence_generation = self._silence_generation

            async def finish_turn() -> None:
                try:
                    if self._on_turn_complete is not None:
                        await self._on_turn_complete()
                except Exception:
                    logger.warning("Voice turn completion failed (details redacted)")
                    return
                # Cancelled completions never reach this point. Model completion
                # can precede seconds of device playback; newer output or input
                # also invalidates an older tool-turn's idle timer.
                if (
                    silence_generation == self._silence_generation
                    and not self._stop_event.is_set()
                ):
                    self._schedule_silence_timeout()

            self._output.submit(finish_turn)
            await asyncio.sleep(0)

    async def interrupt(self) -> None:
        """Discard old output before resetting the device sink for a new turn."""
        self._cancel_silence_timeout()
        self._text_buf.clear()
        await self._output.cancel()
        if self._on_interrupt is not None:
            await self._on_interrupt()

    @staticmethod
    def _has_model_output(server_content: Any) -> bool:
        model_turn = getattr(server_content, "model_turn", None)
        if model_turn is not None and getattr(model_turn, "parts", None):
            return True
        transcription = getattr(server_content, "output_transcription", None)
        return bool(getattr(transcription, "text", None))

    @staticmethod
    def _extract_audio_chunk(server_content: Any) -> bytes | None:
        model_turn = getattr(server_content, "model_turn", None)
        parts = getattr(model_turn, "parts", None) if model_turn is not None else None
        if not parts:
            return None
        buf = bytearray()
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is None:
                continue
            data = getattr(inline, "data", None)
            if data:
                buf.extend(data)
        return bytes(buf) if buf else None

    @staticmethod
    def _extract_text_chunk(server_content: Any) -> str | None:
        transcript = getattr(server_content, "output_transcription", None)
        text = getattr(transcript, "text", None) if transcript is not None else None
        if isinstance(text, str) and text:
            return text

        model_turn = getattr(server_content, "model_turn", None)
        parts = getattr(model_turn, "parts", None) if model_turn is not None else None
        if not parts:
            return None
        out: list[str] = []
        for part in parts:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                out.append(text)
        return "".join(out) if out else None

    @staticmethod
    def _is_text_turn_complete(server_content: Any) -> bool:
        if bool(getattr(server_content, "turn_complete", False)):
            return True
        transcript = getattr(server_content, "output_transcription", None)
        return bool(getattr(transcript, "finished", False))

    async def _dispatch_tool_calls(self, function_calls: list[Any]) -> None:
        from google.genai import types

        results = await self.dispatch_calls(function_calls)
        if self._session is not None:
            await self._session.send_tool_response(
                function_responses=[
                    types.FunctionResponse(**result) for result in results
                ]
            )

    async def inject_user_text(self, text: str) -> bool:
        """Inject a plain user turn into the active Live session."""
        session = self._session
        if session is None:
            return False
        await session.send_realtime_input(text=text)
        return True

    async def send_audio(self, pcm_16khz: bytes) -> None:
        """Forward a 16 kHz mono PCM frame to Gemini Live."""
        self._cancel_silence_timeout()
        async with self._audio_out_lock:
            if self._session is None or self._go_away_received:
                self._buffer_reconnect_audio(pcm_16khz)
                return
            await self._emit_pending_locked()
            await self._ensure_manual_vad_activity_open()
            await self._send_pcm_locked(pcm_16khz)

    def _buffer_reconnect_audio(self, pcm_16khz: bytes) -> None:
        """Hold gated audio while the Live session is down, then replay or drop."""
        if not pcm_16khz:
            return
        now = self._mono_clock()
        if self._pending_audio_since is None:
            self._pending_audio_since = now
            self._session_dead_notified = False
            self._schedule_reconnect_audio_timeout()
        if now - self._pending_audio_since >= self._reconnect_audio_ttl_s:
            self._drop_reconnect_audio(dead=True)
            return
        self._pending_audio.append(pcm_16khz)
        self._pending_audio_bytes += len(pcm_16khz)
        while (
            self._pending_audio_bytes > self._reconnect_audio_max_bytes
            and self._pending_audio
        ):
            dropped = self._pending_audio.popleft()
            self._pending_audio_bytes -= len(dropped)

    def _schedule_reconnect_audio_timeout(self) -> None:
        self._cancel_reconnect_audio_timeout()
        if self._reconnect_audio_ttl_s <= 0:
            return
        try:
            self._reconnect_audio_timeout_task = asyncio.create_task(
                self._run_reconnect_audio_timeout(),
                name="gemini-reconnect-audio-timeout",
            )
        except RuntimeError:
            self._reconnect_audio_timeout_task = None

    def _cancel_reconnect_audio_timeout(self) -> None:
        task = self._reconnect_audio_timeout_task
        self._reconnect_audio_timeout_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _run_reconnect_audio_timeout(self) -> None:
        try:
            await asyncio.sleep(self._reconnect_audio_ttl_s)
            if self._session is None or self._go_away_received:
                self._drop_reconnect_audio(dead=True)
        except asyncio.CancelledError:
            return

    def _clear_reconnect_audio(self) -> None:
        self._pending_audio.clear()
        self._pending_audio_bytes = 0
        self._pending_audio_since = None
        self._cancel_reconnect_audio_timeout()

    def _drop_reconnect_audio(self, *, dead: bool) -> None:
        self._clear_reconnect_audio()
        if not dead or self._session_dead_notified:
            return
        self._session_dead_notified = True
        callback = self._on_session_dead
        if callback is None:
            return
        try:
            result = callback()
        except Exception:
            logger.exception("Gemini Live session-dead callback failed")
            return
        if inspect.isawaitable(result):
            task = asyncio.create_task(result, name="gemini-session-dead")
            task.add_done_callback(_log_session_dead_task)

    async def _flush_reconnect_audio(self) -> None:
        async with self._audio_out_lock:
            await self._emit_pending_locked()

    async def _emit_pending_locked(self) -> None:
        if not self._pending_audio or self._session is None or self._go_away_received:
            return
        frames = list(self._pending_audio)
        self._clear_reconnect_audio()
        self._session_dead_notified = False
        await self._ensure_manual_vad_activity_open()
        for frame in frames:
            await self._send_pcm_locked(frame)

    async def _send_pcm_locked(self, pcm_16khz: bytes) -> None:
        from google.genai import types  # noqa: PLC0415

        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm_16khz, mime_type="audio/pcm;rate=16000"),
        )

    async def send_keepalive_audio(self, pcm_16khz: bytes) -> None:
        """Send a keepalive silent frame.

        Unlike :meth:`send_audio`, this does not cancel the conversation silence timer: keepalive frames are
        made up by the gateway, do not mean the user is speaking, and must not affect the end_conversation
        silence check.
        """
        if self._session is None or self._go_away_received:
            return
        from google.genai import types  # noqa: PLC0415

        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm_16khz, mime_type="audio/pcm;rate=16000"),
        )

    async def clear_conversation_context(self) -> None:
        """Clear local per-turn state while keeping the Live socket open."""
        self._text_buf.clear()
        self._cancel_silence_timeout()

    def _schedule_silence_timeout(self) -> None:
        if getattr(self._esp32, "on_end_conversation", None) is None:
            return
        self._cancel_silence_timeout()
        if self._conversation_idle_timeout_s <= 0:
            return
        try:
            self._silence_timeout_task = asyncio.create_task(
                self._run_silence_timeout(),
                name="gemini-conversation-silence-timeout",
            )
        except RuntimeError:
            self._silence_timeout_task = None

    def _cancel_silence_timeout(self) -> None:
        self._silence_generation += 1
        task = self._silence_timeout_task
        self._silence_timeout_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _run_silence_timeout(self) -> None:
        try:
            await asyncio.sleep(self._conversation_idle_timeout_s)
            await self._dispatch_end_conversation()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Gemini Live silence timeout failed")

    async def send_activity_start(self) -> None:
        """Manual VAD: mark the start of user activity (only with automatic VAD disabled)."""
        if self._session is None or self._go_away_received:
            return
        if not manual_vad_enabled() or self._activity_open:
            return
        from google.genai import types  # noqa: PLC0415

        await self._session.send_realtime_input(activity_start=types.ActivityStart())
        self._activity_open = True

    async def send_activity_end(self) -> None:
        """Manual VAD: mark the end of user activity."""
        if self._session is None or self._go_away_received:
            return
        if not manual_vad_enabled() or not self._activity_open:
            return
        from google.genai import types  # noqa: PLC0415

        await self._session.send_realtime_input(activity_end=types.ActivityEnd())
        self._activity_open = False

    async def send_audio_stream_end(self) -> None:
        """Signal end-of-utterance so Gemini commits the turn."""
        if self._session is None or self._go_away_received:
            return
        if manual_vad_enabled():
            await self.send_activity_end()
            return
        await self._session.send_realtime_input(audio_stream_end=True)

    @property
    def uses_manual_vad(self) -> bool:
        return manual_vad_enabled()

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        """Block until the session is open (used by tests / orchestration)."""
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        """Tear down the receive loop and close the Live session."""
        self._stop_event.set()
        await self._output.cancel()
        self._cancel_silence_timeout()
        self._clear_reconnect_audio()
        await self.close()
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._connected_event.clear()
