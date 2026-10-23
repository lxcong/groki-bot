"""Unit tests for the Gemini Live bridge.

Tests don't open a real Live WebSocket — they validate the in-process
contract: tool-name to ESP32-tool mapping, argument clamping, error
handling, and system instruction composition.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest

from stackchan_mcp.gemini_live_bridge import (
    AVATAR_FACE_ENUM,
    DEFAULT_SYSTEM_INSTRUCTION,
    EMOTION_INTENSITY_ENUM,
    EMOTION_MOOD_ENUM,
    GeminiLiveBridge,
    MEDIA_ACTION_ENUM,
    TOOL_MAP,
    TOOL_METADATA,
    build_function_declarations,
    build_live_config,
    build_system_instruction,
    default_system_instruction,
)


def _fake_claude_cli(directory) -> str:
    binary = directory / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return str(binary)


@pytest.fixture(autouse=True)
def _all_gemini_tools_enabled(request, monkeypatch, tmp_path):
    """Most tests here cover the full tool set (MCP-capable firmware + Mac
    control + an installed claude CLI). Tests marked ``defaults`` see the
    shipped defaults on a machine without the claude CLI instead.
    The Grok Bot hand-off is off in both unless a test turns it on."""
    monkeypatch.delenv("STACKCHAN_TOOL_BOT", raising=False)
    monkeypatch.delenv("STACKCHAN_TOOL_BOT_ID", raising=False)
    monkeypatch.delenv("STACKCHAN_TOOL_BOT_PREFIX", raising=False)
    monkeypatch.delenv("STACKCHAN_ASK_CLAUDE", raising=False)
    monkeypatch.delenv("STACKCHAN_CLAUDE_MODEL", raising=False)
    if request.node.get_closest_marker("defaults"):
        monkeypatch.delenv("STACKCHAN_GEMINI_DEVICE_TOOLS", raising=False)
        monkeypatch.delenv("STACKCHAN_MAC_CONTROL", raising=False)
        monkeypatch.setenv("STACKCHAN_CLAUDE_BIN", str(tmp_path / "no-claude-here"))
    else:
        monkeypatch.setenv("STACKCHAN_GEMINI_DEVICE_TOOLS", "1")
        monkeypatch.setenv("STACKCHAN_MAC_CONTROL", "1")
        monkeypatch.setenv("STACKCHAN_CLAUDE_BIN", _fake_claude_cli(tmp_path))


@pytest.mark.defaults
def test_default_declarations_are_voice_only_without_mac_control():
    """Shipped defaults: no face/LED/head tools (Groki Bot firmware has no
    device MCP) and no Mac control (voice must not drive the computer unless
    the owner opts in)."""
    from stackchan_mcp.mac_control import MAC_TOOL_NAMES

    names = [decl.name for decl in build_function_declarations()]
    assert names == ["end_conversation", "get_current_datetime"]
    assert not set(names) & (set(TOOL_MAP) | MAC_TOOL_NAMES | {"express_emotion"})


@pytest.mark.defaults
def test_default_system_instruction_matches_declared_tools(monkeypatch):
    import stackchan_mcp.gemini_live_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "_load_personality", lambda: "")
    instruction = build_system_instruction(personality_loader=lambda: "")
    assert instruction == default_system_instruction()
    assert "express_emotion" not in instruction
    assert "run_mac_task" not in instruction
    assert "end_conversation" in instruction
    assert "ask_claude" not in instruction


@pytest.mark.defaults
def test_ask_claude_offered_when_claude_cli_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("STACKCHAN_CLAUDE_BIN", _fake_claude_cli(tmp_path))

    names = [decl.name for decl in build_function_declarations()]
    assert names == ["end_conversation", "ask_claude", "get_current_datetime"]
    assert "ask_claude(question)" in default_system_instruction()


@pytest.mark.defaults
@pytest.mark.parametrize("value", ["0", "false", "off"])
def test_ask_claude_can_be_turned_off_with_cli_installed(monkeypatch, tmp_path, value):
    monkeypatch.setenv("STACKCHAN_CLAUDE_BIN", _fake_claude_cli(tmp_path))
    monkeypatch.setenv("STACKCHAN_ASK_CLAUDE", value)

    names = [decl.name for decl in build_function_declarations()]
    assert "ask_claude" not in names
    assert "ask_claude" not in default_system_instruction()
    assert "get_current_datetime" in default_system_instruction()


@pytest.mark.defaults
def test_mac_controller_is_not_created_by_default():
    bridge = GeminiLiveBridge(esp32=FakeESP32(), api_key="test")
    assert bridge._mac is None


class FakeESP32:
    def __init__(self, connected: bool = True, error: dict | None = None) -> None:
        self.device_connected = connected
        self.calls: list[tuple[str, dict]] = []
        self._error = error

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._error is not None:
            return None, self._error
        return {"content": [{"type": "text", "text": '{"ok": true}'}]}, None


class FakeVoiceOnlyESP32(FakeESP32):
    class Connection:
        mcp_supported = False

    connection = Connection()

    def __init__(self) -> None:
        super().__init__(connected=True)
        self.emotions: list[str] = []
        self.leds: list[tuple[int, int, int]] = []
        self.heads: list[tuple[int, int, int]] = []

    async def send_emotion(self, emotion: str, *, notify_activity: bool = True):
        self.emotions.append(emotion)
        return {"ok": True, "emotion": emotion}, None

    async def send_led(
        self,
        r: int,
        g: int,
        b: int,
        *,
        notify_activity: bool = True,
    ):
        self.leds.append((r, g, b))
        return {"ok": True, "r": r, "g": g, "b": b}, None

    async def send_head(
        self,
        yaw: int,
        pitch: int,
        speed: int,
        *,
        notify_activity: bool = True,
        notify_head_command: bool = True,
    ):
        self.heads.append((yaw, pitch, speed))
        return {"ok": True, "yaw": yaw, "pitch": pitch, "speed": speed}, None


def _bridge(**kwargs):
    return GeminiLiveBridge(
        FakeESP32(),
        api_key=kwargs.pop("api_key", "test-key"),
        **kwargs,
    )


def _modality_values(values) -> list[str]:
    return [getattr(value, "value", value) for value in values]


@pytest.fixture(autouse=True)
def fake_google_genai(monkeypatch):
    try:
        import google.genai  # noqa: F401, PLC0415

        return
    except ModuleNotFoundError:
        pass

    class _Type:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    types_mod = ModuleType("google.genai.types")
    for name in (
        "AudioTranscriptionConfig",
        "AutomaticActivityDetection",
        "Blob",
        "Content",
        "ContextWindowCompressionConfig",
        "FunctionDeclaration",
        "FunctionResponse",
        "GoogleSearch",
        "LiveConnectConfig",
        "Part",
        "RealtimeInputConfig",
        "PrebuiltVoiceConfig",
        "Schema",
        "SessionResumptionConfig",
        "SlidingWindow",
        "SpeechConfig",
        "Tool",
        "VoiceConfig",
    ):
        setattr(types_mod, name, type(name, (_Type,), {}))

    genai_mod = ModuleType("google.genai")
    genai_mod.types = types_mod
    google_mod = ModuleType("google")
    google_mod.genai = genai_mod
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)


XIAOZHI_TOOLS = {
    "get_device_status",
    "set_device_volume",
    "set_screen_brightness",
    "set_screen_theme",
    "start_game",
    "stop_game",
    "rename_assistant",
    "design_avatar",
    "apply_avatar",
    "discard_avatar_draft",
}
# design_avatar / apply_avatar / discard_avatar_draft are declared only where the workspace's avatar
# generator and a packaged assets image exist (stackchan_mcp.avatar.available()).
from stackchan_mcp import avatar as _avatar  # noqa: E402

AVATAR_TOOLS = ["design_avatar", "apply_avatar", "discard_avatar_draft"] if _avatar.available() else []


def test_tool_map_covers_voice_backend_tools():
    """Gemini-side public API: three physical control tools plus the xiaozhi firmware tools."""
    assert set(TOOL_MAP) == {
        "move_head",
        "set_avatar",
        "set_all_leds",
    } | XIAOZHI_TOOLS
    assert TOOL_MAP["set_device_volume"] == "self.audio_speaker.set_volume"
    assert TOOL_MAP["get_device_status"] == "self.get_device_status"
    assert TOOL_MAP["move_head"] == "self.robot.set_head_angles"
    assert TOOL_MAP["set_avatar"] == "self.display.set_avatar"
    assert TOOL_MAP["set_all_leds"] == "self.led.set_all"


def test_tool_map_unchanged():
    assert set(TOOL_MAP) == {
        "move_head",
        "set_avatar",
        "set_all_leds",
    } | XIAOZHI_TOOLS
    assert "set_background_color" not in TOOL_MAP
    assert "ask_claude" not in TOOL_MAP
    assert "get_current_datetime" not in TOOL_MAP


def test_build_function_declarations_match_tool_map():
    from stackchan_mcp.mac_control import MAC_TOOL_NAMES

    decls = build_function_declarations()
    names = {decl.name for decl in decls}
    assert names == (set(TOOL_MAP) - {"design_avatar", "apply_avatar", "discard_avatar_draft"} | set(AVATAR_TOOLS)) | MAC_TOOL_NAMES | {
        "express_emotion",
        "end_conversation",
        "ask_claude",
        "get_current_datetime",
    }


def test_build_function_declarations_includes_agent_tools():
    decls = build_function_declarations()
    names = [decl.name for decl in decls]
    assert names[:14 + len(AVATAR_TOOLS)] == [
        "move_head",
        "set_avatar",
        "set_all_leds",
        "express_emotion",
        "get_device_status",
        "set_device_volume",
        "set_screen_brightness",
        "set_screen_theme",
        "start_game",
        "stop_game",
        "rename_assistant",
        *AVATAR_TOOLS,
        "end_conversation",
        "ask_claude",
        "get_current_datetime",
    ]
    by_name = {decl.name: decl for decl in decls}
    ask_claude = by_name["ask_claude"]
    get_datetime = by_name["get_current_datetime"]
    assert ask_claude.parameters.required == ["question"]
    assert "question" in ask_claude.parameters.properties
    assert get_datetime.parameters.properties == {}


def test_build_function_declarations_mac_tools_have_required_args():
    decls = build_function_declarations()
    by_name = {decl.name: decl for decl in decls}
    assert by_name["open_app"].parameters.required == ["name"]
    assert by_name["open_url"].parameters.required == ["url"]
    assert by_name["web_search"].parameters.required == ["query"]
    assert by_name["media_control"].parameters.required == ["action"]
    media_action = by_name["media_control"].parameters.properties["action"]
    assert media_action.format == "enum"
    assert media_action.enum == MEDIA_ACTION_ENUM
    assert by_name["run_shortcut"].parameters.required == ["name"]
    assert by_name["run_mac_task"].parameters.required == ["task"]
    assert by_name["lock_screen"].parameters.properties == {}
    assert by_name["check_mac_task"].parameters.properties == {}


def test_function_declarations_use_structured_enums():
    decls = build_function_declarations()
    by_name = {decl.name: decl for decl in decls}

    face = by_name["set_avatar"].parameters.properties["face"]
    assert face.format == "enum"
    assert face.enum == AVATAR_FACE_ENUM

    express = by_name["express_emotion"]
    mood = express.parameters.properties["mood"]
    intensity = express.parameters.properties["intensity"]
    assert mood.format == "enum"
    assert mood.enum == EMOTION_MOOD_ENUM
    assert intensity.format == "enum"
    assert intensity.enum == EMOTION_INTENSITY_ENUM

    media_action = by_name["media_control"].parameters.properties["action"]
    assert media_action.format == "enum"
    assert media_action.enum == MEDIA_ACTION_ENUM


def test_express_emotion_declaration_mentions_enum_and_once_per_turn():
    decls = build_function_declarations()
    express = next(decl for decl in decls if decl.name == "express_emotion")

    assert "excited" in express.description
    assert "apologetic" in express.description
    assert "at most once per reply turn" in express.description
    assert express.parameters.required == ["mood"]


def test_mac_tool_declarations_include_response_schema():
    decls = build_function_declarations()
    by_name = {decl.name: decl for decl in decls}

    media_response = by_name["media_control"].response
    assert media_response.required == ["ok"]
    assert media_response.properties["ok"].type == "BOOLEAN"
    assert media_response.properties["action"].format == "enum"
    assert media_response.properties["action"].enum == MEDIA_ACTION_ENUM
    assert "player" in media_response.properties
    assert "player_state" in media_response.properties
    assert "error" in media_response.properties

    task_response = by_name["run_mac_task"].response
    assert task_response.required == ["ok"]
    assert task_response.properties["ok"].type == "BOOLEAN"
    assert task_response.properties["status"].format == "enum"
    assert task_response.properties["status"].enum == ["started"]
    assert "task_id" in task_response.properties
    assert "estimated_seconds" in task_response.properties
    assert "user_message" in task_response.properties


def test_build_live_config_includes_system_instruction_and_tools():
    cfg = build_live_config(system_instruction="你好")
    assert cfg.system_instruction.parts[0].text == "你好"
    assert _modality_values(cfg.response_modalities) == ["AUDIO"]
    assert len(cfg.tools) == 2
    assert cfg.tools[0].google_search is not None
    assert len(cfg.tools[1].function_declarations) == 25 + len(AVATAR_TOOLS)


def test_build_live_config_includes_google_search():
    cfg = build_live_config(system_instruction="你好")
    assert len(cfg.tools) == 2
    assert cfg.tools[0].google_search is not None
    assert cfg.tools[1].function_declarations


def test_build_live_config_enables_session_resumption():
    cfg = build_live_config(system_instruction="你好")
    assert cfg.session_resumption is not None
    assert cfg.session_resumption.handle is None


def test_build_live_config_enables_context_compression_and_manual_vad(monkeypatch):
    monkeypatch.delenv("STACKCHAN_CTX_TRIGGER_TOKENS", raising=False)
    monkeypatch.delenv("STACKCHAN_CTX_TARGET_TOKENS", raising=False)
    monkeypatch.delenv("STACKCHAN_VAD_SILENCE_MS", raising=False)
    monkeypatch.delenv("STACKCHAN_MANUAL_VAD", raising=False)

    cfg = build_live_config(system_instruction="你好")

    assert cfg.context_window_compression.trigger_tokens == 25_000
    assert cfg.context_window_compression.sliding_window.target_tokens == 8_000
    vad = cfg.realtime_input_config.automatic_activity_detection
    assert vad.disabled is True


def test_build_live_config_uses_context_and_automatic_vad_env(monkeypatch):
    monkeypatch.setenv("STACKCHAN_CTX_TRIGGER_TOKENS", "30000")
    monkeypatch.setenv("STACKCHAN_CTX_TARGET_TOKENS", "9000")
    monkeypatch.setenv("STACKCHAN_VAD_SILENCE_MS", "750")
    monkeypatch.setenv("STACKCHAN_MANUAL_VAD", "0")

    cfg = build_live_config(system_instruction="你好")

    assert cfg.context_window_compression.trigger_tokens == 30_000
    assert cfg.context_window_compression.sliding_window.target_tokens == 9_000
    vad = cfg.realtime_input_config.automatic_activity_detection
    assert vad.silence_duration_ms == 750


def test_build_live_config_uses_session_resumption_handle():
    cfg = build_live_config(
        system_instruction="你好",
        session_resumption_handle="resume-1",
    )
    assert cfg.session_resumption.handle == "resume-1"


def test_system_instruction_tells_gemini_how_to_exit_conversation():
    assert "end_conversation" in DEFAULT_SYSTEM_INSTRUCTION
    assert "clearly says goodbye" in DEFAULT_SYSTEM_INSTRUCTION


def test_system_instruction_tells_gemini_to_express_emotion_before_speaking():
    assert "express_emotion" in DEFAULT_SYSTEM_INSTRUCTION
    assert "Before you start each spoken reply" in DEFAULT_SYSTEM_INSTRUCTION
    assert 'mood="tired"' in DEFAULT_SYSTEM_INSTRUCTION
    assert 'mood="excited"' in DEFAULT_SYSTEM_INSTRUCTION


def test_system_instruction_tells_gemini_how_to_use_agent_tools():
    assert "You can search the web" in DEFAULT_SYSTEM_INSTRUCTION
    assert "ask_claude" in DEFAULT_SYSTEM_INSTRUCTION
    assert "get_current_datetime" in DEFAULT_SYSTEM_INSTRUCTION
    assert "media_control(action)" in DEFAULT_SYSTEM_INSTRUCTION
    assert "Prefer play or pause" in DEFAULT_SYSTEM_INSTRUCTION
    assert "player_state" in DEFAULT_SYSTEM_INSTRUCTION
    assert "check_mac_task" in DEFAULT_SYSTEM_INSTRUCTION


def test_system_instruction_appends_personality(monkeypatch):
    """The personality file is appended below the base operational rules."""

    def fake_loader() -> str:
        return "我是桌上的小机器人，好奇又有点害羞。"

    instruction = build_system_instruction(personality_loader=fake_loader)
    assert default_system_instruction() in instruction
    assert "我是桌上的小机器人" in instruction
    assert "# Personality" in instruction


def test_personality_file_is_appended_once(monkeypatch, tmp_path):
    """The personality file is appended below the rules, exactly once, and the
    built-in persona stays (append, not replace)."""
    import stackchan_mcp.gemini_live_bridge as bridge_mod

    persona = "My name is Pip. I answer in English."
    path = tmp_path / "personality.md"
    path.write_text(persona + "\n", encoding="utf-8")
    monkeypatch.setenv("STACKCHAN_PERSONALITY_FILE", str(path))

    instruction = build_system_instruction()

    assert instruction.count(persona) == 1
    assert instruction.index("# Personality") < instruction.index(persona)
    assert instruction.startswith(bridge_mod._load_personality())


@pytest.mark.defaults
def test_builtin_persona_is_named_groki():
    instruction = default_system_instruction()
    assert instruction.startswith("You are Groki,")
    assert "你是 Grok" not in instruction


@pytest.mark.defaults
def test_builtin_persona_does_not_hide_the_model():
    import re

    instruction = default_system_instruction()
    assert not re.search(r"不要说\s*Gemini|不要提\s*Gemini", instruction)
    assert not re.search(r"(don't|do not|never)\s+(say|mention|reveal)", instruction, re.I)
    assert "which AI model you run on, answer truthfully" in instruction


@pytest.mark.defaults
def test_builtin_persona_follows_the_users_language():
    import stackchan_mcp.gemini_live_bridge as bridge_mod

    persona = bridge_mod._load_personality()
    assert "中文" not in persona
    assert "Chinese" not in persona
    assert "Reply in the language the user speaks" in persona


def test_personality_file_can_override_the_builtin_persona(monkeypatch, tmp_path):
    path = tmp_path / "personality.md"
    path.write_text("Always speak English. Your name is Pip.\n", encoding="utf-8")
    monkeypatch.setenv("STACKCHAN_PERSONALITY_FILE", str(path))

    instruction = build_system_instruction()

    assert '"# Personality" section follows, it overrides this paragraph' in instruction
    assert instruction.rstrip().endswith("Always speak English. Your name is Pip.")


def test_system_instruction_without_personality_is_just_base():
    """Empty personality file returns the base instruction unchanged."""
    instruction = build_system_instruction(personality_loader=lambda: "")
    assert instruction == default_system_instruction()


def test_normalize_move_head_clamps_yaw_pitch_speed():
    """Firmware speed range is 100..1000 with 150 natural — not 1..100."""
    args = GeminiLiveBridge._normalize_args(
        "move_head",
        {"yaw": 200, "pitch": -10, "speed": 9999},
    )
    assert args == {"yaw": 90, "pitch": 0, "speed": 1000}


def test_normalize_move_head_defaults_speed_to_natural():
    """When Gemini omits speed, use the firmware-recommended 150."""
    args = GeminiLiveBridge._normalize_args(
        "move_head",
        {"yaw": 30, "pitch": 20},
    )
    assert args["speed"] == 150


def test_normalize_move_head_floor_at_firmware_min():
    """speed=10 (Gemini guess) must clamp up to 100, not stay 10."""
    args = GeminiLiveBridge._normalize_args(
        "move_head",
        {"yaw": 0, "pitch": 0, "speed": 10},
    )
    assert args["speed"] == 100


def test_normalize_set_all_leds_clamps_channels():
    args = GeminiLiveBridge._normalize_args(
        "set_all_leds",
        {"r": 300, "g": -5, "b": 128},
    )
    assert args == {"r": 255, "g": 0, "b": 128}


@pytest.mark.asyncio
async def test_dispatch_tool_routes_to_esp32():
    bridge = _bridge()
    result = await bridge._dispatch_tool("move_head", {"yaw": 30, "pitch": 50, "speed": 200})
    assert result["ok"] is True
    assert bridge._esp32.calls == [
        ("self.robot.set_head_angles", {"yaw": 30, "pitch": 50, "speed": 200}),
    ]


def test_face_tracking_tools_hidden_when_tracker_autostart_is_off(monkeypatch):
    monkeypatch.setenv("STACKCHAN_FACE_TRACKER_AUTOSTART", "0")
    names = {decl.name for decl in build_function_declarations()}
    assert not names & {"self.tracking.start", "self.tracking.stop"}


@pytest.mark.defaults
def test_face_tracking_tools_offered_with_shipped_defaults(monkeypatch):
    """The face tracker starts with the gateway by default, so voice can switch head follow."""
    monkeypatch.delenv("STACKCHAN_FACE_TRACKER_AUTOSTART", raising=False)
    names = [decl.name for decl in build_function_declarations()]
    assert names == [
        "end_conversation",
        "get_current_datetime",
        "self.tracking.start",
        "self.tracking.stop",
    ]
    by_name = {decl.name: decl for decl in build_function_declarations()}
    assert by_name["self.tracking.start"].parameters.properties == {}
    assert "look at me" in by_name["self.tracking.start"].description
    assert TOOL_METADATA["self.tracking.start"].exclusive_group == "face_tracking"
    assert TOOL_METADATA["self.tracking.stop"].exclusive_group == "face_tracking"


@pytest.mark.asyncio
async def test_voice_tracking_tools_use_gateway_local_switch_without_device():
    calls = []

    async def switch(enabled):
        calls.append(enabled)
        return {"ok": True, "enabled": enabled}

    bridge = GeminiLiveBridge(FakeESP32(), api_key="k", set_face_tracking=switch)
    assert await bridge._dispatch_tool("self.tracking.start", {}) == {"ok": True, "enabled": True}
    assert await bridge._dispatch_tool("self.tracking.stop", {}) == {"ok": True, "enabled": False}
    assert calls == [True, False]
    assert bridge._esp32.calls == []


@pytest.mark.asyncio
async def test_voice_tracking_tools_without_switch_report_unavailable():
    bridge = GeminiLiveBridge(FakeESP32(), api_key="k")
    assert await bridge._dispatch_tool("self.tracking.start", {}) == {
        "ok": False, "error": "face tracking unavailable",
    }


@pytest.mark.asyncio
async def test_gemini_function_calls_return_tracking_switch_results():
    from types import SimpleNamespace

    results = []

    async def switch(enabled):
        return {"ok": True, "enabled": enabled}

    class Session:
        async def send_tool_response(self, *, function_responses):
            results.extend(function_responses)

    bridge = GeminiLiveBridge(FakeESP32(), api_key="k", set_face_tracking=switch)
    bridge._session = Session()
    await bridge._dispatch_tool_calls([
        SimpleNamespace(id="start-1", name="self.tracking.start", args={}),
        SimpleNamespace(id="stop-1", name="self.tracking.stop", args={}),
    ])
    assert [(r.id, r.response) for r in results] == [
        ("start-1", {"ok": True, "enabled": True}),
        ("stop-1", {"ok": True, "enabled": False}),
    ]


@pytest.mark.asyncio
async def test_dispatch_set_background_color_is_unknown():
    bridge = _bridge()
    result = await bridge._dispatch_tool(
        "set_background_color",
        {"r": 255, "g": 0, "b": 0},
    )
    assert result == {"ok": False, "error": "unknown tool set_background_color"}
    assert bridge._esp32.calls == []


@pytest.mark.asyncio
async def test_dispatch_tool_unknown_returns_error():
    bridge = _bridge()
    result = await bridge._dispatch_tool("self_destruct", {})
    assert result["ok"] is False
    assert "unknown tool" in result["error"]
    assert bridge._esp32.calls == []


@pytest.mark.asyncio
async def test_dispatch_end_conversation_calls_gateway_handler():
    called = 0

    async def on_end_conversation():
        nonlocal called
        called += 1
        return {"ok": True}

    esp32 = FakeESP32()
    esp32.on_end_conversation = on_end_conversation
    bridge = GeminiLiveBridge(esp32, api_key="k")

    result = await bridge._dispatch_tool("end_conversation", {})

    assert result["ok"] is True
    assert called == 1
    assert esp32.calls == []


@pytest.mark.asyncio
async def test_dispatch_end_conversation_calls_extra_callback():
    called = 0
    extra_called = 0

    async def on_end_conversation():
        nonlocal called
        called += 1
        return {"ok": True}

    async def extra_callback():
        nonlocal extra_called
        extra_called += 1

    esp32 = FakeESP32()
    esp32.on_end_conversation = on_end_conversation
    bridge = GeminiLiveBridge(
        esp32,
        api_key="k",
        on_end_conversation=extra_callback,
    )

    result = await bridge._dispatch_tool("end_conversation", {})

    assert result["ok"] is True
    assert called == 1
    assert extra_called == 1


@pytest.mark.asyncio
async def test_dispatch_set_all_leds_uses_direct_led_when_ws_has_no_mcp():
    esp32 = FakeVoiceOnlyESP32()
    bridge = GeminiLiveBridge(esp32, api_key="k")

    result = await bridge._dispatch_tool(
        "set_all_leds",
        {"r": 0, "g": 180, "b": 180},
    )

    assert result["ok"] is True
    assert result["led"] == {"r": 0, "g": 180, "b": 180}
    assert esp32.calls == []
    assert esp32.leds == [(0, 180, 180)]


@pytest.mark.asyncio
async def test_dispatch_move_head_uses_direct_head_when_ws_has_no_mcp():
    esp32 = FakeVoiceOnlyESP32()
    head_commands = 0

    def on_head_command():
        nonlocal head_commands
        head_commands += 1

    bridge = GeminiLiveBridge(
        esp32,
        api_key="k",
        on_head_command=on_head_command,
    )

    result = await bridge._dispatch_tool(
        "move_head",
        {"yaw": 30, "pitch": 50, "speed": 200},
    )

    assert result["ok"] is True
    assert result["head"] == {"yaw": 30, "pitch": 50, "speed": 200}
    assert esp32.calls == []
    assert esp32.heads == [(30, 50, 200)]
    assert head_commands == 1


@pytest.mark.asyncio
async def test_dispatch_set_all_leds_deferred_during_listening(caplog):
    esp32 = FakeVoiceOnlyESP32()
    bridge = GeminiLiveBridge(
        esp32,
        api_key="k",
        wake_gate_state_getter=lambda: "LISTENING",
    )

    with caplog.at_level(logging.INFO, logger="stackchan_mcp.voice_tools"):
        result = await bridge._dispatch_tool(
            "set_all_leds",
            {"r": 0, "g": 0, "b": 0},
        )

    assert result == {"ok": True, "deferred": "listening_status_light_active"}
    assert esp32.leds == []
    assert esp32.calls == []
    assert "Gemini Live set_all_leds deferred" in caplog.text
    assert "deferred=listening_status_light_active" in caplog.text


@pytest.mark.asyncio
async def test_dispatch_set_all_leds_passes_during_dormant():
    esp32 = FakeVoiceOnlyESP32()
    bridge = GeminiLiveBridge(
        esp32,
        api_key="k",
        wake_gate_state_getter=lambda: "DORMANT",
    )

    result = await bridge._dispatch_tool(
        "set_all_leds",
        {"r": 255, "g": 0, "b": 0},
    )

    assert result["ok"] is True
    assert result["led"] == {"r": 255, "g": 0, "b": 0}
    assert esp32.leds == [(255, 0, 0)]


@pytest.mark.asyncio
async def test_dispatch_set_all_leds_without_direct_led_reports_channel_unavailable():
    class VoiceOnlyWithoutLed(FakeESP32):
        class Connection:
            mcp_supported = False

        connection = Connection()

    bridge = GeminiLiveBridge(VoiceOnlyWithoutLed(), api_key="k")

    result = await bridge._dispatch_tool("set_all_leds", {"r": 1, "g": 2, "b": 3})

    assert result == {"ok": False, "error": "device led channel unavailable"}


@pytest.mark.asyncio
async def test_dispatch_move_head_without_direct_head_reports_channel_unavailable():
    class VoiceOnlyWithoutHead(FakeESP32):
        class Connection:
            mcp_supported = False

        connection = Connection()

    bridge = GeminiLiveBridge(VoiceOnlyWithoutHead(), api_key="k")

    result = await bridge._dispatch_tool("move_head", {"yaw": 1, "pitch": 2})

    assert result == {"ok": False, "error": "device head channel unavailable"}


@pytest.mark.asyncio
async def test_dispatch_express_emotion_schedules_body_commands_and_records_face():
    bridge = _bridge()

    result = await bridge._dispatch_tool(
        "express_emotion",
        {"mood": "tired", "intensity": "low"},
    )
    tasks = list(bridge._emotion_tasks)
    await asyncio.gather(*tasks)

    assert result["ok"] is True
    assert result["face"] == "sleeping"
    assert bridge.current_turn_emotion_face == "sleeping"
    calls = dict(bridge._esp32.calls)
    assert calls["self.display.set_avatar"] == {"face": "sleeping"}
    assert calls["self.led.set_all"] == {"r": 18, "g": 28, "b": 68}
    assert calls["self.robot.set_head_angles"] == {"yaw": 0, "pitch": 9, "speed": 450}


@pytest.mark.asyncio
async def test_curious_emotion_uses_doubt_not_thinking():
    bridge = _bridge()

    result = await bridge._dispatch_tool(
        "express_emotion",
        {"mood": "curious", "intensity": "high"},
    )
    await asyncio.gather(*list(bridge._emotion_tasks))

    assert result["ok"] is True
    assert result["face"] == "doubt"
    assert result["face"] != "thinking"
    calls = dict(bridge._esp32.calls)
    assert calls["self.display.set_avatar"] == {"face": "doubt"}


@pytest.mark.asyncio
async def test_ask_claude_sets_thinking_face_while_processing():
    bridge = _bridge()
    bridge._claude_bin = "/nonexistent/claude"

    result = await bridge._dispatch_ask_claude({"question": "分析一下"})
    await asyncio.sleep(0)

    assert result["ok"] is False
    assert any(
        name == "self.display.set_avatar" and arguments.get("face") == "thinking"
        for name, arguments in bridge._esp32.calls
    )


@pytest.mark.asyncio
async def test_dispatch_express_emotion_uses_llm_emotion_when_ws_has_no_mcp():
    esp32 = FakeVoiceOnlyESP32()
    bridge = GeminiLiveBridge(esp32, api_key="k")

    result = await bridge._dispatch_tool(
        "express_emotion",
        {"mood": "tired", "intensity": "low"},
    )
    await asyncio.gather(*list(bridge._emotion_tasks))

    assert result["ok"] is True
    assert esp32.calls == []
    assert esp32.emotions == ["sleepy"]
    assert esp32.leds == [(18, 28, 68)]


@pytest.mark.asyncio
async def test_dispatch_express_emotion_returns_ack_before_hardware_finishes():
    class SlowESP32(FakeESP32):
        def __init__(self) -> None:
            super().__init__()
            self.called = asyncio.Event()
            self.release = asyncio.Event()

        async def call_tool(self, name, arguments):
            self.called.set()
            await self.release.wait()
            return await super().call_tool(name, arguments)

    esp32 = SlowESP32()
    bridge = GeminiLiveBridge(esp32, api_key="k")

    result = await bridge._dispatch_tool("express_emotion", {"mood": "happy"})
    assert result["ok"] is True
    assert esp32.calls == []

    await asyncio.wait_for(esp32.called.wait(), timeout=1.0)
    esp32.release.set()
    await asyncio.gather(*list(bridge._emotion_tasks))
    assert len(esp32.calls) == 3


def test_dispatch_get_datetime_returns_readable():
    result = GeminiLiveBridge._dispatch_get_datetime()
    assert result["ok"] is True
    assert "T" in result["datetime"]
    assert result["weekday"] in {
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    }
    assert result["readable"].endswith(result["weekday"])


@pytest.mark.asyncio
async def test_dispatch_ask_claude_missing_question():
    bridge = _bridge()
    result = await bridge._dispatch_ask_claude({"question": "   "})
    assert result == {"ok": False, "error": "question is required"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "expected"),
    [(None, "claude-sonnet-5"), ("claude-opus-5", "claude-opus-5")],
)
async def test_dispatch_ask_claude_uses_configured_model(monkeypatch, configured, expected):
    import stackchan_mcp.gemini_live_bridge as bridge_mod

    if configured is not None:
        monkeypatch.setenv("STACKCHAN_CLAUDE_MODEL", configured)
    calls = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"answer", b""

    async def fake_exec(*argv, **kwargs):
        calls.append(argv)
        return FakeProc()

    monkeypatch.setattr(bridge_mod.asyncio, "create_subprocess_exec", fake_exec)
    bridge = _bridge()
    result = await bridge._dispatch_ask_claude({"question": "分析一下"})

    assert result == {"ok": True, "answer": "answer"}
    argv = calls[0]
    assert argv[argv.index("--model") + 1] == expected


@pytest.mark.asyncio
async def test_dispatch_ask_claude_not_found():
    bridge = _bridge()
    bridge._claude_bin = "/nonexistent/claude"
    result = await bridge._dispatch_ask_claude({"question": "分析一下"})
    assert result["ok"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_dispatch_tool_returns_error_when_device_offline():
    """Don't queue commands for a device that isn't there. Gemini will retry."""
    bridge = GeminiLiveBridge(FakeESP32(connected=False), api_key="k")
    result = await bridge._dispatch_tool("move_head", {"yaw": 0, "pitch": 10})
    assert result["ok"] is False
    assert "offline" in result["error"]


@pytest.mark.asyncio
async def test_dispatch_tool_propagates_esp32_error():
    esp32 = FakeESP32(error={"code": -32000, "message": "ESP32 not initialized"})
    bridge = GeminiLiveBridge(esp32, api_key="k")
    result = await bridge._dispatch_tool("set_avatar", {"face": "happy"})
    assert result["ok"] is False
    assert "not initialized" in result["error"]


@pytest.mark.asyncio
async def test_handle_message_dispatches_tool_call_via_session_send_tool_response():
    """Receiving a tool_call must call esp32 AND send a function_response back."""
    bridge = _bridge()

    sent_responses: list[list] = []

    class FakeSession:
        async def send_tool_response(self, *, function_responses):
            sent_responses.append(list(function_responses))

    bridge._session = FakeSession()

    class FakeFunctionCall:
        id = "call-1"
        name = "set_avatar"
        args = {"face": "happy"}

    class FakeToolCall:
        function_calls = [FakeFunctionCall()]

    class FakeResponse:
        tool_call = FakeToolCall()
        server_content = None

    await bridge._handle_message(FakeResponse())

    assert bridge._esp32.calls == [("self.display.set_avatar", {"face": "happy"})]
    assert len(sent_responses) == 1
    assert len(sent_responses[0]) == 1
    fr = sent_responses[0][0]
    assert fr.name == "set_avatar"
    assert fr.id == "call-1"
    assert fr.response["ok"] is True


@pytest.mark.asyncio
async def test_dispatch_tool_calls_parallelizes_different_exclusive_groups():
    bridge = _bridge()
    sent_responses: list[list] = []

    class FakeSession:
        async def send_tool_response(self, *, function_responses):
            sent_responses.append(list(function_responses))

    class FakeFunctionCall:
        def __init__(self, call_id: str, name: str) -> None:
            self.id = call_id
            self.name = name
            self.args = {}

    active = 0
    max_active = 0

    async def fake_dispatch(name, _args):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"ok": True, "name": name}

    bridge._session = FakeSession()
    bridge._dispatch_tool = fake_dispatch  # type: ignore[method-assign]

    await bridge._dispatch_tool_calls(
        [
            FakeFunctionCall("device-1", "move_head"),
            FakeFunctionCall("media-1", "media_control"),
        ]
    )

    assert max_active == 2
    assert [r.id for r in sent_responses[0]] == ["device-1", "media-1"]


@pytest.mark.asyncio
async def test_dispatch_tool_calls_serializes_same_exclusive_group():
    bridge = _bridge()

    class FakeSession:
        async def send_tool_response(self, *, function_responses):
            self.function_responses = list(function_responses)

    class FakeFunctionCall:
        def __init__(self, call_id: str, name: str) -> None:
            self.id = call_id
            self.name = name
            self.args = {}

    active_device = 0
    max_active_device = 0
    order: list[str] = []

    async def fake_dispatch(name, _args):
        nonlocal active_device, max_active_device
        if TOOL_METADATA[name].exclusive_group == "device":
            active_device += 1
            max_active_device = max(max_active_device, active_device)
        order.append(name)
        await asyncio.sleep(0.01)
        if TOOL_METADATA[name].exclusive_group == "device":
            active_device -= 1
        return {"ok": True, "name": name}

    bridge._session = FakeSession()
    bridge._dispatch_tool = fake_dispatch  # type: ignore[method-assign]

    await bridge._dispatch_tool_calls(
        [
            FakeFunctionCall("device-1", "move_head"),
            FakeFunctionCall("device-2", "set_avatar"),
        ]
    )

    assert max_active_device == 1
    assert order == ["move_head", "set_avatar"]


@pytest.mark.asyncio
async def test_handle_tool_call_cancellation_cancels_background_task():
    from stackchan_mcp.debug_status import DebugStatus

    st = DebugStatus()
    bridge = _bridge(debug_status=st)
    cancelled: list[int] = []

    class FakeMac:
        async def cancel_task(self, task_id: int):
            cancelled.append(task_id)
            return {"ok": True, "task_id": task_id, "state": "cancelled"}

    class Cancellation:
        ids = ["call-1"]

    class Response:
        tool_call_cancellation = Cancellation()
        tool_call = None
        server_content = None

    bridge._mac = FakeMac()  # type: ignore[assignment]
    bridge._tool_call_task_ids["call-1"] = 42

    await bridge._handle_message(Response())

    assert cancelled == [42]
    snap = st.snapshot()["gemini"]
    assert snap["tool_call_cancel_count"] == 1
    assert snap["last_tool_call_cancel_at"] is not None


@pytest.mark.asyncio
async def test_dispatch_tool_calls_skips_cancelled_call_before_start():
    bridge = _bridge()
    sent_responses: list[list] = []
    dispatched: list[str] = []

    class FakeSession:
        async def send_tool_response(self, *, function_responses):
            sent_responses.append(list(function_responses))

    class FakeFunctionCall:
        id = "call-1"
        name = "move_head"
        args = {"yaw": 0, "pitch": 0}

    async def fake_dispatch(name, _args):
        dispatched.append(name)
        return {"ok": True}

    bridge._session = FakeSession()
    bridge._dispatch_tool = fake_dispatch  # type: ignore[method-assign]
    bridge._cancelled_tool_call_ids.add("call-1")

    await bridge._dispatch_tool_calls([FakeFunctionCall()])

    assert dispatched == []
    assert sent_responses[0][0].response == {
        "ok": False,
        "cancelled": True,
        "error": "tool call cancelled before start",
    }


@pytest.mark.asyncio
async def test_inject_user_text_sends_realtime_text_only(fake_google_genai):
    sent = []
    bridge = GeminiLiveBridge(FakeESP32(), api_key="k")

    class FakeSession:
        async def send_realtime_input(self, **kwargs):
            sent.append(kwargs)

    bridge._session = FakeSession()

    active = await bridge.inject_user_text("放一首歌")

    assert active is True
    assert sent == [{"text": "放一首歌"}]
    assert list(sent[0]) == ["text"]


@pytest.mark.asyncio
async def test_announce_mac_task_sends_system_realtime_text_only(fake_google_genai):
    sent = []
    bridge = GeminiLiveBridge(FakeESP32(), api_key="k")

    class FakeSession:
        async def send_realtime_input(self, **kwargs):
            sent.append(kwargs)

    bridge._session = FakeSession()

    await bridge._announce_mac_task(
        {
            "id": 7,
            "task": "整理桌面截图",
            "status": "done",
            "result": "已整理 3 张截图",
        }
    )

    assert len(sent) == 1
    assert list(sent[0]) == ["text"]
    text = sent[0]["text"]
    assert "[System notice, not the user speaking]" in text
    assert "Background task #7" in text
    assert "has finished" in text
    assert "已整理 3 张截图" in text
    assert "in their language" in text


@pytest.mark.asyncio
async def test_handle_message_forwards_audio_to_callback():
    """server_content with model_turn audio Part must hit on_audio."""

    audio_chunks: list[bytes] = []

    async def on_audio(chunk: bytes) -> None:
        audio_chunks.append(chunk)

    bridge = GeminiLiveBridge(FakeESP32(), api_key="k", on_audio=on_audio)

    class FakeInline:
        data = b"PCM-SAMPLE"

    class FakePart:
        inline_data = FakeInline()

    class FakeTurn:
        parts = [FakePart()]

    class FakeContent:
        model_turn = FakeTurn()

    class FakeResponse:
        tool_call = None
        server_content = FakeContent()

    await bridge._handle_message(FakeResponse())
    assert audio_chunks == [b"PCM-SAMPLE"]


@pytest.mark.asyncio
async def test_start_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    bridge = GeminiLiveBridge(FakeESP32(), api_key=None)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        await bridge.start()


# --- USB-first dispatch (USB is a faster channel, not the only one) -----------


class FakeUsbTransport:
    def __init__(self, connected=True, raises=None) -> None:
        self.connected = connected
        self.calls: list[tuple[str, dict]] = []
        self._raises = raises

    async def call_tool(self, name, arguments, *, timeout_s=None):
        self.calls.append((name, arguments))
        if self._raises is not None:
            raise self._raises
        return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


@pytest.mark.asyncio
async def test_dispatch_prefers_usb_when_connected():
    usb = FakeUsbTransport(connected=True)
    bridge = GeminiLiveBridge(FakeESP32(), api_key="k", usb_transport=usb)
    result = await bridge._dispatch_tool("move_head", {"yaw": 0, "pitch": 30, "speed": 200})
    assert result["ok"] is True
    assert len(usb.calls) == 1
    name, args = usb.calls[0]
    assert name == "self.robot.set_head_angles"
    assert args == {"yaw": 0, "pitch": 30, "speed": 200}
    assert bridge._esp32.calls == []


@pytest.mark.asyncio
async def test_dispatch_falls_back_to_ws_when_usb_disconnected():
    usb = FakeUsbTransport(connected=False)
    bridge = GeminiLiveBridge(FakeESP32(), api_key="k", usb_transport=usb)
    result = await bridge._dispatch_tool("set_avatar", {"face": "happy"})
    assert result["ok"] is True
    assert usb.calls == []
    assert len(bridge._esp32.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_falls_back_to_ws_when_usb_raises():
    usb = FakeUsbTransport(connected=True, raises=RuntimeError("usb gone"))
    bridge = GeminiLiveBridge(FakeESP32(), api_key="k", usb_transport=usb)
    result = await bridge._dispatch_tool("set_avatar", {"face": "happy"})
    assert result["ok"] is True
    assert len(usb.calls) == 1
    assert len(bridge._esp32.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_returns_offline_when_both_channels_down():
    usb = FakeUsbTransport(connected=False)
    esp32 = FakeESP32(connected=False)
    bridge = GeminiLiveBridge(esp32, api_key="k", usb_transport=usb)
    result = await bridge._dispatch_tool("move_head", {"yaw": 0, "pitch": 0})
    assert result["ok"] is False
    assert "offline" in result["error"]


# --- TEXT response → on_text callback (buffered until turn_complete) ----------


@pytest.mark.asyncio
async def test_handle_text_chunks_buffered_until_turn_complete():
    """Gemini streams text; only turn_complete hands the whole sentence to on_text for TTS."""
    captured: list[str] = []

    async def on_text(text: str) -> None:
        captured.append(text)

    bridge = GeminiLiveBridge(FakeESP32(), api_key="k", on_text=on_text)

    class Part:
        def __init__(self, text):
            self.text = text
            self.inline_data = None

    class Turn:
        def __init__(self, parts):
            self.parts = parts

    class Content:
        def __init__(self, parts, complete):
            self.model_turn = Turn(parts)
            self.turn_complete = complete
            self.output_transcription = None

    class Resp:
        def __init__(self, parts, complete):
            self.tool_call = None
            self.server_content = Content(parts, complete)

    # Stream three fragments, last one with turn_complete=True.
    await bridge._handle_message(Resp([Part("好的，")], False))
    await bridge._handle_message(Resp([Part("我抬一下")], False))
    await bridge._handle_message(Resp([Part("头。")], True))

    assert captured == ["好的，我抬一下头。"]


@pytest.mark.asyncio
async def test_handle_text_skips_empty_turn():
    """An empty turn must not trigger on_text / TTS."""
    captured: list[str] = []

    async def on_text(text: str) -> None:
        captured.append(text)

    bridge = GeminiLiveBridge(FakeESP32(), api_key="k", on_text=on_text)

    class Content:
        model_turn = None
        turn_complete = True

    class Resp:
        tool_call = None
        server_content = Content()

    await bridge._handle_message(Resp())
    assert captured == []


@pytest.mark.asyncio
async def test_receive_loop_keeps_same_session_after_turn_complete():
    """google-genai receive() returns after one model turn; that is not a
    socket close, so the bridge must call receive() again on the same session
    instead of reconnecting after every reply."""
    completed_turns = 0

    async def on_turn_complete() -> None:
        nonlocal completed_turns
        completed_turns += 1
        if completed_turns == 2:
            bridge._stop_event.set()

    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="k",
        on_turn_complete=on_turn_complete,
    )

    class Content:
        model_turn = None
        turn_complete = True

    class Resp:
        tool_call = None
        server_content = Content()

    class MultiTurnSession:
        def __init__(self) -> None:
            self.receive_calls = 0

        async def receive(self):
            self.receive_calls += 1
            yield Resp()

    session = MultiTurnSession()
    bridge._session = session

    await bridge._receive_loop()

    assert session.receive_calls == 2
    assert completed_turns == 2


def test_build_live_config_text_mode_omits_speech_config():
    """3.1 Live does not output TEXT directly; the audio transcription provides the text for Edge TTS."""
    cfg = build_live_config(response_modality="TEXT")
    assert _modality_values(cfg.response_modalities) == ["AUDIO"]
    assert cfg.speech_config is not None
    assert cfg.output_audio_transcription is not None
    assert len(cfg.tools) == 2


@pytest.mark.asyncio
async def test_silence_timeout_triggers_end_conversation_after_turn_complete():
    called = asyncio.Event()

    async def on_end_conversation():
        called.set()

    esp32 = FakeESP32()
    esp32.on_end_conversation = on_end_conversation
    bridge = GeminiLiveBridge(
        esp32,
        api_key="k",
        conversation_idle_timeout_s=0.01,
    )

    class Content:
        model_turn = None
        output_transcription = None
        turn_complete = True

    class Resp:
        tool_call = None
        server_content = Content()

    await bridge._handle_message(Resp())

    await asyncio.wait_for(called.wait(), timeout=1.0)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gemini-3.8-live", ["AUDIO"]),
        ("gemini-3.1-flash-live-preview", ["AUDIO"]),
        ("gemini-2.5-flash-native-audio-latest", ["TEXT"]),
    ],
)
def test_build_live_config_text_mode_remaps_for_gemini_3_models(model, expected):
    """3.8 Live rejects TEXT with 1007; 3.x uses audio plus transcription, 2.x keeps TEXT."""
    cfg = build_live_config(response_modality="TEXT", model=model)
    assert _modality_values(cfg.response_modalities) == expected
    assert cfg.output_audio_transcription is not None


@pytest.mark.asyncio
async def test_model_output_after_early_turn_complete_keeps_conversation_open():
    """3.8 Live sends turn_complete after a tool call, then speaks; the silence timer must not end it."""
    called = False

    async def on_end_conversation():
        nonlocal called
        called = True

    esp32 = FakeESP32()
    esp32.on_end_conversation = on_end_conversation
    bridge = GeminiLiveBridge(
        esp32,
        api_key="k",
        conversation_idle_timeout_s=0.03,
    )

    class EarlyDone:
        model_turn = None
        output_transcription = None
        turn_complete = True

    class Part:
        inline_data = None
        text = None

    class Speaking:
        class model_turn:  # noqa: N801
            parts = [Part()]

        output_transcription = None
        turn_complete = False

    def resp(content):
        class Resp:
            tool_call = None
            tool_call_cancellation = None
            server_content = content

        return Resp()

    await bridge._handle_message(resp(EarlyDone()))
    await asyncio.sleep(0.01)
    await bridge._handle_message(resp(Speaking()))
    await asyncio.sleep(0.05)

    assert called is False


@pytest.mark.asyncio
async def test_send_audio_cancels_silence_timeout():
    called = False

    async def on_end_conversation():
        nonlocal called
        called = True

    esp32 = FakeESP32()
    esp32.on_end_conversation = on_end_conversation
    bridge = GeminiLiveBridge(
        esp32,
        api_key="k",
        conversation_idle_timeout_s=0.02,
    )

    bridge._schedule_silence_timeout()
    await bridge.send_audio(b"\x00\x00")
    await asyncio.sleep(0.04)

    assert called is False


@pytest.mark.asyncio
async def test_send_audio_buffers_when_session_missing_and_replays_after_flush():
    session = RecordingSession()
    bridge = _bridge()
    bridge._session = None

    await bridge.send_audio(b"held-1")
    await bridge.send_audio(b"held-2")
    assert session.calls == []

    bridge._session = session
    await bridge._flush_reconnect_audio()

    audio = [
        call["audio"].data
        for call in session.calls
        if call.get("audio") is not None
    ]
    assert audio == [b"held-1", b"held-2"]


@pytest.mark.asyncio
async def test_new_audio_during_flush_is_sent_after_cached_frames(monkeypatch):
    monkeypatch.setenv("STACKCHAN_MANUAL_VAD", "1")
    flush_busy = asyncio.Event()
    allow_cached_send = asyncio.Event()

    class BlockingSession:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def send_realtime_input(self, **kwargs):
            audio = kwargs.get("audio")
            payload = getattr(audio, "data", audio) if audio is not None else None
            if payload == b"cached":
                flush_busy.set()
                await allow_cached_send.wait()
            self.calls.append(kwargs)

    session = BlockingSession()
    bridge = _bridge()
    bridge._session = None
    await bridge.send_audio(b"cached")

    async def resume() -> None:
        bridge._session = session
        await bridge._flush_reconnect_audio()

    async def late_frame() -> None:
        await flush_busy.wait()
        send_task = asyncio.create_task(bridge.send_audio(b"live"))
        await asyncio.sleep(0)
        allow_cached_send.set()
        await send_task

    await asyncio.gather(resume(), late_frame())

    audio = [
        getattr(call["audio"], "data", call["audio"])
        for call in session.calls
        if call.get("audio") is not None
    ]
    assert audio == [b"cached", b"live"]
    starts = [call for call in session.calls if call.get("activity_start") is not None]
    assert len(starts) == 1


@pytest.mark.asyncio
async def test_reconnect_audio_timeout_drops_buffer_and_signals_dead():
    dead: list[bool] = []

    async def on_dead():
        dead.append(True)

    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="k",
        on_session_dead=on_dead,
        reconnect_audio_ttl_s=0.05,
    )
    bridge._session = None
    await bridge.send_audio(b"held")
    await asyncio.sleep(0.12)

    session = RecordingSession()
    bridge._session = session
    await bridge._flush_reconnect_audio()
    assert session.calls == []
    assert dead == [True]


@pytest.mark.asyncio
async def test_handle_output_transcription_flushes_when_finished():
    """With AUDIO + output_transcription, a finished transcription triggers Edge TTS."""
    captured: list[str] = []

    async def on_text(text: str) -> None:
        captured.append(text)

    bridge = GeminiLiveBridge(FakeESP32(), api_key="k", on_text=on_text)

    class Transcript:
        def __init__(self, text: str, finished: bool):
            self.text = text
            self.finished = finished

    class Content:
        model_turn = None
        turn_complete = False

        def __init__(self, text: str, finished: bool):
            self.output_transcription = Transcript(text, finished)

    class Resp:
        tool_call = None

        def __init__(self, text: str, finished: bool):
            self.server_content = Content(text, finished)

    await bridge._handle_message(Resp("好的，", False))
    await bridge._handle_message(Resp("我抬头", True))

    assert captured == ["好的，我抬头"]


# --- session lifecycle / reconnect -------------------------------------------


class _RecordingFakeSession:
    """Async context manager + async iterator pretending to be a Live session.

    Receives a finite list of responses, yields them through ``receive()``,
    then exits cleanly so the bridge sees a natural end-of-session.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_exc):
        self.exited = True
        return False

    async def receive(self):
        for r in self._responses:
            yield r

    async def send_tool_response(self, **_kwargs):
        pass


