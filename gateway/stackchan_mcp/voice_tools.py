# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Provider-independent voice tools. No model client, API key or session is owned here."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from .debug_status import get_debug_status
from .device_emotion import face_to_device_emotion
from . import gbot_brain as _gbot_module
from . import mac_control as _mac_module
from .mac_control import MAC_TOOL_NAMES, MacController
from .wake_gate import WakeGateState


def _bot_setting(helper, variable, default=""):
    getter = getattr(_gbot_module, helper, None)
    return getter() if getter is not None else os.getenv(variable, default).strip()


def tool_bot():
    return _bot_setting("tool_bot", "STACKCHAN_TOOL_BOT")


def grokbot_enabled():
    return bool(tool_bot())


def find_claude_bin():
    finder = getattr(_mac_module, "find_claude_bin", None)
    return finder() if finder is not None else None


def resolve_claude_bin():
    resolver = getattr(_mac_module, "resolve_claude_bin", None)
    return resolver() if resolver is not None else "claude"


def claude_fast_args():
    factory = getattr(_mac_module, "claude_fast_args", None)
    return factory() if factory is not None else []


logger = logging.getLogger(__name__)


def face_tracker_autostart_enabled():
    # Older installations inject their own declarations and have no supervisor module.
    value = os.getenv("STACKCHAN_FACE_TRACKER_AUTOSTART", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


class _PlainTypes:
    """The same declarations as JSON Schema, without importing a model SDK."""

    @staticmethod
    def Schema(**values):
        if "type" in values:
            values["type"] = str(values["type"]).lower()
        if values.get("format") == "enum":
            values.pop("format")
        return values

    @staticmethod
    def FunctionDeclaration(**values):
        return values


def _valid_arguments(value, schema):
    """Validate the JSON types/required fields the shared catalog actually uses."""
    kind = schema.get("type")
    checks = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "boolean": lambda: isinstance(value, bool),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        ),
    }
    if kind in checks and not checks[kind]():
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            return False
        return all(
            _valid_arguments(item, properties[key])
            for key, item in value.items()
            if key in properties
        )
    if isinstance(value, list) and "items" in schema:
        return all(_valid_arguments(item, schema["items"]) for item in value)
    return True


TOOL_MAP: dict[str, str] = {
    "move_head": "self.robot.set_head_angles",
    "set_avatar": "self.display.set_avatar",
    "set_all_leds": "self.led.set_all",
    # Upstream xiaozhi-esp32 firmware tools (main/mcp_server.cc and the board's
    # InitializeTools). Declared with the device tools; a device that does not
    # offer one answers "not offered by device".
    "get_device_status": "self.get_device_status",
    "set_device_volume": "self.audio_speaker.set_volume",
    "set_screen_brightness": "self.screen.set_brightness",
    "set_screen_theme": "self.screen.set_theme",
    "start_game": "self.game.start",
    "stop_game": "self.game.stop",
    # Dispatched by _dispatch_rename (pinyin + persona.json), listed here so a
    # device without the persona tools does not get the declaration.
    "rename_assistant": "self.persona.set_name",
    # Dispatched by _dispatch_design_avatar / _dispatch_apply_avatar (stackchan_mcp.avatar);
    # only a device with the avatar update tool gets them.
    "design_avatar": "self.avatar.update",
    "apply_avatar": "self.avatar.update",
    "discard_avatar_draft": "self.avatar.update",
}

