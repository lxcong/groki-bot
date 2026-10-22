# SPDX-License-Identifier: MIT
"""Renaming the assistant from a conversation: persona.json, the device and the voice persona."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp import persona as persona_mod
from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge
from stackchan_mcp.personality import load_personality_file
from stackchan_mcp.voice_tools import VoiceToolExecutor


def _device_text(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}]}


@pytest.fixture
def store(monkeypatch, tmp_path):
    path = tmp_path / "persona.json"
    monkeypatch.setenv("STACKCHAN_PERSONA_FILE", str(path))
    monkeypatch.setattr(persona_mod, "_default_store", None)
    return persona_mod.default_store()


@pytest.fixture(autouse=True)
def device_tools(monkeypatch):
    monkeypatch.setenv("STACKCHAN_GEMINI_DEVICE_TOOLS", "1")
    monkeypatch.setenv("STACKCHAN_MAC_CONTROL", "0")
    monkeypatch.setenv("STACKCHAN_ASK_CLAUDE", "0")
    monkeypatch.delenv("STACKCHAN_TOOL_BOT", raising=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("小乐", "小乐"), (" “阿福” ", "阿福"), ("「多多」", "多多"), ("小不点儿", "小不点儿")],
)
def test_validate_name_accepts_chinese_names(raw, expected):
    assert persona_mod.validate_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "Bob", "乐", "小乐小乐小", "小 乐", "小乐2"])
def test_validate_name_rejects_what_multinet_cannot_hear(raw):
    with pytest.raises(persona_mod.NameError_) as info:
        persona_mod.validate_name(raw)
    assert info.value.spoken


@pytest.mark.parametrize(("name", "pinyin"), [("小乐", "xiao le"), ("阿福", "a fu"), ("团团", "tuan tuan")])
def test_wake_pinyin_is_one_toneless_syllable_per_character(name, pinyin):
    assert persona_mod.wake_pinyin(name) == pinyin


def test_store_defaults_roundtrip_and_rename(store):
    assert not store.exists
    assert store.load() == persona_mod.Persona()
    saved = store.rename("阿福", "a fu")
    assert store.exists
    assert saved.name == "阿福" and saved.wake_word == "阿福" and saved.wake_pinyin == "a fu"
    assert saved.appearance == persona_mod.DEFAULT_APPEARANCE
    again = persona_mod.PersonaStore(store.path).load()
    assert again.name == "阿福" and again.updated_at > 0


def test_personality_template_renders_current_persona(store, monkeypatch, tmp_path):
    template = tmp_path / "persona.md"
    template.write_text("# {{name}}\n你是{{name}}，{{appearance}}。叫一声\"{{wake_word}}\"", encoding="utf-8")
    monkeypatch.setenv("STACKCHAN_PERSONALITY_FILE", str(template))
    assert load_personality_file().startswith("# 小乐\n你是小乐，一只")
    store.rename("阿福", "a fu")
    rendered = load_personality_file()
    assert "阿福" in rendered and "{{" not in rendered and "小乐" not in rendered


def test_grok_agent_name_follows_persona_json(store, monkeypatch):
    monkeypatch.setenv("STACKCHAN_GBOT_VOICE_AGENT_NAME", "小乐")
    bridge = GbotVoiceBridge(
        on_audio=AsyncMock(), on_turn_complete=AsyncMock(), on_interrupt=AsyncMock(), on_session_dead=AsyncMock()
    )
    assert bridge.agent_name == "小乐"
    store.rename("阿福", "a fu")
    assert bridge.agent_name == "阿福"
    assert bridge._session_request()["agentName"] == "阿福"
    fixed = GbotVoiceBridge(
        on_audio=AsyncMock(), on_turn_complete=AsyncMock(), on_interrupt=AsyncMock(), on_session_dead=AsyncMock(),
        agent_name="Groki",
    )
    assert fixed.agent_name == "Groki"


@pytest.mark.asyncio
async def test_rename_tool_renames_device_first_then_saves(store):
    device = SimpleNamespace(
        device_connected=True,
        call_tool=AsyncMock(return_value=(_device_text({"ok": True, "name": "阿福"}), None)),
    )
    executor = VoiceToolExecutor(device, mac_controller=SimpleNamespace())
    try:
        assert "rename_assistant" in {d["name"] for d in executor.declarations()}
        assert "## Your name" in executor.instructions()
        result = await executor.execute("rename_assistant", {"name": "阿福"})
        assert result["ok"] and "阿福" in result["spoken"]
        device.call_tool.assert_awaited_once_with("self.persona.set_name", {"name": "阿福", "wake_pinyin": "a fu"})
        assert store.load().name == "阿福" and store.load().wake_pinyin == "a fu"

        same = await executor.execute("rename_assistant", {"name": "阿福"})
        assert same["ok"] and device.call_tool.await_count == 1
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_rename_tool_keeps_persona_when_device_refuses_or_name_is_bad(store):
    device = SimpleNamespace(
        device_connected=True,
        call_tool=AsyncMock(return_value=(_device_text("failed: MultiNet rejected: xx"), None)),
    )
    executor = VoiceToolExecutor(device, mac_controller=SimpleNamespace())
    try:
        bad = await executor.execute("rename_assistant", {"name": "Bob"})
        assert not bad["ok"] and bad["spoken"] and device.call_tool.await_count == 0
        refused = await executor.execute("rename_assistant", {"name": "阿福"})
        assert not refused["ok"] and "换一个" in refused["spoken"]
        assert not store.exists
        device.device_connected = False
        offline = await executor.execute("rename_assistant", {"name": "阿福"})
        assert not offline["ok"] and offline["error"] == "device offline"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_sync_adopts_device_name_until_the_user_saved_one(store):
    device = SimpleNamespace(
        call_tool=AsyncMock(return_value=(_device_text({"name": "小乐", "wake_word": "小乐", "wake_pinyin": "xiao le"}), None))
    )
    assert await persona_mod.sync_device_persona(device, store) == "adopted"
    assert store.exists and store.load().name == "小乐"
    device.call_tool.assert_awaited_once_with("self.persona.get", {})


@pytest.mark.asyncio
async def test_sync_pushes_persona_json_to_a_device_with_another_name(store):
    store.rename("阿福", "a fu")
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        if name == "self.persona.get":
            return _device_text({"name": "小乐", "wake_pinyin": "xiao le"}), None
        return _device_text({"ok": True}), None

    device = SimpleNamespace(call_tool=call_tool)
    assert await persona_mod.sync_device_persona(device, store) == "pushed"
    assert calls[1] == ("self.persona.set_name", {"name": "阿福", "wake_pinyin": "a fu"})

    calls.clear()
    device.call_tool = AsyncMock(return_value=(_device_text({"name": "阿福", "wake_pinyin": "a fu"}), None))
    assert await persona_mod.sync_device_persona(device, store) == "in_sync"
    device.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_skips_devices_without_persona_tools(store):
    device = SimpleNamespace(call_tool=AsyncMock(return_value=(None, {"code": -32601, "message": "unknown tool"})))
    assert await persona_mod.sync_device_persona(device, store) == "skipped"
    assert not store.exists