class _ScriptedLive:
    """Returns a different fake session for each connect() call."""

    def __init__(self, sessions):
        self._iter = iter(sessions)
        self.connect_count = 0
        self.configs = []

    def connect(self, *, model, config):
        self.connect_count += 1
        self.configs.append(config)
        return next(self._iter)


class _FakeClient:
    def __init__(self, sessions):
        self.live = _ScriptedLive(sessions)
        self.aio = type("A", (), {"live": self.live})()


@pytest.mark.asyncio
async def test_bridge_reconnects_after_session_naturally_ends():
    """Gemini Live caps at 15 min — the bridge must transparently reconnect."""
    sessions = [
        _RecordingFakeSession([]),  # first session ends immediately
        _RecordingFakeSession([]),  # second session lifts off
    ]

    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    bridge._client = _FakeClient(sessions)
    # Run _run manually so we can stop after two cycles deterministically.
    task = asyncio.create_task(bridge._run())
    # Give the loop time to spin up two sessions.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if bridge._session_count >= 2:
            break
    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert bridge._session_count == 2
    assert sessions[0].entered and sessions[0].exited
    assert sessions[1].entered and sessions[1].exited


@pytest.mark.asyncio
async def test_bridge_does_not_reconnect_when_reconnect_off():
    """Old single-session callers must keep their fail-fast behaviour."""
    sessions = [_RecordingFakeSession([])]
    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=False,
    )
    bridge._client = _FakeClient(sessions)
    task = asyncio.create_task(bridge._run())
    await asyncio.wait_for(task, timeout=2.0)
    assert bridge._session_count == 1


