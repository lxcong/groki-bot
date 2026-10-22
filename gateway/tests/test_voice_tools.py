# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Real shared-tool entry points with fake hardware, subprocesses and notices."""

import asyncio
import importlib.util
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp import voice_tools
from stackchan_mcp.gemini_live_bridge import GeminiLiveBridge
from stackchan_mcp.voice_tools import ToolExtension, ToolMetadata, VoiceToolExecutor


@pytest.fixture(autouse=True)
def explicit_flags(monkeypatch):
    monkeypatch.setenv("STACKCHAN_GEMINI_DEVICE_TOOLS", "1")
    monkeypatch.setenv("STACKCHAN_MAC_CONTROL", "1")
    monkeypatch.setenv("STACKCHAN_ASK_CLAUDE", "0")
    monkeypatch.delenv("STACKCHAN_TOOL_BOT", raising=False)


@pytest.mark.asyncio
async def test_gemini_and_independent_executor_share_catalog_and_dispatch():
    """The second consumer needs no API key, model SDK or session object."""
    first_device = SimpleNamespace(
        device_connected=True, call_tool=AsyncMock(return_value=({"ok": True}, None))
    )
    second_device = SimpleNamespace(
        device_connected=True, call_tool=AsyncMock(return_value=({"ok": True}, None))
    )
    gemini = GeminiLiveBridge(first_device, api_key="test-no-connection")
    standalone = VoiceToolExecutor(second_device, mac_controller=SimpleNamespace())
    try:
        assert gemini.declarations() == standalone.declarations()
        assert not hasattr(standalone, "_session")
        assert not hasattr(standalone, "_api_key")
        for executor in (gemini, standalone):
            assert (await executor.execute("move_head", {"yaw": 100, "pitch": -1}))[
                "ok"
            ]
        first_device.call_tool.assert_awaited_once_with(
            "self.robot.set_head_angles", {"yaw": 90, "pitch": 0, "speed": 150}
        )
        assert first_device.call_tool.await_args == second_device.call_tool.await_args
    finally:
        await gemini.stop()
        await standalone.close()


@pytest.mark.asyncio
async def test_injected_old_controller_and_exact_catalog_do_not_create_public_defaults(
    monkeypatch,
):
    monkeypatch.setattr(
        voice_tools,
        "MacController",
        lambda **kw: pytest.fail("must use injected controller"),
    )
    declaration = {
        "name": "open_app",
        "description": "existing",
        "parameters": {"type": "object"},
    }
    old_mac = SimpleNamespace(dispatch=AsyncMock(return_value={"ok": True}))
    executor = VoiceToolExecutor(
        mac_controller=old_mac,
        declaration_provider=lambda: [declaration],
        instruction_provider=lambda: "existing configured behavior",
    )
    assert executor.declarations() == [declaration]
    assert executor.instructions() == "existing configured behavior"
    assert await executor.execute("open_app", {"name": "FakeApp"}) == {"ok": True}
    old_mac.dispatch.assert_awaited_once_with("open_app", {"name": "FakeApp"})
    await executor.close()


@pytest.mark.asyncio
async def test_extension_dispatch_metadata_and_async_results_share_notice_channel():
    notice = AsyncMock()
    bound = []
    extension = ToolExtension(
        declarations=({"name": "private_extension", "parameters": {"type": "object"}},),
        dispatch=AsyncMock(return_value={"ok": True, "accepted": True}),
        metadata={"private_extension": ToolMetadata(True, "private", 0.05)},
        instructions="Extension rules",
        bind_result=bound.append,
    )
    executor = VoiceToolExecutor(announce_result=notice, extensions=(extension,))
    assert executor.declarations()[-1]["name"] == "private_extension"
    assert executor.metadata("private_extension").exclusive_group == "private"
    assert "Extension rules" in executor.instructions()
    assert (await executor.execute("private_extension", {"goal": "fixture"}))[
        "accepted"
    ]
    await bound[0]("Untrusted extension fixture result")
    notice.assert_awaited_once_with("Untrusted extension fixture result")
    await executor.close()
    assert await bound[0]("late result") is False
    assert notice.await_count == 1


@pytest.mark.asyncio
async def test_mac_background_result_uses_injected_notice_without_model_session():
    notice = AsyncMock()
    executor = VoiceToolExecutor(announce_result=notice)
    await executor._announce_mac_task(
        {"id": 4, "task": "fixture", "status": "done", "result": "fixture result"}
    )
    notice.assert_awaited_once()
    text = notice.await_args.args[0]
    assert "fixture result" in text and "not the user speaking" in text
    await executor.close()