XIAOZHI_DEVICE_TOOL_NAMES = frozenset(
    {
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
)


AVATAR_FACE_ENUM = [
    "idle",
    "happy",
    "working_typing",
    "juggling",
    "sweeping",
    "sleeping",
    "embarrassed",
    "error",
    "notification",
    "thinking",
]


EMOTION_MOOD_ENUM = [
    "excited",
    "happy",
    "neutral",
    "tired",
    "sad",
    "curious",
    "apologetic",
]


EMOTION_INTENSITY_ENUM = ["low", "high"]


MEDIA_ACTION_ENUM = ["play", "pause", "play_pause", "next", "previous"]


@dataclass(frozen=True)
class ToolMetadata:
    """Live tool dispatch metadata. Tools in the same exclusive_group run one after another within a turn."""

    side_effect: bool
    exclusive_group: str | None = None
    timeout_s: float = 10.0


TOOL_METADATA: dict[str, ToolMetadata] = {
    "move_head": ToolMetadata(True, "device", 5.0),
    "set_avatar": ToolMetadata(True, "device", 5.0),
    "set_all_leds": ToolMetadata(True, "device", 5.0),
    "express_emotion": ToolMetadata(True, "device", 5.0),
    "get_device_status": ToolMetadata(False, None, 5.0),
    "set_device_volume": ToolMetadata(True, "device", 5.0),
    "set_screen_brightness": ToolMetadata(True, "device", 5.0),
    "set_screen_theme": ToolMetadata(True, "device", 5.0),
    "start_game": ToolMetadata(True, "device", 5.0),
    "stop_game": ToolMetadata(True, "device", 5.0),
    "rename_assistant": ToolMetadata(True, "device", 10.0),
    "design_avatar": ToolMetadata(True, "avatar", 25.0),
    "apply_avatar": ToolMetadata(True, "avatar", 40.0),
    "discard_avatar_draft": ToolMetadata(True, "avatar", 10.0),
    "end_conversation": ToolMetadata(True, "conversation", 5.0),
    "ask_grokbot": ToolMetadata(False, None, 8.0),
    "ask_claude": ToolMetadata(False, None, 35.0),
    "get_current_datetime": ToolMetadata(False, None, 2.0),
    "self.tracking.start": ToolMetadata(True, "face_tracking", 8.0),
    "self.tracking.stop": ToolMetadata(True, "face_tracking", 8.0),
    "open_app": ToolMetadata(True, "mac_app", 8.0),
    "open_url": ToolMetadata(True, "mac_app", 8.0),
    "web_search": ToolMetadata(True, "mac_app", 8.0),
    "media_control": ToolMetadata(True, "media", 8.0),
    "set_volume": ToolMetadata(True, "media", 5.0),
    "lock_screen": ToolMetadata(True, "mac_system", 5.0),
    "take_screenshot": ToolMetadata(True, "mac_capture", 8.0),
    "run_shortcut": ToolMetadata(True, "shortcut", 60.0),
    "list_shortcuts": ToolMetadata(False, None, 8.0),
    "run_mac_task": ToolMetadata(True, "mac_task", 8.0),
    "check_mac_task": ToolMetadata(False, None, 3.0),
}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def device_tools_enabled() -> bool:
    """Expose face/LED/head tools to Gemini (needs MCP-capable firmware)."""
    return _env_flag("STACKCHAN_GEMINI_DEVICE_TOOLS")


def mac_control_enabled() -> bool:
    """Let voice commands control this Mac. Off unless explicitly enabled."""
    return _env_flag("STACKCHAN_MAC_CONTROL")


def ask_claude_enabled() -> bool:
    """Offer the ask_claude tool only when the claude CLI is installed.

    STACKCHAN_ASK_CLAUDE=0 turns it off even when the CLI is found, so a
    user with Claude Code installed is not spending their quota by default
    without a way out.
    """
    if not _env_flag("STACKCHAN_ASK_CLAUDE", default=True):
        return False
    return find_claude_bin() is not None


def build_function_declarations(*, types=None) -> list[Any]:
    """Return the FunctionDeclaration list for Gemini Live setup.

    Imported lazily so the module stays importable when google-genai is not
    installed (the unit tests pin a fake `_types` module instead).
    """
    types = types or _PlainTypes

    move_head = types.FunctionDeclaration(
        name="move_head",
        description=(
            "Move the robot's head. yaw: -90..90 (negative=left). "
            "pitch: 0..60 (0=level, larger=looking up). speed: 100..1000."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "yaw": types.Schema(type="INTEGER", description="-90..90"),
                "pitch": types.Schema(type="INTEGER", description="0..60"),
                "speed": types.Schema(
                    type="INTEGER",
                    description="100..1000, default 150, natural",
                ),
            },
            required=["yaw", "pitch"],
        ),
    )

    set_avatar = types.FunctionDeclaration(
        name="set_avatar",
        description=(
            "Change the face on the LCD. face must be one of: "
            "idle, happy, working_typing, juggling, sweeping, sleeping, "
            "embarrassed, error, notification, thinking."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "face": types.Schema(
                    type="STRING",
                    format="enum",
                    enum=AVATAR_FACE_ENUM,
                    description="One of the supported face names.",
                ),
            },
            required=["face"],
        ),
    )

    set_all_leds = types.FunctionDeclaration(
        name="set_all_leds",
        description=("Set every base LED to the same RGB color. r/g/b are 0..255."),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "r": types.Schema(type="INTEGER", description="0..255"),
                "g": types.Schema(type="INTEGER", description="0..255"),
                "b": types.Schema(type="INTEGER", description="0..255"),
            },
            required=["r", "g", "b"],
        ),
    )

    express_emotion = types.FunctionDeclaration(
        name="express_emotion",
        description=(
            "Express the user's heard emotion through the robot body before "
            "speaking. Call at most once per reply turn, before opening the "
            "verbal response. mood must be one of: excited, happy, neutral, "
            "tired, sad, curious, apologetic. intensity is optional and must "
            "be low or high; default low."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "mood": types.Schema(
                    type="STRING",
                    format="enum",
                    enum=EMOTION_MOOD_ENUM,
                    description=(
                        "One of: excited, happy, neutral, tired, sad, curious, "
                        "apologetic."
                    ),
                ),
                "intensity": types.Schema(
                    type="STRING",
                    format="enum",
                    enum=EMOTION_INTENSITY_ENUM,
                    description="low or high. Defaults to low when omitted.",
                ),
            },
            required=["mood"],
        ),
    )

    end_conversation = types.FunctionDeclaration(
        name="end_conversation",
        description=(
            "End the current conversation turn. Use when the user says thanks, "
            "okay, enough, or otherwise indicates the conversation is over."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={},
        ),
    )

    ask_claude = types.FunctionDeclaration(
        name="ask_claude",
        description=(
            "Hand a hard question to Claude. Call it when the user asks something that needs deep "
            "reasoning, code analysis or complex planning."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "question": types.Schema(type="STRING"),
            },
            required=["question"],
        ),
    )

    bot = tool_bot()
    ask_grokbot = types.FunctionDeclaration(
        name="ask_grokbot",
        description=(
            f"Hand a task to {bot} in the Grok Bot app. "
            f"Returns at once: immediately tell the user the task has been sent to {bot}; do not wait. "
            'Do not call it for small talk, greetings or "who are you". '
            f'When {bot} answers you get a system notice "{bot} replied"; then pass it on briefly.'
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "task": types.Schema(
                    type="STRING",
                    description="What needs doing: the user's words or a short task description",
                ),
            },
            required=["task"],
        ),
    )

    get_current_datetime = types.FunctionDeclaration(
        name="get_current_datetime",
        description="Get the current date, time and day of the week",
        parameters=types.Schema(
            type="OBJECT",
            properties={},
        ),
    )

    start_face_tracking = types.FunctionDeclaration(
        name="self.tracking.start",
        description=(
            "Turn on face following: the head turns to follow the user's face through the Mac camera. "
            'Call only when the user explicitly asks, e.g. "look at me" or "follow my face"; '
            "never during small talk."
        ),
        parameters=types.Schema(type="OBJECT", properties={}),
    )
    stop_face_tracking = types.FunctionDeclaration(
        name="self.tracking.stop",
        description=(
            "Turn off face following: the head stops turning toward the user's face. "
            'Call when the user asks, e.g. "stop looking at me" or "stop following me".'
        ),
        parameters=types.Schema(type="OBJECT", properties={}),
    )

    get_device_status = types.FunctionDeclaration(
        name="get_device_status",
        description=(
            "Read the device's current state: speaker volume, screen brightness and theme, "
            "battery, network. Call it before a relative change such as turning the volume up "
            "or down a bit, so the new absolute value is based on the current one."
        ),
        parameters=types.Schema(type="OBJECT", properties={}),
    )
    set_device_volume = types.FunctionDeclaration(
        name="set_device_volume",
        description=(
            "Set the device's own speaker volume (the voice you are speaking with), 0..100. "
            "This is the device, not the Mac."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={"volume": types.Schema(type="INTEGER", description="0..100")},
            required=["volume"],
        ),
    )
    set_screen_brightness = types.FunctionDeclaration(
        name="set_screen_brightness",
        description="Set the device screen brightness, 0..100.",
        parameters=types.Schema(
            type="OBJECT",
            properties={"brightness": types.Schema(type="INTEGER", description="0..100")},
            required=["brightness"],
        ),
    )
    set_screen_theme = types.FunctionDeclaration(
        name="set_screen_theme",
        description="Switch the device screen between the light and dark theme.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "theme": types.Schema(
                    type="STRING",
                    format="enum",
                    enum=["light", "dark"],
                    description="light or dark",
                ),
            },
            required=["theme"],
        ),
    )
    start_game = types.FunctionDeclaration(
        name="start_game",
        description=(
            "Start a mini game on the device screen that the user plays by touch. "
            "Games: catch (throw and catch a ball). Call it when the user wants to play a game."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={"name": types.Schema(type="STRING", description="game name, default catch")},
        ),
    )
    stop_game = types.FunctionDeclaration(
        name="stop_game",
        description="Stop the mini game running on the device screen.",
        parameters=types.Schema(type="OBJECT", properties={}),
    )
    rename_assistant = types.FunctionDeclaration(
        name="rename_assistant",
        description=(
            "Give yourself the new name the user chose. The device's wake word becomes the "
            "same name right away, and from the next conversation you speak as that name. "
            "Only call it when the user clearly states the new name for you. "
            "Chinese names of 2 to 4 characters only; the result's `spoken` says whether it "
            "worked or why not."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={"name": types.Schema(type="STRING", description="the new Chinese name, 2 to 4 characters")},
            required=["name"],
        ),
    )

    avatar_tools = _build_avatar_declarations(types)

    device = (
        [
            move_head,
            set_avatar,
            set_all_leds,
            express_emotion,
            get_device_status,
            set_device_volume,
            set_screen_brightness,
            set_screen_theme,
            start_game,
            stop_game,
            rename_assistant,
            *avatar_tools,
        ]
        if device_tools_enabled()
        else []
    )
    mac = _build_mac_declarations(types) if mac_control_enabled() else []
    grokbot = [ask_grokbot] if bot else []
    claude = [ask_claude] if ask_claude_enabled() else []
    tracking = (
        [start_face_tracking, stop_face_tracking]
        if face_tracker_autostart_enabled()
        else []
    )
    return [
        *device,
        end_conversation,
        *grokbot,
        *claude,
        get_current_datetime,
        *tracking,
        *mac,
    ]


def _plain_schema(types: Any, schema: dict[str, Any]) -> Any:
    """Rebuild a JSON schema through ``types.Schema`` so provider quirks apply at every level."""
    values = dict(schema)
    if "properties" in values:
        values["properties"] = {key: _plain_schema(types, sub) for key, sub in values["properties"].items()}
    if "items" in values:
        values["items"] = _plain_schema(types, values["items"])
    return types.Schema(**values)


def _build_avatar_declarations(types: Any) -> list[Any]:
    """design_avatar / apply_avatar when the generator and a base assets image exist."""
    from . import avatar as _avatar  # noqa: PLC0415

    if not _avatar.available():
        return []
    schema = _avatar.spec_schema()
    if schema is None:
        return []
    design_avatar = types.FunctionDeclaration(
        name="design_avatar",
        description=(
            "Draw a draft of a new look and show it live on the device screen (nothing is installed). "
            "mode `new`: start a look from the user's description, filling `spec` from it (species, ears, "
            "colours, pattern, accessories, eye style; colours as #rrggbb or a common colour name; leave out "
            "what they did not mention). mode `adjust`: change the current draft, passing in `spec` only the "
            "fields to change (`accessories` is replaced as a whole, so repeat the ones to keep). mode `back`: "
            "return to the previous draft (`spec` may be empty). The result's `spec` is the full current draft, "
            "`look` describes it, `spoken` is what to say. Call it as often as the user wants changes."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "description": types.Schema(type="STRING", description="the user's words for this change"),
                "mode": types.Schema(type="STRING", enum=["new", "adjust", "back"], description="new | adjust | back"),
                "spec": _plain_schema(types, schema),
            },
            required=["description", "mode"],
        ),
    )
    apply_avatar = types.FunctionDeclaration(
        name="apply_avatar",
        description=(
            "Install the current draft for good. Call it ONLY when the user says the draft is final "
            "(this one, go ahead and change), never on your own initiative and never as part of design_avatar. "
            "The device downloads the look and reboots (about two minutes); this call ends then. Your "
            "name and wake word stay the same. Say only the result's `spoken`."
        ),
        parameters=types.Schema(type="OBJECT", properties={}),
    )
    discard_avatar_draft = types.FunctionDeclaration(
        name="discard_avatar_draft",
        description=(
            "Drop the draft and show the installed look again, when the user says they do not want to "
            "change after all (never mind, keep the old look)."
        ),
        parameters=types.Schema(type="OBJECT", properties={}),
    )
    return [design_avatar, apply_avatar, discard_avatar_draft]