@pytest.mark.asyncio
async def test_snap_to_face_runs_only_on_first_session():
    """Reconnects must not re-trigger wake-up cues like snap_to_face."""
    sessions = [
        _RecordingFakeSession([]),
        _RecordingFakeSession([]),
    ]
    snap_calls = []

    async def snap():
        snap_calls.append(True)

    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
        snap_to_face=snap,
    )
    bridge._client = _FakeClient(sessions)
    task = asyncio.create_task(bridge._run())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if bridge._session_count >= 2:
            break
    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert snap_calls == [True], "snap_to_face should fire exactly once"


@pytest.mark.asyncio
async def test_bridge_recovers_from_transient_connect_failure():
    """A network blip on connect() should retry, not abort."""

    sessions = [Exception("transient connect failure"), _RecordingFakeSession([])]

    class _FlakyLive:
        def __init__(self, plan):
            self._plan = list(plan)
            self.connect_count = 0

        def connect(self, *, model, config):
            self.connect_count += 1
            nxt = self._plan.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

    class _FlakyClient:
        def __init__(self, plan):
            self.aio = type("A", (), {"live": _FlakyLive(plan)})()

    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    bridge._client = _FlakyClient(sessions)
    task = asyncio.create_task(bridge._run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if bridge._session_count >= 1:
            break
    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert bridge._session_count == 1


@pytest.mark.asyncio
async def test_receive_loop_stores_resumption_handle_and_stops_on_go_away():
    bridge = GeminiLiveBridge(FakeESP32(), api_key="k")

    class Update:
        resumable = True
        new_handle = "resume-1"

    class GoAway:
        time_left = "60s"

    class Resp:
        session_resumption_update = Update()
        go_away = GoAway()
        tool_call = None
        server_content = None

    class Session:
        def __init__(self) -> None:
            self.sent_audio = False
            self.activity_end_sent = False

        async def receive(self):
            yield Resp()

        async def send_realtime_input(self, **kwargs):
            if kwargs.get("activity_end") is not None:
                self.activity_end_sent = True
                return
            self.sent_audio = True

    session = Session()
    bridge._session = session

    await bridge._receive_loop()

    assert bridge._resumption_handle == "resume-1"
    assert bridge._go_away_received is True
    await bridge.send_audio(b"\x00\x00")
    await bridge.send_audio_stream_end()
    assert session.sent_audio is False
    assert session.activity_end_sent is False


@pytest.mark.asyncio
async def test_reconnect_uses_latest_resumption_handle():
    class Update:
        resumable = True
        new_handle = "resume-1"

    class GoAway:
        time_left = "60s"

    class Resp:
        session_resumption_update = Update()
        go_away = GoAway()
        tool_call = None
        server_content = None

    sessions = [
        _RecordingFakeSession([Resp()]),
        _RecordingFakeSession([]),
    ]
    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    bridge._client = _FakeClient(sessions)

    task = asyncio.create_task(bridge._run())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if bridge._session_count >= 2:
            break
    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    configs = bridge._client.live.configs
    assert configs[0].session_resumption.handle is None
    assert configs[1].session_resumption.handle == "resume-1"


@pytest.mark.asyncio
async def test_successful_connection_resets_backoff_after_later_error(monkeypatch):
    import stackchan_mcp.gemini_live_bridge as bridge_mod

    class ExplodingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def receive(self):
            raise RuntimeError("1008")
            yield

    class FlakyLive:
        def __init__(self):
            self.plan = [Exception("connect failed"), ExplodingSession()]

        def connect(self, *, model, config):
            nxt = self.plan.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

    class FlakyClient:
        def __init__(self):
            self.aio = type("A", (), {"live": FlakyLive()})()

    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=1.0,
        reconnect_max_backoff_s=30.0,
    )
    bridge._client = FlakyClient()
    timeouts = []

    async def fake_wait_for(aw, *, timeout):
        if hasattr(aw, "close"):
            aw.close()
        timeouts.append(timeout)
        if len(timeouts) >= 2:
            bridge._stop_event.set()
        raise asyncio.TimeoutError

    monkeypatch.setattr(bridge_mod.asyncio, "wait_for", fake_wait_for)

    await bridge._run()

    assert timeouts == [1.0, 1.0]