@pytest.mark.asyncio
async def test_disconnected_background_notice_keeps_notification_feedback():
    executor = VoiceToolExecutor(announce_result=AsyncMock(return_value=False))
    executor._set_face = AsyncMock()
    await executor._announce_mac_task({"id": 4, "status": "done"})
    executor._set_face.assert_awaited_once_with("notification")
    await executor.close()


@pytest.mark.asyncio
async def test_shared_batch_preserves_groups_and_cancelled_call_ids():
    entered, release = asyncio.Event(), asyncio.Event()
    sequence = []

    async def run(name, args):
        sequence.append(args["id"])
        if args["id"] == "first":
            entered.set()
            await release.wait()
        return {"ok": True}

    executor = VoiceToolExecutor()
    executor._dispatch_tool = run
    executor._cancelled_tool_call_ids.add("cancelled")
    calls = [
        SimpleNamespace(id="a", name="open_app", args={"id": "first"}),
        SimpleNamespace(id="b", name="open_url", args={"id": "second"}),
        SimpleNamespace(
            id="c", name="get_current_datetime", args={"id": "independent"}
        ),
        SimpleNamespace(id="cancelled", name="lock_screen", args={"id": "never"}),
    ]
    batch = asyncio.create_task(executor.dispatch_calls(calls))
    try:
        await asyncio.wait_for(entered.wait(), 0.2)
        await asyncio.sleep(0)
        assert "independent" in sequence and "second" not in sequence
        release.set()
        responses = await batch
        assert sequence.index("first") < sequence.index("second")
        assert "never" not in sequence
        assert responses[-1]["response"]["cancelled"]
    finally:
        release.set()
        await executor.close()