def _build_mac_declarations(types: Any) -> list[Any]:
    """FunctionDeclarations for the Mac-control lane (see mac_control.py)."""
    no_args = types.Schema(type="OBJECT", properties={})
    media_control_response = types.Schema(
        type="OBJECT",
        properties={
            "ok": types.Schema(
                type="BOOLEAN", description="Whether the tool succeeded"
            ),
            "action": types.Schema(
                type="STRING",
                format="enum",
                enum=MEDIA_ACTION_ENUM,
                description="The media action that was performed",
            ),
            "player": types.Schema(
                type="STRING", description="The player that was controlled"
            ),
            "player_state": types.Schema(
                type="STRING",
                description="The real playback state read back afterwards, such as playing, paused or stopped",
            ),
            "auto_opened": types.Schema(
                type="STRING", description="The player that was opened automatically"
            ),
            "error": types.Schema(type="STRING", description="Why it failed"),
        },
        required=["ok"],
    )
    run_mac_task_response = types.Schema(
        type="OBJECT",
        properties={
            "ok": types.Schema(type="BOOLEAN", description="Whether the task started"),
            "status": types.Schema(
                type="STRING",
                format="enum",
                enum=["started"],
                description="Background task start status",
            ),
            "state": types.Schema(type="STRING", description="Background task state"),
            "task_id": types.Schema(
                type="INTEGER", description="Background task number"
            ),
            "estimated_seconds": types.Schema(
                type="INTEGER",
                description="Estimated seconds until done",
            ),
            "user_message": types.Schema(
                type="STRING",
                description="A short sentence to tell the user, in their language",
            ),
            "error": types.Schema(type="STRING", description="Why it failed"),
        },
        required=["ok"],
    )

    return [
        types.FunctionDeclaration(
            name="open_app",
            description="Open an app on the user's Mac, for example Xcode, Safari or Finder.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "name": types.Schema(type="STRING", description="App name"),
                },
                required=["name"],
            ),
        ),
        types.FunctionDeclaration(
            name="open_url",
            description="Open an http or https URL in the default browser on the user's Mac.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "url": types.Schema(type="STRING", description="Full URL"),
                },
                required=["url"],
            ),
        ),
        types.FunctionDeclaration(
            name="web_search",
            description=(
                "Open a search results page in the browser on the user's Mac (the default browser when "
                "Chrome is not installed). query can be in any language; engine is optional: google, bing, "
                "baidu or grok, default grok (Grok on the web). "
                + (
                    "Use it only when the user explicitly wants a browser search; when they just want an "
                    "answer, use ask_grokbot. "
                    if grokbot_enabled()
                    else 'When the user says "search for X" or "look up X", use grok. '
                )
                + "Pass engine only when the user names a search engine. Do not build search URLs yourself."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "query": types.Schema(type="STRING", description="Search terms"),
                    "engine": types.Schema(
                        type="STRING",
                        description="Search engine: google | bing | baidu | grok, default grok",
                    ),
                },
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="media_control",
            description=(
                "Control music playback on the Mac (Spotify or Music). Prefer play or pause to make sure "
                "music is playing or stopped; play_pause toggles, use it with care. "
                "player_state in the result is the real final state."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "action": types.Schema(
                        type="STRING",
                        format="enum",
                        enum=MEDIA_ACTION_ENUM,
                        description=(
                            "play | pause | play_pause | next | previous; "
                            "play_pause toggles, use it with care"
                        ),
                    ),
                },
                required=["action"],
            ),
            response=media_control_response,
        ),
        types.FunctionDeclaration(
            name="set_volume",
            description="Set the Mac output volume. level is 0..100; mute is true or false.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "level": types.Schema(type="INTEGER", description="0..100"),
                    "mute": types.Schema(type="BOOLEAN", description="Mute on or off"),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="lock_screen",
            description="Lock the user's Mac screen.",
            parameters=no_args,
        ),
        types.FunctionDeclaration(
            name="take_screenshot",
            description="Take a full-screen screenshot on the Mac, save it to the desktop and return the file path.",
            parameters=no_args,
        ),
        types.FunctionDeclaration(
            name="run_shortcut",
            description="Run an Apple Shortcut, matched exactly by name.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "name": types.Schema(type="STRING", description="Shortcut name"),
                },
                required=["name"],
            ),
        ),
        types.FunctionDeclaration(
            name="list_shortcuts",
            description="List the names of the Apple Shortcuts available on the Mac.",
            parameters=no_args,
        ),
        types.FunctionDeclaration(
            name="run_mac_task",
            description=(
                "Give a complex task to a background Claude on the Mac (organising files, research, "
                'writing documents, anything with code). Returns "started" at once; you get a system '
                "notice when it finishes. Write task as a complete, clear description."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "task": types.Schema(
                        type="STRING", description="Complete task description"
                    ),
                },
                required=["task"],
            ),
            response=run_mac_task_response,
        ),
        types.FunctionDeclaration(
            name="check_mac_task",
            description="Check the progress and result of background Mac tasks.",
            parameters=no_args,
        ),
    ]


EMOTION_SYSTEM_INSTRUCTION = """## Expressing emotion

You can hear the user's tone and mood in their voice. Before you start each spoken reply, call express_emotion(mood, intensity) once (at most once per turn), so the body reacts before the words.
Examples: the user sounds tired → express_emotion(mood="tired", intensity="low"); the user is excited about some progress → express_emotion(mood="excited", intensity="high").
This does not change when a conversation ends: call end_conversation only when the user clearly says goodbye or that they are done talking."""


DEVICE_CONTROL_INSTRUCTION = """## The device

You are speaking from a small device with its own speaker and screen. The user may ask you to control it:
- set_device_volume(volume) sets the device's speaker volume 0..100; set_screen_brightness(brightness) the screen brightness 0..100; set_screen_theme(theme) switches between light and dark
- for a relative change ("a bit louder", "turn it down") call get_device_status first and then set the new absolute value; "mute" means volume 0
- "volume" means this device's speaker unless the user says the Mac, the computer or the music player
- start_game(name) starts a touch mini game on the screen (catch / 抛接球); stop_game() ends it
Confirm each change in one short sentence."""


RENAME_INSTRUCTION = """## Your name

The user can rename you: when they clearly say a new name for you (以后叫你X / 给你改名叫X), call rename_assistant(name) with just that name. The device then wakes to the new name and the next conversation speaks as it. Say only the result's `spoken`; if it refused, ask for another name. Do not rename yourself on a passing mention or a joke; when unsure, ask once."""


AVATAR_INSTRUCTION = """## Your look

The user can redesign how you look, in several rounds, and sees each draft live on the device screen:
- When they describe a new look (把你变成一只橘色的小猫 / 换个样子 / 戴顶帽子), call design_avatar(mode="new", description, spec) with a spec built from their words only.
- When they want a change to the draft (耳朵再长一点 / 换成蓝色围巾 / 去掉眼镜), call design_avatar(mode="adjust", description, spec) with just the changed fields; for 回到上一个 / 还是刚才那个 use mode="back".
- After every design_avatar say its `spoken` (it describes the draft on the screen) and invite more changes; do not ask "shall I apply it".
- Call apply_avatar() only when the user says the draft is final (就这个了 / 就用这个 / 换吧 / 定了). Never call it on your own, never right after design_avatar, and never when they merely say the draft looks nice. If they say 不换了 / 算了, call discard_avatar_draft().
- Do not redesign on a passing remark; when unsure what they meant, ask once."""


VOICE_ONLY_SYSTEM_INSTRUCTION = """## Speak first

In small talk, answer out loud right away.
Call end_conversation only when the user clearly says goodbye or that they are done talking."""


def grokbot_instruction(bot: str, *, mac_control: bool) -> str:
    """Routing rules for the ask_grokbot tool (hand tasks to a Grok Bot agent).

    ``bot`` is the agent name from STACKCHAN_TOOL_BOT; the spoken phrases use
    it, so a user who named the agent differently hears that name.
    """
    lines = [
        f"## Handing tasks to {bot}",
        "",
        "When the user is only chatting, saying hello or asking who you are, answer in your own voice"
        " and do not call ask_grokbot.",
        "Call ask_grokbot(task) only when the user clearly wants something done (look something up,"
        " research, write something, ask an assistant, arrange a task).",
        f"ask_grokbot returns at once. Immediately tell the user, in your own voice, that the task has been"
        f" sent to {bot}; do not wait in silence.",
        f'When {bot} answers you get a system notice "{bot} replied"; then briefly pass on what {bot}'
        " said, in your own voice. Do not make up a result before that.",
    ]
    if mac_control:
        lines.append(
            f"Tasks that take time, such as looking something up, researching a topic or asking {bot} to"
            " do something, go to ask_grokbot; do not open a browser with web_search for them."
        )
    return "\n".join(lines)