# --- Session keepalive and the active-drop warning -----------------------------


class RecordingSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_realtime_input(self, **kwargs):
        self.calls.append(kwargs)


def test_close_code_extracts_from_rcvd_then_falls_back_to_text():
    from stackchan_mcp.gemini_live_bridge import _close_code

    class Rcvd:
        code = 1008

    class ClosedError(Exception):
        rcvd = Rcvd()

    assert _close_code(ClosedError("x")) == 1008
    assert _close_code(Exception("received 1008 (policy violation)")) == 1008
    assert _close_code(Exception("plain failure")) is None
    assert _close_code(None) is None


def test_note_session_end_during_listening_logs_error_and_counts(caplog):
    from stackchan_mcp.debug_status import DebugStatus

    st = DebugStatus()
    bridge = _bridge(debug_status=st)
    st.on_gemini_connected(1)
    st.on_wake_woke()  # LISTENING → an active conversation
    bridge._session = object()

    with caplog.at_level("ERROR"):
        bridge._note_session_end(Exception("socket closed 1008"))

    snap = st.snapshot()["gemini"]
    assert snap["active_drops"] == 1
    assert snap["reconnect_1008_count"] == 1
    assert snap["last_error"]["message"] == "socket closed 1008"
    assert "dropped during an active conversation" in caplog.text