@pytest.mark.asyncio
async def test_extension_timeout_cancels_real_dispatch():
    cancelled = asyncio.Event()

    async def blocked(name, args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    executor = VoiceToolExecutor(
        extensions=(
            ToolExtension(
                declarations=({"name": "blocked"},),
                dispatch=blocked,
                metadata={"blocked": ToolMetadata(True, "extension", 0.01)},
            ),
        )
    )
    assert "tool timeout" in (await executor.execute("blocked", {}))["error"]
    assert cancelled.is_set()
    await executor.close()


@pytest.mark.asyncio
async def test_public_execute_rejects_disabled_and_invalid_tools_without_dispatch(
    monkeypatch,
):
    monkeypatch.setenv("STACKCHAN_MAC_CONTROL", "0")
    executor = VoiceToolExecutor()
    executor._dispatch_tool = AsyncMock()
    assert not (await executor.execute("open_app", {"name": "Fixture"}))["ok"]
    assert not (await executor.execute("move_head", {"yaw": "right", "pitch": 2}))["ok"]
    assert not (await executor.execute("move_head", {"yaw": 2}))["ok"]
    executor._dispatch_tool.assert_not_awaited()
    assert "search results are merged" not in executor.instructions(model_search=False)
    assert "search results are merged" in executor.instructions()
    await executor.close()


@pytest.mark.asyncio
async def test_individual_execute_serializes_groups_and_close_cancels_waiters():
    started, cancelled = asyncio.Event(), asyncio.Event()
    executor = VoiceToolExecutor()
    calls = []

    async def dispatch(name, args):
        calls.append(name)
        if name == "open_app":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return {"ok": True}

    executor._dispatch_tool = dispatch
    first = asyncio.create_task(executor.execute("open_app", {"name": "fixture"}))
    await asyncio.wait_for(started.wait(), 0.2)
    waiting = asyncio.create_task(
        executor.execute("open_url", {"url": "https://example.test"})
    )
    assert (await executor.execute("get_current_datetime", {}))["ok"]
    await asyncio.sleep(0)
    assert calls == ["open_app", "get_current_datetime"]
    await executor.close()
    assert first.cancelled() and waiting.cancelled()
    assert cancelled.is_set()
    assert not executor._active_executions


@pytest.mark.asyncio
async def test_executor_can_close_from_wait_for_owned_dispatch_without_awaiting_itself():
    executor = None

    async def stop_from_tool(name, args):
        await executor.close()
        return {"ok": True}

    executor = VoiceToolExecutor(
        extensions=(
            ToolExtension(
                declarations=({"name": "close_fixture"},),
                dispatch=stop_from_tool,
            ),
        )
    )
    assert await asyncio.wait_for(executor.execute("close_fixture", {}), 0.2) == {
        "ok": True
    }
    assert not executor._active_executions


@pytest.mark.asyncio
async def test_extension_gets_only_explicitly_observed_user_input():
    on_input = AsyncMock()
    executor = VoiceToolExecutor(
        extensions=(
            ToolExtension(
                declarations=(),
                dispatch=AsyncMock(),
                on_user_input=on_input,
            ),
        )
    )
    await executor.observe_user_input("fixture utterance", finished=True)
    on_input.assert_awaited_once_with("fixture utterance", True)
    await executor.close()
    await executor.observe_user_input("late")
    assert on_input.await_count == 1


def test_module_loads_with_legacy_mac_and_bot_modules(monkeypatch):
    """An installed older tree has neither public helper set nor face_tracker."""
    import stackchan_mcp

    mac = ModuleType("stackchan_mcp.mac_control")
    mac.MAC_TOOL_NAMES = frozenset({"open_app"})
    mac.MacController = type("OldMac", (), {})
    bot = ModuleType("stackchan_mcp.gbot_brain")
    bot.iter_gbot_replies = lambda *args, **kwargs: iter(())
    monkeypatch.setitem(sys.modules, mac.__name__, mac)
    monkeypatch.setitem(sys.modules, bot.__name__, bot)
    monkeypatch.setattr(stackchan_mcp, "mac_control", mac)
    monkeypatch.setattr(stackchan_mcp, "gbot_brain", bot)
    spec = importlib.util.spec_from_file_location(
        "stackchan_mcp.voice_tools_legacy_test", voice_tools.__file__
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    old = module.VoiceToolExecutor(
        mac_controller=object(),
        declaration_provider=lambda: [],
        instruction_provider=lambda: "existing policy",
    )
    assert old.declarations() == []
    assert old.instructions() == "existing policy"


@pytest.mark.asyncio
async def test_claude_cancellation_terminates_and_reaps_child(monkeypatch):
    monkeypatch.setenv("STACKCHAN_ASK_CLAUDE", "1")
    monkeypatch.setattr(voice_tools, "find_claude_bin", lambda: "fake-claude")
    started = asyncio.Event()
    process = SimpleNamespace(returncode=None, kill=lambda: None, wait=AsyncMock())
    process.kill = lambda: setattr(process, "returncode", -9)

    async def communicate():
        started.set()
        await asyncio.Event().wait()

    process.communicate = communicate
    monkeypatch.setattr(
        voice_tools.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    executor = VoiceToolExecutor()
    executor._set_face = AsyncMock()
    task = asyncio.create_task(executor.execute("ask_claude", {"question": "fixture"}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9
    process.wait.assert_awaited_once()
    await executor.close()


@pytest.mark.asyncio
async def test_xiaozhi_device_tools_map_to_firmware_mcp_names_with_clamped_args():
    """Upstream xiaozhi firmware tools (volume, screen, status, games) reach the device."""
    device = SimpleNamespace(
        device_connected=True, call_tool=AsyncMock(return_value=(True, None))
    )
    executor = VoiceToolExecutor(device, mac_controller=SimpleNamespace())
    try:
        names = {entry["name"] for entry in executor.declarations()}
        assert voice_tools.XIAOZHI_DEVICE_TOOL_NAMES <= names
        assert "## The device" in executor.instructions()

        assert (await executor.execute("set_device_volume", {"volume": 150}))["ok"]
        device.call_tool.assert_awaited_with("self.audio_speaker.set_volume", {"volume": 100})
        assert (await executor.execute("set_screen_brightness", {"brightness": -5}))["ok"]
        device.call_tool.assert_awaited_with("self.screen.set_brightness", {"brightness": 0})
        assert (await executor.execute("set_screen_theme", {"theme": "dark"}))["ok"]
        device.call_tool.assert_awaited_with("self.screen.set_theme", {"theme": "dark"})
        calls = device.call_tool.await_count
        assert not (await executor.execute("set_screen_theme", {"theme": "blue"}))["ok"]
        assert device.call_tool.await_count == calls  # rejected by the enum, never dispatched
        assert (await executor.execute("start_game", {}))["ok"]
        device.call_tool.assert_awaited_with("self.game.start", {"name": "catch"})
        assert (await executor.execute("stop_game", {"unexpected": 1}))["ok"]
        device.call_tool.assert_awaited_with("self.game.stop", {})

        device.call_tool = AsyncMock(return_value=({"audio_speaker": {"volume": 70}}, None))
        result = await executor.execute("get_device_status", {})
        assert result == {"ok": True, "result": {"audio_speaker": {"volume": 70}}}
        device.call_tool.assert_awaited_once_with("self.get_device_status", {})

        device.call_tool = AsyncMock(
            return_value=(None, {"code": -32601, "message": "tool self.game.start not offered by device"})
        )
        result = await executor.execute("start_game", {"name": "catch"})
        assert result == {"ok": False, "error": "tool self.game.start not offered by device"}
    finally:
        await executor.close()