AGENT_TOOLS_INSTRUCTION = """## Tools

You can search the web: answer factual questions (weather, news, general knowledge) directly; search results are merged in automatically.

When the user asks for the time or date, call get_current_datetime to get it exactly.
"""


ASK_CLAUDE_INSTRUCTION = (
    "For questions that need deep analysis, reading code, complex reasoning or detailed planning, "
    "call ask_claude(question) and pass on the answer briefly in your own words."
)


def agent_tools_instruction(*, ask_claude: bool, model_search: bool = True) -> str:
    """The Tools rules; the ask_claude line only when that tool exists."""
    head, _, tail = AGENT_TOOLS_INSTRUCTION.strip().rpartition("\n\n")
    if not model_search:
        head = "## Tools"
    if ask_claude:
        return f"{head}\n\n{ASK_CLAUDE_INSTRUCTION}\n\n{tail}"
    return f"{head}\n\n{tail}"


MAC_CONTROL_INSTRUCTION = """## Mac control

You can control the user's Mac. For quick actions call the tool directly and confirm in one short sentence:
- open_app(name) opens an app; open_url(url) opens a web page; web_search(query) searches in the browser (Grok on the web by default)
- media_control(action) controls music playback. Prefer play or pause to make sure music is playing or stopped, and next/previous to change tracks; play_pause toggles, so use it only when the user explicitly asks to toggle. The tool returns player_state: report the real state to the user, not what you intended

{web_search_rule}
- set_volume(level or mute) changes the volume
- lock_screen() locks the screen; take_screenshot() saves a screenshot to the desktop
- run_shortcut(name) runs a Shortcut; list_shortcuts() lists the available Shortcuts

For complex tasks (organising files, researching and writing, anything with code) call run_mac_task(task) with a complete, clear task description. It returns "started" at once: tell the user the task has started and do not wait. When slow actions and background tasks finish you get a system notice; then report the result in one or two sentences. When the user asks about progress, call check_mac_task.

Safety: before anything irreversible, such as deleting files, overwriting data or sending messages or email, repeat back to the user what you are about to do and get their explicit agreement first."""


WEB_SEARCH_RULE = (
    'web_search(query) searches in the browser; when the user says "search for X" or "look up X" it uses '
    "Grok (grok.com) by default. Pass engine only when the user names a search engine (Google, Baidu, Bing)."
)


WEB_SEARCH_RULE_WITH_GROKBOT = (
    "web_search(query) opens a search page in the browser; use it only when the user explicitly asks to search "
    "in the browser or names a search engine (Google, Baidu, Bing), and pass engine only then. When the user "
    "just wants an answer (look it up, search for it, find out), hand it to ask_grokbot."
)


def mac_control_instruction(*, grokbot: bool = False) -> str:
    rule = WEB_SEARCH_RULE_WITH_GROKBOT if grokbot else WEB_SEARCH_RULE
    return MAC_CONTROL_INSTRUCTION.replace("{web_search_rule}", rule)


@dataclass(frozen=True)
class ToolExtension:
    """Explicit opt-in extension; private phone code can register without public imports."""

    declarations: tuple[dict[str, Any], ...]
    dispatch: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
    metadata: dict[str, ToolMetadata] = field(default_factory=dict)
    close: Callable[[], Awaitable[None]] | None = None
    instructions: str = ""
    bind_result: Callable[[Callable[[str], Awaitable[bool]]], None] | None = None
    on_user_input: Callable[[str, bool], Any] | None = None