def test_note_session_end_while_dormant_stays_quiet(caplog):
    from stackchan_mcp.debug_status import DebugStatus

    st = DebugStatus()
    bridge = _bridge(debug_status=st)
    st.on_gemini_connected(1)
    bridge._session = object()
    # Setting up the bridge may warn about unrelated things (for example no
    # `claude` CLI on PATH on CI runners); only the session-end path counts.
    caplog.clear()

    with caplog.at_level("ERROR"):
        bridge._note_session_end(None)

    snap = st.snapshot()["gemini"]
    assert snap["active_drops"] == 0
    assert snap["reconnect_1008_count"] == 0
    assert caplog.records == []


@pytest.mark.asyncio
async def test_send_keepalive_audio_does_not_cancel_silence_timer():
    bridge = _bridge()
    session = RecordingSession()
    bridge._session = session
    timer = asyncio.get_running_loop().create_task(asyncio.sleep(30))
    bridge._silence_timeout_task = timer
    try:
        await bridge.send_keepalive_audio(b"\x00" * 64)
        assert len(session.calls) == 1
        assert timer.cancelled() is False

        # Control: real user audio cancels the silence timer.
        await bridge.send_audio(b"\x00" * 64)
        await asyncio.sleep(0)
        assert timer.cancelled() is True
    finally:
        timer.cancel()


