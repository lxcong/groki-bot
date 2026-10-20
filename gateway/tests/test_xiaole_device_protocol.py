"""Gaps between the gateway and upstream xiaozhi-esp32 firmware, found with the Xiaole board."""

import json

import pytest

from stackchan_mcp.esp32_client import ESP32Connection
from stackchan_mcp.gemini_voice_proxy import device_sentence_message, device_stt_message
from stackchan_mcp.touch_bridge import TOUCH_TOOL_NAME, TouchEventBridge


class FakeWs:
    def __init__(self) -> None:
        self.closed_with: tuple[int, str] | None = None

    async def close(self, code=1000, reason=""):
        self.closed_with = (code, reason)


def test_connection_has_tool_defaults_to_true_until_the_list_is_known():
    connection = ESP32Connection(FakeWs(), "s1")
    assert connection.has_tool("self.touch.get_touch_state")
    connection.tools = [{"name": "self.get_device_status"}, {"name": "self.audio_speaker.set_volume"}]
    assert connection.has_tool("self.get_device_status")
    assert not connection.has_tool("self.touch.get_touch_state")


@pytest.mark.asyncio
async def test_connection_close_ws_closes_the_device_socket_once():
    ws = FakeWs()
    connection = ESP32Connection(ws, "s1")
    await connection.close_ws("conversation ended")
    assert ws.closed_with == (1000, "conversation ended")
    connection.disconnect()
    ws.closed_with = None
    await connection.close_ws()
    assert ws.closed_with is None


class FakeEsp32:
    device_connected = True

    def __init__(self, offered: bool) -> None:
        self.offered = offered
        self.calls = 0

    def has_tool(self, name: str) -> bool:
        return self.offered

    async def call_tool(self, name, arguments):
        self.calls += 1
        return {"content": [{"type": "text", "text": json.dumps({"available": False})}]}, None


@pytest.mark.asyncio
async def test_touch_bridge_skips_devices_without_the_touch_tool():
    esp32 = FakeEsp32(offered=False)
    bridge = TouchEventBridge(esp32, poll_interval_s=0.01)
    bridge.add_listener(lambda event: None)
    assert await bridge.poll_once() is None
    assert esp32.calls == 0
    esp32.offered = True
    await bridge.poll_once()
    assert esp32.calls == 1


def test_device_subtitle_messages_follow_the_xiaozhi_protocol():
    assert device_stt_message("查一下天气") == {"type": "stt", "text": "查一下天气"}
    assert device_sentence_message("好的") == {"type": "tts", "state": "sentence_start", "text": "好的"}
    assert TOUCH_TOOL_NAME == "self.touch.get_touch_state"