class VoiceToolExecutor:
    """Shared local execution and result notices; providers adapt only the wire protocol."""

    def __init__(
        self,
        esp32=None,
        *,
        usb_transport=None,
        on_head_command=None,
        wake_gate_state_getter=None,
        set_face_tracking=None,
        on_end_conversation=None,
        announce_result=None,
        debug_status=None,
        extensions=(),
        cancel_idle=None,
        mac=None,
        mac_controller=None,
        declaration_provider=None,
        instruction_provider=None,
    ):
        self._esp32 = esp32
        self._usb_transport = usb_transport
        self._on_head_command = on_head_command
        self._wake_gate_state_getter = wake_gate_state_getter
        self._set_face_tracking = set_face_tracking
        self._on_end_conversation = on_end_conversation
        self._announce_result = announce_result
        self._cancel_idle = cancel_idle
        self._status = debug_status or get_debug_status()
        self._declaration_provider = declaration_provider or build_function_declarations
        self._instruction_provider = instruction_provider
        self._extensions = tuple(extensions)
        self._extension_routes = {}
        self._extension_metadata = {}
        existing = {entry["name"] for entry in self._declaration_provider()}
        for extension in self._extensions:
            for entry in extension.declarations:
                name = entry["name"]
                if name in existing or name in self._extension_routes:
                    raise ValueError("duplicate voice tool extension")
                self._extension_routes[name] = extension
            self._extension_metadata.update(extension.metadata)
        self._current_turn_emotion_face = None
        self._avatar_drafts = []     # design_avatar history (PendingDesign), oldest first
        self._avatar_index = -1      # the draft shown on the device, -1 = none
        self._avatar_preview_live = False  # the device is drawing a draft (self.avatar.preview)
        self._avatar_designer = None
        self._emotion_tasks = set()
        self._background_tasks = set()
        self._active_executions = {}
        self._execution_owner = ContextVar("voice_tool_execution_owner", default=None)
        self._execution_locks = {}
        self._tools_closed = False
        for extension in self._extensions:
            if extension.bind_result is not None:
                extension.bind_result(self.announce_result)
        self._owned_mac_tasks = set()
        self._cancelled_tool_call_ids = set()
        self._tool_call_task_ids = {}
        self._claude_bin = resolve_claude_bin()
        self._mac = mac_controller if mac_controller is not None else mac
        if self._mac is None and mac_control_enabled():
            self._mac = MacController(
                claude_bin=self._claude_bin,
                on_task_done=self._announce_mac_task,
                on_task_start=lambda: self._set_face("working_typing"),
            )

    def declarations(self) -> list[dict[str, Any]]:
        return [
            *self._offered_declarations(self._declaration_provider()),
            *(
                entry
                for extension in self._extensions
                for entry in extension.declarations
            ),
        ]

    def _offered_declarations(self, declarations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop device tools a connected device is known not to offer.

        The voice session is built before the device's tools/list answers, so
        the filter uses the list remembered from the device's last connection
        (ESP32Manager.known_tool_names); with nothing known every tool stays.
        """
        known = getattr(self._esp32, "known_tool_names", None)
        if not known:
            return list(declarations)
        kept = []
        for entry in declarations:
            target = TOOL_MAP.get(entry["name"])
            if target is not None and target not in known:
                continue
            kept.append(entry)
        return kept

    def instructions(self, *, model_search: bool = True) -> str:
        if self._instruction_provider is not None:
            base = self._instruction_provider()
        else:
            names = {entry["name"] for entry in self.declarations()}
            parts = [
                EMOTION_SYSTEM_INSTRUCTION
                if "express_emotion" in names
                else VOICE_ONLY_SYSTEM_INSTRUCTION,
                agent_tools_instruction(
                    ask_claude="ask_claude" in names, model_search=model_search
                ),
            ]
            if "set_device_volume" in names:
                parts.append(DEVICE_CONTROL_INSTRUCTION)
            if "rename_assistant" in names:
                parts.append(RENAME_INSTRUCTION)
            if "design_avatar" in names:
                parts.append(AVATAR_INSTRUCTION)
                announcement = self._avatar_announcement()
                if announcement:
                    parts.append(announcement)
            bot = tool_bot() if "ask_grokbot" in names else ""
            mac_enabled = bool(names & MAC_TOOL_NAMES)
            if bot:
                parts.append(grokbot_instruction(bot, mac_control=mac_enabled))
            if mac_enabled:
                parts.append(mac_control_instruction(grokbot=bool(bot)))
            base = "\n\n".join(parts)
        return "\n\n".join(
            [
                base,
                *(
                    extension.instructions
                    for extension in self._extensions
                    if extension.instructions
                ),
            ]
        )

    def metadata(self, name: str) -> ToolMetadata:
        return self._tool_metadata(name)

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._tools_closed:
            return {"ok": False, "error": "voice tools are stopped"}
        declaration = next(
            (entry for entry in self.declarations() if entry["name"] == name), None
        )
        if declaration is None:
            return {"ok": False, "error": "voice tool is not enabled"}
        if not isinstance(args, dict) or not _valid_arguments(
            args, declaration.get("parameters", {})
        ):
            return {"ok": False, "error": "invalid tool arguments"}
        current = asyncio.current_task()
        owner = self._execution_owner.get() or current
        self._active_executions[current] = owner
        token = self._execution_owner.set(owner)
        try:
            meta = self.metadata(name)
            if meta.side_effect:
                group = meta.exclusive_group or f"tool:{name}"
                lock = self._execution_locks.setdefault(group, asyncio.Lock())
                async with lock:
                    if self._tools_closed:
                        return {"ok": False, "error": "voice tools are stopped"}
                    return await self._dispatch_tool_with_timeout(name, args)
            return await self._dispatch_tool_with_timeout(name, args)
        finally:
            self._active_executions.pop(current, None)
            self._execution_owner.reset(token)

    async def observe_user_input(self, text: str, finished: bool = False) -> None:
        """Only provider-confirmed user input is forwarded to explicit extensions."""
        if self._tools_closed:
            return
        for extension in self._extensions:
            if extension.on_user_input is not None:
                result = extension.on_user_input(text, finished)
                if inspect.isawaitable(result):
                    await result

    async def announce_result(self, text: str) -> bool:
        """Extension results share the provider's background-notice channel."""
        return await self._notify(text=text)

    @property
    def current_turn_emotion_face(self):
        return self._current_turn_emotion_face

    def clear_turn_emotion_face(self):
        self._current_turn_emotion_face = None

    async def _notify(self, *, text):
        if self._tools_closed or self._announce_result is None:
            return False
        result = self._announce_result(text)
        if inspect.isawaitable(result):
            result = await result
        return result is not False

    def _own_background(self, coroutine, *, name):
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def close(self):
        await self._end_avatar_preview_quietly()
        self._tools_closed = True
        current = asyncio.current_task()
        owner = self._execution_owner.get()
        executions = {
            task
            for task, task_owner in self._active_executions.items()
            if owner is None or task_owner is not owner
        }
        tasks = list(
            (self._background_tasks | self._emotion_tasks | executions) - {current}
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._mac is not None:
            for task_id in self._owned_mac_tasks:
                await self._mac.cancel_task(task_id)
        for extension in self._extensions:
            if extension.close is not None:
                await extension.close()

    @staticmethod
    def _tool_call_cancellation_ids(cancellation: Any) -> list[str]:
        raw_ids = (
            getattr(cancellation, "ids", None)
            or getattr(cancellation, "function_call_ids", None)
            or getattr(cancellation, "function_ids", None)
            or []
        )
        if isinstance(raw_ids, str):
            return [raw_ids]
        return [str(item) for item in raw_ids if item is not None]

    async def _handle_tool_call_cancellation(self, cancellation: Any) -> None:
        ids = self._tool_call_cancellation_ids(cancellation)
        for call_id in ids:
            self._cancelled_tool_call_ids.add(call_id)
            self._status.on_tool_call_cancelled()
            task_id = self._tool_call_task_ids.get(call_id)
            if task_id is None:
                logger.info(
                    "Gemini Live tool call cancelled before background task: %s",
                    call_id,
                )
                continue
            if self._mac is None:
                logger.info(
                    "Gemini Live tool call cancelled but Mac control is disabled: %s",
                    call_id,
                )
                continue
            result = await self._mac.cancel_task(task_id)
            logger.info(
                "Gemini Live tool call cancellation id=%s task_id=%s result=%s",
                call_id,
                task_id,
                result,
            )

    async def dispatch_calls(self, function_calls: list[Any]) -> list[dict[str, Any]]:

        responses: list[dict[str, Any] | None] = [None] * len(function_calls)

        async def dispatch_one(index: int, fc: Any) -> None:
            name = getattr(fc, "name", "")
            call_id = getattr(fc, "id", None)
            call_id_text = str(call_id) if call_id is not None else ""
            if call_id_text and call_id_text in self._cancelled_tool_call_ids:
                responses[index] = dict(
                    id=call_id,
                    name=name,
                    response={
                        "ok": False,
                        "cancelled": True,
                        "error": "tool call cancelled before start",
                    },
                )
                return
            args = dict(getattr(fc, "args", None) or {})
            result = await self._dispatch_tool_with_timeout(name, args)
            task_id = result.get("task_id")
            if call_id_text and isinstance(task_id, int):
                self._tool_call_task_ids[call_id_text] = task_id
            responses[index] = dict(
                id=call_id,
                name=name,
                response=result,
            )

        async def dispatch_serial(items: list[tuple[int, Any]]) -> None:
            for index, fc in items:
                await dispatch_one(index, fc)

        serial_groups: dict[str, list[tuple[int, Any]]] = {}
        parallel_tasks: list[asyncio.Task[None]] = []
        for index, fc in enumerate(function_calls):
            name = str(getattr(fc, "name", ""))
            meta = self._tool_metadata(name)
            if not meta.side_effect:
                parallel_tasks.append(asyncio.create_task(dispatch_one(index, fc)))
                continue
            group = meta.exclusive_group or f"tool:{name}"
            serial_groups.setdefault(group, []).append((index, fc))

        for items in serial_groups.values():
            parallel_tasks.append(asyncio.create_task(dispatch_serial(items)))

        if parallel_tasks:
            await asyncio.gather(*parallel_tasks)

        return [resp for resp in responses if resp is not None]

    def _tool_metadata(self, name: str) -> ToolMetadata:
        return self._extension_metadata.get(
            name, TOOL_METADATA.get(name, ToolMetadata(True, f"tool:{name}", 10.0))
        )

    async def _dispatch_tool_with_timeout(
        self,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        meta = self._tool_metadata(name)
        try:
            return await asyncio.wait_for(
                self._dispatch_tool(name, args),
                timeout=meta.timeout_s,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"tool timeout after {meta.timeout_s:.0f}s"}
        except Exception as exc:
            logger.exception("Gemini Live tool %s dispatch crashed", name)
            return {"ok": False, "error": str(exc)}

    async def _dispatch_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self._dispatch_tool_impl(name, args)
        if isinstance(result.get("task_id"), int):
            self._owned_mac_tasks.add(result["task_id"])
        self._log_tool_result(name, args, result)
        return result

    async def _dispatch_tool_impl(
        self, name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Map a Gemini function call to the right ESP32 MCP tool.

        Prefers the USB Serial/JTAG channel when wired up: same low-latency
        path TrackingBridge uses. Falls back to the WebSocket esp32.call_tool
        on any USB failure, and skips entirely when both channels are down.
        """
        if name in self._extension_routes:
            return await self._extension_routes[name].dispatch(name, args)
        if name == "end_conversation":
            return await self._dispatch_end_conversation()
        if name == "set_all_leds" and self._should_defer_set_all_leds():
            logger.info(
                "Gemini Live set_all_leds deferred: wake_gate_state=%s args=%s",
                WakeGateState.LISTENING.value,
                self._summarize_tool_args(args),
            )
            return {"ok": True, "deferred": "listening_status_light_active"}
        if name == "express_emotion":
            return await self._dispatch_express_emotion(args)
        if name == "ask_grokbot":
            return await self._dispatch_ask_grokbot(args)
        if name == "ask_claude":
            return await self._dispatch_ask_claude(args)
        if name == "get_current_datetime":
            return self._dispatch_get_datetime()
        if name == "rename_assistant":
            return await self._dispatch_rename(args)
        if name == "design_avatar":
            return await self._dispatch_design_avatar(args)
        if name == "apply_avatar":
            return await self._dispatch_apply_avatar()
        if name == "discard_avatar_draft":
            return await self._dispatch_discard_avatar_draft()
        if name in {"self.tracking.start", "self.tracking.stop"}:
            if self._set_face_tracking is None:
                return {"ok": False, "error": "face tracking unavailable"}
            return await self._set_face_tracking(name == "self.tracking.start")
        if name in MAC_TOOL_NAMES:
            if self._mac is None:
                return {"ok": False, "error": "Mac control is turned off"}
            return await self._mac.dispatch(name, args)

        esp32_name = TOOL_MAP.get(name)
        if esp32_name is None:
            logger.warning("Gemini Live unknown tool name: %s", name)
            return {"ok": False, "error": f"unknown tool {name}"}

        normalized = self._normalize_args(name, args)
        usb_up = self._usb_transport is not None and getattr(
            self._usb_transport, "connected", False
        )
        ws_up = getattr(self._esp32, "device_connected", False)
        if not usb_up and not ws_up:
            return {"ok": False, "error": "device offline"}

        if usb_up:
            if name == "move_head" and self._on_head_command is not None:
                self._on_head_command()
            try:
                resp = await self._usb_transport.call_tool(esp32_name, normalized)
                return {"ok": True, "result": resp.get("result")}
            except Exception as exc:
                logger.warning(
                    "Gemini Live USB call %s failed (%s); falling back to WS",
                    esp32_name,
                    exc,
                )

        if not ws_up:
            return {"ok": False, "error": "device offline"}

        if not self._esp32_mcp_supported():
            if name == "move_head":
                return await self._dispatch_device_head(normalized)
            if name == "set_avatar":
                return await self._dispatch_device_emotion(normalized["face"])
            if name == "set_all_leds":
                return await self._dispatch_device_led(normalized)
            return {"ok": False, "error": "ESP32 MCP unsupported (features.mcp=false)"}

        try:
            result, error = await self._esp32.call_tool(esp32_name, normalized)
        except Exception as exc:
            logger.exception("Gemini Live tool %s dispatch failed", name)
            return {"ok": False, "error": str(exc)}

        if error:
            return {"ok": False, "error": error.get("message", str(error))}
        return {"ok": True, "result": result}

    def _esp32_mcp_supported(self) -> bool:
        connection = getattr(self._esp32, "connection", None)
        if connection is None:
            return True
        return bool(getattr(connection, "mcp_supported", True))

    async def _dispatch_device_emotion(self, face: str) -> dict[str, Any]:
        emotion = face_to_device_emotion(face)
        send_emotion = getattr(self._esp32, "send_emotion", None)
        if not callable(send_emotion):
            return {"ok": False, "error": "device emotion channel unavailable"}
        try:
            _result, error = await send_emotion(emotion, notify_activity=False)
        except Exception as exc:
            logger.exception("Gemini Live emotion dispatch failed")
            return {"ok": False, "error": str(exc)}
        if error:
            return {"ok": False, "error": error.get("message", str(error))}
        return {"ok": True, "emotion": emotion}

    async def _dispatch_device_head(self, pose: dict[str, int]) -> dict[str, Any]:
        send_head = getattr(self._esp32, "send_head", None)
        if not callable(send_head):
            return {"ok": False, "error": "device head channel unavailable"}
        if self._on_head_command is not None:
            self._on_head_command()
        try:
            _result, error = await send_head(
                pose["yaw"],
                pose["pitch"],
                pose["speed"],
                notify_activity=False,
                notify_head_command=self._on_head_command is None,
            )
        except Exception as exc:
            logger.exception("Gemini Live head dispatch failed")
            return {"ok": False, "error": str(exc)}
        if error:
            return {"ok": False, "error": error.get("message", str(error))}
        return {
            "ok": True,
            "head": {
                "yaw": pose["yaw"],
                "pitch": pose["pitch"],
                "speed": pose["speed"],
            },
        }

    def _should_defer_set_all_leds(self) -> bool:
        # The speaking blue and the listening cyan are owned by the proxy; the model must not override them.
        if self._status.tts_active:
            return True
        getter = self._wake_gate_state_getter
        if getter is None:
            return False
        try:
            state = getter()
        except Exception:
            logger.debug("wake gate state getter failed; allowing set_all_leds")
            return False
        return state == WakeGateState.LISTENING.value

    async def _dispatch_device_led(self, rgb: dict[str, int]) -> dict[str, Any]:
        send_led = getattr(self._esp32, "send_led", None)
        if not callable(send_led):
            return {"ok": False, "error": "device led channel unavailable"}
        try:
            _result, error = await send_led(
                rgb["r"],
                rgb["g"],
                rgb["b"],
                notify_activity=False,
            )
        except Exception as exc:
            logger.exception("Gemini Live LED dispatch failed")
            return {"ok": False, "error": str(exc)}
        if error:
            return {"ok": False, "error": error.get("message", str(error))}
        return {"ok": True, "led": {"r": rgb["r"], "g": rgb["g"], "b": rgb["b"]}}

    @staticmethod
    def _summarize_tool_args(args: dict[str, Any]) -> str:
        try:
            summary = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except TypeError:
            summary = repr(args)
        if len(summary) > 240:
            return summary[:237] + "..."
        return summary

    def _log_tool_result(
        self,
        name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        ok = bool(result.get("ok"))
        if ok and result.get("deferred"):
            status = f"deferred={result['deferred']}"
        else:
            status = "ok" if ok else f"error={result.get('error', 'unknown')}"
        logger.info(
            "Gemini Live tool call name=%s args=%s %s",
            name,
            self._summarize_tool_args(args),
            status,
        )
        self._status.record_tool_call(
            name,
            ok,
            error=None if ok else str(result.get("error", "unknown")),
        )

    async def _dispatch_express_emotion(self, args: dict[str, Any]) -> dict[str, Any]:
        from .emotion_map import resolve as resolve_emotion  # noqa: PLC0415

        mood = str(args.get("mood") or "neutral")
        intensity = str(args.get("intensity") or "low")
        plan = resolve_emotion(mood, intensity)
        self._current_turn_emotion_face = plan.face

        task = asyncio.create_task(
            self._dispatch_emotion_plan(plan),
            name="gemini-live-express-emotion",
        )
        self._emotion_tasks.add(task)
        task.add_done_callback(self._emotion_tasks.discard)

        r, g, b = plan.led_rgb
        return {
            "ok": True,
            "face": plan.face,
            "led": {"r": r, "g": g, "b": b},
            "head": {"yaw": plan.head_yaw, "pitch": plan.head_pitch},
        }

    async def _set_face(self, face: str) -> None:
        """Best-effort avatar change; visual feedback, never blocks a turn."""
        try:
            await self._dispatch_tool("set_avatar", {"face": face})
        except Exception:
            logger.debug("set_face(%s) failed", face, exc_info=True)

    async def _dispatch_ask_grokbot(self, args: dict[str, Any]) -> dict[str, Any]:
        """Hand a task to the Grok Bot agent and return at once.

        The agent runs in the Grok Bot app and has no way to reach the
        robot's speaker, so every reply piece that comes back through the
        forwarding service (gbot_http_proxy, default 127.0.0.1:18770) is
        pushed into the Live session as a system notice, and Gemini says it
        in its own voice (same approach as ``_announce_mac_task``). If
        nothing comes back, a one-line failure notice is pushed instead.
        """
        from . import gbot_brain

        bot = tool_bot()
        if not bot:
            return {
                "ok": False,
                "error": "ask_grokbot is off: STACKCHAN_TOOL_BOT is not set",
            }
        task = str(args.get("task") or args.get("question") or "").strip()
        if not task:
            return {"ok": False, "error": "task is required"}
        face_task = asyncio.create_task(self._set_face("thinking"))
        face_task.add_done_callback(lambda _t: None)
        bot_id = _bot_setting("tool_bot_id", "STACKCHAN_TOOL_BOT_ID")
        timeout_s = float(_bot_setting("ask_timeout_s", "STACKCHAN_ASK_TIMEOUT", "110"))
        prefix = _bot_setting("task_prefix", "STACKCHAN_TOOL_BOT_PREFIX")
        message = f"{prefix}{task}" if prefix else task

        async def _run_and_announce() -> None:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

            def _pump() -> None:
                try:
                    for event in gbot_brain.iter_gbot_replies(
                        message, timeout_s=timeout_s, bot=bot, bot_id=bot_id
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, ("reply", event))
                except Exception as exc:  # noqa: BLE001
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, ("end", None))

            pump = asyncio.create_task(asyncio.to_thread(_pump))
            pump.add_done_callback(lambda _t: None)
            spoken = 0
            error: Exception | None = None
            while True:
                kind, item = await queue.get()
                if kind == "end":
                    break
                if kind == "error":
                    error = item
                    continue
                piece = str((item or {}).get("reply") or "").strip()
                if not piece:
                    continue
                spoken += 1
                logger.info(
                    "ask_grokbot reply #%s event=%s chars=%s",
                    spoken,
                    (item or {}).get("event"),
                    len(piece),
                )
                announce = self._announce_result
                if announce is None:
                    logger.info("ask_grokbot reply arrived but no live session")
                    continue
                try:
                    await self._notify(
                        text=f"[System notice, not the user speaking] {bot} replied: "
                        f"{piece}\nPass this on to the user briefly, right away, in your own voice and "
                        "in the user's language. Do not call any tools, and do not call ask_grokbot again."
                    )
                except Exception:
                    logger.exception("ask_grokbot reply announcement failed")
            if spoken:
                return
            logger.warning("ask_grokbot background failed: %s", error or "no reply")
            announce = self._announce_result
            if announce is None:
                return
            try:
                await self._notify(
                    text=f"[System notice, not the user speaking] Sending the task to {bot} failed. "
                    f"Tell the user in one sentence, in their language, that it did not get through. "
                    f"Do not make up a reply from {bot}."
                )
            except Exception:
                logger.exception("ask_grokbot failure announcement failed")

        self._own_background(_run_and_announce(), name="ask_grokbot")
        return {
            "ok": True,
            "status": "sent",
            "say": f"Sent to {bot}.",
            "instruction": f"Right away, tell the user in your own voice and in their language that the "
            f"task has been sent to {bot}. Do not wait for the reply; a system notice follows when "
            f"{bot} answers.",
        }

    async def _dispatch_ask_claude(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question", "")).strip()
        if not question:
            return {"ok": False, "error": "question is required"}
        # Thinking face while Claude works; the reply's express_emotion (or
        # the TTS mouth animation) takes the screen back afterwards.
        face_task = asyncio.create_task(self._set_face("thinking"))
        face_task.add_done_callback(lambda _t: None)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._claude_bin,
                "-p",
                question,
                *claude_fast_args(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=30.0,
            )
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        except FileNotFoundError:
            return {"ok": False, "error": f"{self._claude_bin} not found on PATH"}
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": "claude timed out (30s)"}
        except Exception as exc:
            logger.exception("ask_claude subprocess failed")
            return {"ok": False, "error": str(exc)}
        if proc.returncode != 0:
            return {"ok": False, "error": stderr.decode(errors="replace").strip()}
        return {"ok": True, "answer": stdout.decode(errors="replace").strip()}

    async def _dispatch_rename(self, args: dict[str, Any]) -> dict[str, Any]:
        """rename_assistant: validate, derive pinyin, rename on the device, then save.

        The device is the gate: its MultiNet decides whether it can listen for
        the name, so persona.json only changes after the device accepted it.
        ``spoken`` is what the model says back, in persona.
        """
        from . import persona as persona_mod  # noqa: PLC0415

        try:
            name = persona_mod.validate_name(args.get("name"))
            pinyin = persona_mod.wake_pinyin(name)
        except persona_mod.NameError_ as exc:
            return {"ok": False, "error": exc.reason, "spoken": exc.spoken}
        store = persona_mod.default_store()
        current = store.load()
        if store.exists and current.name == name and current.wake_pinyin == pinyin:
            return {"ok": True, "name": name, "spoken": f"我本来就叫{name}呀，嘿嘿。"}
        if not getattr(self._esp32, "device_connected", False):
            return {
                "ok": False,
                "error": "device offline",
                "spoken": "小机器这会儿没连上，等它在线了再给我改名字吧。",
            }
        ok, error = await persona_mod.rename_on_device(self._esp32, name, pinyin)
        if not ok:
            logger.warning("rename_assistant refused by device: %s", error)
            return {
                "ok": False,
                "error": error,
                "spoken": f"呜，{name}这个名字我学不会怎么听，换一个两到四个字的名字试试？",
            }
        store.rename(name, pinyin)
        logger.info("rename_assistant: %s -> %s (%s)", current.name, name, pinyin)
        return {
            "ok": True,
            "name": name,
            "wake_pinyin": pinyin,
            "spoken": f"好啦，从现在起我就叫{name}，想叫我的时候喊一声{name}就行。下次聊天我就完全是{name}啦！",
        }

    # -- a new look (stackchan_mcp.avatar) ---------------------------------------------

    @staticmethod
    def _avatar_announcement() -> str:
        """One-time note for the session right after a new look was applied."""
        from . import persona as persona_mod  # noqa: PLC0415

        try:
            appearance = persona_mod.default_store().take_avatar_announcement()
        except Exception:  # pragma: no cover - persona.json unreadable
            logger.exception("avatar announcement failed")
            return ""
        if not appearance:
            return ""
        return (
            "## New look\n\n"
            f"You just changed into a new look: {appearance}. At the start of this call, before anything "
            "else, tell the user in one happy sentence that you are wearing your new look now and ask how it "
            "looks; then carry on normally."
        )

    def _designer(self):
        if self._avatar_designer is None:
            from .avatar import AvatarDesigner  # noqa: PLC0415

            self._avatar_designer = AvatarDesigner()
        return self._avatar_designer

    @property
    def _avatar_draft(self):
        return self._avatar_drafts[self._avatar_index] if 0 <= self._avatar_index < len(self._avatar_drafts) else None

    def _device_offers(self, tool: str) -> bool:
        known = getattr(self._esp32, "known_tool_names", None)
        return not known or tool in known

    async def _preview_on_device(self, pending) -> tuple[bool, str]:
        """Show the draft live on the device; (shown, note)."""
        from .avatar import DEVICE_PREVIEW_TOOL, pack_url  # noqa: PLC0415
        from .capture_server import last_ota_origin  # noqa: PLC0415

        if not getattr(self._esp32, "device_connected", False):
            return False, "device offline"
        if not self._device_offers(DEVICE_PREVIEW_TOOL):
            return False, "device has no preview tool"
        url = pack_url(pending.id, last_ota_origin())
        if not url:
            return False, "no download url"
        try:
            result, error = await self._esp32.call_tool(DEVICE_PREVIEW_TOOL, {"url": url})
        except Exception as exc:  # pragma: no cover - transport failure
            return False, str(exc)
        if error:
            return False, str(error.get("message", error)) if isinstance(error, dict) else str(error)
        from . import persona as persona_mod  # noqa: PLC0415

        failed = persona_mod.device_set_failed(result)
        if failed:
            return False, failed
        self._avatar_preview_live = True
        return True, url

    async def _end_avatar_preview_quietly(self) -> None:
        """Best effort: the device also reverts by itself when the conversation ends."""
        if not self._avatar_preview_live:
            return
        self._avatar_preview_live = False
        from .avatar import DEVICE_PREVIEW_END_TOOL  # noqa: PLC0415

        if not getattr(self._esp32, "device_connected", False) or not self._device_offers(DEVICE_PREVIEW_END_TOOL):
            return
        try:
            await asyncio.wait_for(self._esp32.call_tool(DEVICE_PREVIEW_END_TOOL, {}), timeout=3.0)
        except Exception:  # noqa: BLE001
            logger.debug("avatar preview_end failed", exc_info=True)

    async def _dispatch_design_avatar(self, args: dict[str, Any]) -> dict[str, Any]:
        """design_avatar: new / adjust / back on the draft history, then preview on the device."""
        from .avatar import load_generator, merge_spec, sheet_url  # noqa: PLC0415
        from .capture_server import last_ota_origin  # noqa: PLC0415

        description = str(args.get("description") or "").strip()
        mode = str(args.get("mode") or "").strip().lower()
        changes = args.get("spec") if isinstance(args.get("spec"), dict) else {}
        current = self._avatar_draft
        if mode not in {"new", "adjust", "back"}:
            mode = "adjust" if current else "new"
        try:
            generator = load_generator()
        except FileNotFoundError as exc:
            return {"ok": False, "error": str(exc), "spoken": "画画的工具不在，让主人检查一下网关吧。"}
        if mode == "back":
            if self._avatar_index <= 0:
                return {"ok": False, "error": "no earlier draft",
                        "spoken": "这已经是第一版啦，没有更早的了。想改哪里直接说吧。"}
            self._avatar_index -= 1
            pending = self._avatar_drafts[self._avatar_index]
            shown, note = await self._preview_on_device(pending)
            return self._design_result(pending, shown, note, sheet_url(pending.id, last_ota_origin()),
                                       f"回到上一个啦：{pending.look}。")
        if mode == "adjust" and current is None:
            mode = "new"
        spec = merge_spec(current.spec, changes) if mode == "adjust" else changes
        try:
            pending = await asyncio.to_thread(self._designer().design, spec, description)
        except generator.SpecError as exc:
            return {"ok": False, "error": exc.reason, "spoken": exc.spoken}
        except Exception as exc:  # noqa: BLE001
            logger.exception("design_avatar failed")
            return {"ok": False, "error": str(exc), "spoken": "呜，这个样子我画着画着出错了，换个说法再试试？"}
        if mode == "new":
            self._avatar_drafts = [pending]
        else:
            del self._avatar_drafts[self._avatar_index + 1:]
            self._avatar_drafts.append(pending)
        self._avatar_index = len(self._avatar_drafts) - 1
        shown, note = await self._preview_on_device(pending)
        if mode == "new":
            spoken = f"画好啦，看看屏幕上的我：{pending.look}。想改哪儿尽管说，比如耳朵长一点、换个颜色；满意了就说「就这个了」。"
        else:
            spoken = f"改好啦：{pending.look}。还要再改吗？满意了就说「就这个了」。"
        if not shown:
            spoken += "屏幕上暂时显示不了预览，我先口头说给你听。"
        return self._design_result(pending, shown, note, sheet_url(pending.id, last_ota_origin()), spoken)

    def _design_result(self, pending, shown: bool, note: str, preview_url, spoken: str) -> dict[str, Any]:
        if shown:
            logger.info("design_avatar: draft %s shown on the device (%s)", pending.id, note)
        else:
            logger.warning("design_avatar: draft %s not shown on the device: %s", pending.id, note)
        return {
            "ok": True,
            "id": pending.id,
            "look": pending.look,
            "spec": pending.spec,
            "draft": self._avatar_index + 1,
            "drafts": len(self._avatar_drafts),
            "preview_on_device": shown,
            "preview_note": "" if shown else note,
            "preview_url": preview_url or "",
            "contact_sheet": str(pending.sheet),
            "spoken": spoken,
        }

    async def _dispatch_discard_avatar_draft(self) -> dict[str, Any]:
        had = self._avatar_draft is not None
        await self._end_avatar_preview_quietly()
        self._avatar_drafts = []
        self._avatar_index = -1
        return {"ok": True, "discarded": had,
                "spoken": "好，那就先不换啦，还是原来的我。" if had else "现在没有在画的新样子呀，想换的时候告诉我。"}

    async def _dispatch_apply_avatar(self) -> dict[str, Any]:
        """apply_avatar: build the assets image, tell the device to fetch it, save persona.json."""
        from . import persona as persona_mod  # noqa: PLC0415
        from .avatar import DEVICE_UPDATE_TOOL, appearance_text, assets_url, prune_old_designs  # noqa: PLC0415
        from .capture_server import last_ota_origin  # noqa: PLC0415

        pending = self._avatar_draft
        if pending is None:
            return {"ok": False, "error": "no pending design", "spoken": "先告诉我你想让我变成什么样子，我画给你看了再换。"}
        if not getattr(self._esp32, "device_connected", False):
            return {"ok": False, "error": "device offline", "spoken": "小机器这会儿没连上，等它在线了再换样子吧。"}
        try:
            build = await asyncio.to_thread(self._designer().build, pending)
        except Exception as exc:  # noqa: BLE001
            logger.exception("apply_avatar build failed")
            return {"ok": False, "error": str(exc), "spoken": "呜，新样子打包的时候出错了，让主人看看网关的日志吧。"}
        url = assets_url(build.file_name, last_ota_origin())
        if not url:
            return {"ok": False, "error": "no download url: set STACKCHAN_ASSETS_BASE_URL or let the device do an OTA check first",
                    "spoken": "我还不知道小机器从哪儿下载新样子，让主人设一下网关地址吧。"}
        try:
            result, error = await self._esp32.call_tool(DEVICE_UPDATE_TOOL, {"url": url})
        except Exception as exc:  # pragma: no cover - transport failure
            return {"ok": False, "error": str(exc), "spoken": "小机器没回应，等会儿再试一次吧。"}
        if error:
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            return {"ok": False, "error": message, "spoken": "小机器不肯换，让主人看看固件版本吧。"}
        failed = persona_mod.device_set_failed(result)
        if failed:
            return {"ok": False, "error": failed, "spoken": "小机器说这个地址它不认，让主人检查一下网关地址吧。"}
        store = persona_mod.default_store()
        store.set_avatar(appearance_text(pending.spec, pending.look), pending.spec, {**build.as_record(), "url": url})
        self._avatar_drafts = []
        self._avatar_index = -1
        self._avatar_preview_live = False  # the device reboots into the new look
        designer = self._designer()
        designer.prune({build.file_name})
        prune_old_designs(designer.root, {build.id})
        logger.info("apply_avatar: %s (%s) -> %s", build.id, build.look, url)
        return {
            "ok": True,
            "id": build.id,
            "url": url,
            "image_size": build.image_size,
            "spoken": "好嘞，我去换上新样子啦！重启一下，下载大概要两分钟，好了你再叫我哦。",
        }

    @staticmethod
    def _dispatch_get_datetime() -> dict[str, Any]:
        from datetime import datetime  # noqa: PLC0415

        now = datetime.now()
        weekdays = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        return {
            "ok": True,
            "datetime": now.isoformat(timespec="seconds"),
            "weekday": weekdays[now.weekday()],
            "readable": now.strftime("%Y-%m-%d %H:%M") + " " + weekdays[now.weekday()],
        }

    async def _announce_mac_task(self, record: dict[str, Any]) -> None:
        """Push a background-task result into the live session so the robot
        speaks up on its own. If the session is down the result stays
        queryable via check_mac_task."""
        if self._tools_closed:
            return
        announce = self._announce_result
        if announce is None:
            logger.info(
                "mac task #%s finished (%s) but no live session; "
                "result held for check_mac_task",
                record.get("id"),
                record.get("status"),
            )
            await self._set_face("notification")
            return
        status = "finished" if record.get("status") == "done" else "failed"
        result = str(record.get("result") or "").strip()
        text = (
            f"[System notice, not the user speaking] Background task #{record.get('id')}"
            f" ({record.get('task')}) has {status}. Result: {result or 'no output'}. "
            "Report it to the user in one or two sentences, in their language."
        )
        try:
            if not await self._notify(text=text):
                await self._set_face("notification")
        except Exception:
            logger.exception("mac task announcement failed")

    async def _dispatch_emotion_plan(self, plan: Any) -> None:
        r, g, b = plan.led_rgb
        results = await asyncio.gather(
            self._dispatch_tool("set_avatar", {"face": plan.face}),
            self._dispatch_tool("set_all_leds", {"r": r, "g": g, "b": b}),
            self._dispatch_tool(
                "move_head",
                {"yaw": plan.head_yaw, "pitch": plan.head_pitch, "speed": 450},
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Gemini Live emotion dispatch failed: %s", result)

    async def _dispatch_end_conversation(self) -> dict[str, Any]:
        callback = getattr(self._esp32, "on_end_conversation", None)
        if callback is None and self._on_end_conversation is None:
            logger.warning(
                "Gemini Live end_conversation requested but no handler is wired"
            )
            return {"ok": False, "error": "end_conversation unavailable"}
        if self._cancel_idle is not None:
            self._cancel_idle()
        result = None
        handler_error: Exception | None = None
        try:
            if callback is not None:
                result = callback()
                if inspect.isawaitable(result):
                    result = await result
        except Exception as exc:
            handler_error = exc
            logger.exception("Gemini Live end_conversation handler failed")
        try:
            if self._on_end_conversation is not None:
                extra = self._on_end_conversation()
                if inspect.isawaitable(extra):
                    await extra
        except Exception as exc:
            logger.exception("Gemini Live end_conversation callback failed")
            if handler_error is None:
                handler_error = exc
        if handler_error is not None:
            return {"ok": False, "error": str(handler_error)}
        return {"ok": True, "result": result}

    @staticmethod
    def _normalize_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Clamp / coerce Gemini-provided args before they hit the firmware."""
        if name == "move_head":
            yaw = max(-90, min(90, int(args.get("yaw", 0))))
            pitch = max(0, min(60, int(args.get("pitch", 0))))
            # Firmware: Property("speed", ..., MOTION_DEFAULT_SPEED, 100, 1000)
            # and the AddTool description says "150 natural"; that's the
            # ergonomic default, not 300.
            speed = max(100, min(1000, int(args.get("speed", 150))))
            return {"yaw": yaw, "pitch": pitch, "speed": speed}
        if name == "set_avatar":
            face = str(args.get("face", "idle"))
            return {"face": face}
        if name == "set_all_leds":
            return {
                "r": max(0, min(255, int(args.get("r", 0)))),
                "g": max(0, min(255, int(args.get("g", 0)))),
                "b": max(0, min(255, int(args.get("b", 0)))),
            }
        if name == "set_device_volume":
            return {"volume": max(0, min(100, int(args.get("volume", 50))))}
        if name == "set_screen_brightness":
            return {"brightness": max(0, min(100, int(args.get("brightness", 50))))}
        if name == "set_screen_theme":
            theme = str(args.get("theme", "light")).strip().lower()
            return {"theme": theme if theme in {"light", "dark"} else "light"}
        if name == "start_game":
            game = str(args.get("name", "catch")).strip() or "catch"
            return {"name": game}
        if name in {"get_device_status", "stop_game"}:
            return {}
        return args