@pytest.mark.asyncio
async def test_send_keepalive_audio_noop_without_session():
    bridge = _bridge()
    bridge._session = None
    await bridge.send_keepalive_audio(b"\x00" * 64)  # does not raise


@pytest.mark.asyncio
async def test_receive_loop_records_usage_metadata():
    from stackchan_mcp.debug_status import DebugStatus

    class OneShotSession:
        def __init__(self, response) -> None:
            self.response = response
            self.calls = 0

        def receive(self):
            async def gen():
                if self.calls == 0:
                    self.calls += 1
                    yield self.response

            return gen()

    st = DebugStatus(clock=lambda: 321.0)
    bridge = _bridge(debug_status=st)
    usage = SimpleNamespace(
        total_token_count=123,
        prompt_token_count=100,
        response_token_count=20,
        tool_use_prompt_token_count=3,
        response_tokens_details=[
            SimpleNamespace(
                modality=SimpleNamespace(value="AUDIO"),
                token_count=20,
            )
        ],
    )
    bridge._session = OneShotSession(SimpleNamespace(usage_metadata=usage))

    await bridge._receive_loop()

    token_usage = st.snapshot()["gemini"]["token_usage"]
    assert token_usage["total_token_count"] == 123
    assert token_usage["prompt_token_count"] == 100
    assert token_usage["response_token_count"] == 20
    assert token_usage["tool_use_prompt_token_count"] == 3
    assert token_usage["response_tokens_details"] == [
        {"modality": "AUDIO", "token_count": 20}
    ]
    assert token_usage["updated_at"] == 321.0


@pytest.mark.asyncio
async def test_dispatch_set_all_leds_deferred_during_tts():
    esp32 = FakeVoiceOnlyESP32()
    st = __import__("stackchan_mcp.debug_status", fromlist=["DebugStatus"]).DebugStatus()
    st.on_tts_state(True)
    bridge = GeminiLiveBridge(
        esp32,
        api_key="k",
        wake_gate_state_getter=lambda: "DORMANT",
        debug_status=st,
    )

    result = await bridge._dispatch_tool("set_all_leds", {"r": 0, "g": 0, "b": 0})

    assert result == {"ok": True, "deferred": "listening_status_light_active"}
    assert esp32.leds == []


@pytest.mark.asyncio
async def test_receive_stall_watchdog_closes_session_and_records_metric(monkeypatch):
    from stackchan_mcp.debug_status import DebugStatus

    clock = {"now": 1000.0}

    class HangingSession:
        def __init__(self) -> None:
            self.closed = False

        async def receive(self):
            while not self.closed:
                await asyncio.sleep(0.02)
                if False:  # pragma: no cover
                    yield None

        async def close(self) -> None:
            self.closed = True

    st = DebugStatus()
    bridge = _bridge(debug_status=st)
    bridge._receive_stall_s = 5.0
    bridge._mono_clock = lambda: clock["now"]
    session = HangingSession()
    bridge._session = session

    task = asyncio.create_task(bridge._receive_loop())
    try:
        await asyncio.sleep(0.05)
        clock["now"] = 1006.0
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        pytest.fail("receive loop did not exit after stall threshold")

    assert session.closed is True
    assert st.snapshot()["gemini"]["reconnect_receive_stall_count"] == 1


@pytest.mark.asyncio
async def test_resumption_handle_updates_debug_status():
    from stackchan_mcp.debug_status import DebugStatus

    st = DebugStatus(clock=lambda: 42.0)
    bridge = _bridge(debug_status=st)

    class Update:
        resumable = True
        new_handle = "resume-abc"

    bridge._handle_session_lifecycle_message(
        SimpleNamespace(session_resumption_update=Update(), go_away=None)
    )

    snap = st.snapshot()["gemini"]
    assert snap["has_resumption_handle"] is True
    assert snap["resumption_handle_updated_at"] == 42.0


@pytest.mark.asyncio
async def test_send_activity_start_and_end_use_manual_vad_signals(monkeypatch):
    monkeypatch.setenv("STACKCHAN_MANUAL_VAD", "1")
    sent: list[str] = []

    class Session:
        async def send_realtime_input(self, **kwargs):
            if kwargs.get("activity_start") is not None:
                sent.append("start")
            if kwargs.get("activity_end") is not None:
                sent.append("end")

    bridge = _bridge()
    bridge._session = Session()

    await bridge.send_activity_start()
    await bridge.send_activity_end()

    assert sent == ["start", "end"]
    assert bridge.activity_open is False


@pytest.mark.asyncio
async def test_send_audio_auto_opens_activity_after_end(monkeypatch):
    monkeypatch.setenv("STACKCHAN_MANUAL_VAD", "1")
    session = RecordingSession()
    bridge = _bridge()
    bridge._session = session

    await bridge.send_activity_start()
    await bridge.send_activity_end()
    await bridge.send_audio(b"\x01\x02")

    kinds: list[str] = []
    for call in session.calls:
        if call.get("activity_start") is not None:
            kinds.append("start")
        elif call.get("activity_end") is not None:
            kinds.append("end")
        elif call.get("audio") is not None:
            kinds.append("audio")
    assert kinds == ["start", "end", "start", "audio"]


@pytest.mark.asyncio
async def test_send_audio_auto_opens_activity_on_first_frame(monkeypatch):
    monkeypatch.setenv("STACKCHAN_MANUAL_VAD", "1")
    session = RecordingSession()
    bridge = _bridge()
    bridge._session = session

    await bridge.send_audio(b"\x01\x02")

    assert len(session.calls) == 2
    assert session.calls[0].get("activity_start") is not None
    assert session.calls[1].get("audio") is not None
    assert bridge.activity_open is True


@pytest.mark.asyncio
async def test_reconnect_resets_activity_open_flag(monkeypatch):
    monkeypatch.setenv("STACKCHAN_MANUAL_VAD", "1")
    sessions = [
        _RecordingFakeSession([]),
        _RecordingFakeSession([]),
    ]
    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    bridge._client = _FakeClient(sessions)
    bridge._activity_open = True

    task = asyncio.create_task(bridge._run())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if bridge._session_count >= 1:
            break

    assert bridge.activity_open is False

    session = RecordingSession()
    bridge._session = session
    await bridge.send_audio(b"\xaa\xbb")

    assert session.calls[0].get("activity_start") is not None
    assert session.calls[1].get("audio") is not None

    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)


class _ExpiredHandleConnect:
    """__aenter__ raises the server's 1008 handle rejection, like a connect with a dead handle."""

    def __init__(self, exc_text: str) -> None:
        self._exc_text = exc_text
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1
        raise RuntimeError(self._exc_text)

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_bridge_drops_resumption_handle_when_server_says_expired():
    """After the server rejects the handle with 1008 "session expired", the handle must be dropped.

    Regression: after the handle expired, the bridge kept reconnecting with the same dead handle
    every 30 s, was rejected for two hours, and voice stayed dead until the process restarted.
    """
    sessions = [
        _ExpiredHandleConnect("1008 None. BidiGenerateContent session expired"),
        _RecordingFakeSession([]),  # the new session after dropping the handle starts normally
    ]
    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    bridge._client = _FakeClient(sessions)
    bridge._resumption_handle = "stale-handle"

    task = asyncio.create_task(bridge._run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if bridge._session_count >= 1:
            break
    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert bridge._resumption_handle is None
    assert bridge._session_count == 1
    # the second connect must carry no handle (a fresh session)
    assert bridge._client.live.configs[1].session_resumption.handle is None


@pytest.mark.asyncio
async def test_bridge_keeps_resumption_handle_on_transient_network_error():
    """An ordinary network error must not drop the handle: dropping a valid handle throws away the conversation context."""
    sessions = [
        _ExpiredHandleConnect("[Errno 61] Could not connect to proxy 127.0.0.1:1080"),
        _RecordingFakeSession([]),
    ]
    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    bridge._client = _FakeClient(sessions)
    bridge._resumption_handle = "still-valid-handle"

    task = asyncio.create_task(bridge._run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if bridge._session_count >= 1:
            break
    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert bridge._resumption_handle == "still-valid-handle"
    assert bridge._client.live.configs[1].session_resumption.handle == "still-valid-handle"


@pytest.mark.asyncio
async def test_bridge_drops_resumption_handle_when_server_says_not_found():
    """A 1008 'Requested entity was not found' rejection must drop the handle."""
    sessions = [
        _ExpiredHandleConnect("1008 None. Requested entity was not found."),
        _RecordingFakeSession([]),
    ]
    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    bridge._client = _FakeClient(sessions)
    bridge._resumption_handle = "stale-handle"

    task = asyncio.create_task(bridge._run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if bridge._session_count >= 1:
            break
    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert bridge._resumption_handle is None
    assert bridge._session_count == 1
    assert bridge._client.live.configs[1].session_resumption.handle is None


@pytest.mark.asyncio
async def test_bridge_drops_resumption_handle_after_consecutive_failures():
    """The same handle is dropped after three failures, whatever the wording."""
    stale = "stale-handle"
    sessions = [
        _ExpiredHandleConnect("1011 None. Internal error encountered."),
        _ExpiredHandleConnect("1011 None. Internal error encountered."),
        _ExpiredHandleConnect("1011 None. Internal error encountered."),
        _RecordingFakeSession([]),
    ]
    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    bridge._client = _FakeClient(sessions)
    bridge._resumption_handle = stale

    task = asyncio.create_task(bridge._run())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if bridge._session_count >= 1:
            break
    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    configs = bridge._client.live.configs
    assert configs[0].session_resumption.handle == stale
    assert configs[1].session_resumption.handle == stale
    assert configs[2].session_resumption.handle == stale
    assert configs[3].session_resumption.handle is None
    assert bridge._session_count == 1


@pytest.mark.asyncio
async def test_bridge_resets_resumption_failure_count_after_successful_connect():
    """A successful connect resets the counter, so two later failures keep the handle."""
    handle = "still-valid-handle"
    sessions = [
        _ExpiredHandleConnect("1011 None. Internal error encountered."),
        _ExpiredHandleConnect("1011 None. Internal error encountered."),
        _RecordingFakeSession([]),
        _ExpiredHandleConnect("1011 None. Internal error encountered."),
        _ExpiredHandleConnect("1011 None. Internal error encountered."),
    ]
    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    bridge._client = _FakeClient(sessions)
    bridge._resumption_handle = handle

    task = asyncio.create_task(bridge._run())
    for _ in range(300):
        await asyncio.sleep(0.01)
        if bridge._client.live.connect_count >= 5:
            break
    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert bridge._resumption_handle == handle
    assert bridge._session_count == 1
    configs = bridge._client.live.configs
    assert len(configs) == 5
    assert all(cfg.session_resumption.handle == handle for cfg in configs)


class _UpdateThenFailSession:
    """Issue a new handle, then fail receive() to simulate a mid-session drop."""

    def __init__(self, new_handle: str, exc_text: str) -> None:
        self._new_handle = new_handle
        self._exc_text = exc_text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def receive(self):
        class Update:
            resumable = True
            new_handle = self._new_handle

        yield SimpleNamespace(
            session_resumption_update=Update(),
            go_away=None,
            tool_call=None,
            server_content=None,
        )
        raise RuntimeError(self._exc_text)


@pytest.mark.asyncio
async def test_bridge_does_not_count_failure_against_handle_issued_mid_session():
    """A drop after a mid-session handle refresh must not count against the new handle."""
    sessions = [
        _UpdateThenFailSession("fresh", "1011 None. Internal error encountered."),
        _ExpiredHandleConnect("[Errno 61] Could not connect to proxy 127.0.0.1:1080"),
        _ExpiredHandleConnect("[Errno 61] Could not connect to proxy 127.0.0.1:1080"),
        _RecordingFakeSession([]),
    ]
    bridge = GeminiLiveBridge(
        FakeESP32(),
        api_key="test-key",
        reconnect_on_close=True,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    bridge._client = _FakeClient(sessions)
    bridge._resumption_handle = "old"

    task = asyncio.create_task(bridge._run())
    for _ in range(300):
        await asyncio.sleep(0.01)
        if bridge._session_count >= 2:
            break
    bridge._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    configs = bridge._client.live.configs
    assert configs[1].session_resumption.handle == "fresh"
    assert configs[2].session_resumption.handle == "fresh"
    assert configs[3].session_resumption.handle == "fresh"
    assert bridge._resumption_handle == "fresh"


# --- Grok Bot hand-off (ask_grokbot) ------------------------------------------


@pytest.mark.defaults
def test_ask_grokbot_is_off_by_default():
    """No STACKCHAN_TOOL_BOT: the tool is not declared and its rules are absent."""
    names = [decl.name for decl in build_function_declarations()]
    assert "ask_grokbot" not in names
    assert "ask_grokbot" not in default_system_instruction()


def test_ask_grokbot_declared_with_configured_bot_name(monkeypatch):
    monkeypatch.setenv("STACKCHAN_TOOL_BOT", "Butler")
    decls = {decl.name: decl for decl in build_function_declarations()}
    assert "ask_grokbot" in decls
    assert "Butler" in decls["ask_grokbot"].description
    assert "ask_grokbot" in decls["web_search"].description
    instruction = default_system_instruction()
    assert "ask_grokbot(task)" in instruction
    assert "has been sent to Butler" in instruction
    assert "hand it to ask_grokbot" in instruction  # web_search defers to the agent
    assert TOOL_METADATA["ask_grokbot"].side_effect is False


def test_ask_grokbot_refuses_when_not_configured():
    async def run() -> dict:
        bridge = GeminiLiveBridge(SimpleNamespace(), api_key="x", reconnect_on_close=False)
        return await bridge._dispatch_tool("ask_grokbot", {"task": "anything"})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "STACKCHAN_TOOL_BOT" in result["error"]


def _run_ask_grokbot(monkeypatch, fake_iter, *, wait_for: int):
    import stackchan_mcp.gbot_brain as gbot_brain

    monkeypatch.setattr(gbot_brain, "iter_gbot_replies", fake_iter)

    class Session:
        def __init__(self) -> None:
            self.texts: list[str] = []

        async def send_realtime_input(self, **kwargs) -> None:
            self.texts.append(kwargs.get("text") or "")

    async def run() -> tuple[dict, Session]:
        bridge = GeminiLiveBridge(SimpleNamespace(), api_key="x", reconnect_on_close=False)

        async def _no_face(_face: str) -> None:
            return None

        bridge._set_face = _no_face
        session = Session()
        bridge._session = session
        result = await bridge._dispatch_tool("ask_grokbot", {"task": "打开苹果官网"})
        for _ in range(100):
            if len(session.texts) >= wait_for:
                break
            await asyncio.sleep(0.02)
        return result, session

    return asyncio.run(run())


def test_ask_grokbot_relays_each_reply_into_live_session(monkeypatch):
    """The agent cannot reach the robot's speaker: every reply piece is pushed
    into the Live session so Gemini says it."""
    monkeypatch.setenv("STACKCHAN_TOOL_BOT", "助手")
    monkeypatch.setenv("STACKCHAN_TOOL_BOT_ID", "bot-id-123")
    calls: list[dict] = []

    def fake_iter(text, *, timeout_s=None, bot=None, bot_id=None):
        calls.append({"text": text, "bot": bot, "bot_id": bot_id})
        yield {"reply": "好，我这就去办。", "event": "first"}
        yield {"reply": "办好了，网页已经打开。", "event": "more"}

    result, session = _run_ask_grokbot(monkeypatch, fake_iter, wait_for=2)
    assert result["ok"] is True and result["say"] == "Sent to 助手."
    assert calls and calls[0]["bot"] == "助手" and calls[0]["bot_id"] == "bot-id-123"
    assert calls[0]["text"].endswith("打开苹果官网")
    assert calls[0]["text"].startswith("[Voice task forwarded")  # default read-aloud prefix
    assert len(session.texts) == 2
    assert "助手 replied" in session.texts[0] and "好，我这就去办。" in session.texts[0]
    assert "in the user's language" in session.texts[0]
    assert "办好了，网页已经打开。" in session.texts[1]


def test_ask_grokbot_empty_prefix_sends_task_as_is(monkeypatch):
    monkeypatch.setenv("STACKCHAN_TOOL_BOT", "助手")
    monkeypatch.setenv("STACKCHAN_TOOL_BOT_PREFIX", "")
    calls: list[str] = []

    def fake_iter(text, *, timeout_s=None, bot=None, bot_id=None):
        calls.append(text)
        yield {"reply": "好。", "event": "first"}

    _run_ask_grokbot(monkeypatch, fake_iter, wait_for=1)
    assert calls == ["打开苹果官网"]


def test_ask_grokbot_failure_pushes_one_notice(monkeypatch):
    monkeypatch.setenv("STACKCHAN_TOOL_BOT", "助手")

    def fake_iter(text, *, timeout_s=None, bot=None, bot_id=None):
        raise RuntimeError("forwarding service down")
        yield  # pragma: no cover

    result, session = _run_ask_grokbot(monkeypatch, fake_iter, wait_for=1)
    assert result["ok"] is True
    assert len(session.texts) == 1
    assert "Sending the task to 助手 failed" in session.texts[0]


# ---- language: English prompt, replies in the user's language ---------------

_CJK = __import__("re").compile(r"[㐀-鿿]")


@pytest.mark.parametrize("device_tools", [False, True])
@pytest.mark.parametrize("mac_control", [False, True])
@pytest.mark.parametrize("grokbot", ["", "assistant"])
@pytest.mark.parametrize("ask_claude", [False, True])
def test_system_instruction_is_english_and_follows_the_users_language(
    device_tools, mac_control, grokbot, ask_claude
):
    """A Chinese speaker must still get Chinese answers although every rule is
    written in English: the language rule is always present, comes right after
    the persona (before any other rule), and no rule names a fixed reply
    language."""
    import re

    import stackchan_mcp.gemini_live_bridge as bridge_mod

    instruction = default_system_instruction(
        device_tools=device_tools, mac_control=mac_control, grokbot=grokbot, ask_claude=ask_claude
    )
    assert not _CJK.search(instruction), _CJK.findall(instruction)[:10]
    persona_end = len(bridge_mod._load_personality())
    language_at = instruction.index("Always reply in the language the user is speaking")
    assert language_at > persona_end
    assert "Chinese when the user speaks Chinese" in instruction
    for heading in re.findall(r"^## .+$", instruction, flags=re.M):
        if heading != "## Language":
            assert instruction.index(heading) > language_at, heading
    assert not re.search(r"(reply|answer|speak|respond)\s+(only\s+)?in\s+(English|Chinese)", instruction, re.I)


def test_tool_declarations_are_english(monkeypatch):
    monkeypatch.setenv("STACKCHAN_TOOL_BOT", "assistant")

    def texts(schema):
        if schema is None:
            return []
        out = [schema.description or ""]
        for prop in (schema.properties or {}).values():
            out += texts(prop)
        return out

    for decl in build_function_declarations():
        for text in [decl.description or ""] + texts(decl.parameters) + texts(getattr(decl, "response", None)):
            assert not _CJK.search(text), (decl.name, text)


def test_spoken_notices_ask_for_the_users_language(monkeypatch):
    monkeypatch.setenv("STACKCHAN_TOOL_BOT", "assistant")

    def fake_iter(text, *, timeout_s=None, bot=None, bot_id=None):
        raise RuntimeError("down")
        yield  # pragma: no cover

    result, session = _run_ask_grokbot(monkeypatch, fake_iter, wait_for=1)
    assert "in their language" in result["instruction"]
    assert "in their language" in session.texts[0]
