"""Lightweight polling bridge for StackChan head-touch events."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TOUCH_TOOL_NAME = "self.touch.get_touch_state"
DEFAULT_HOOK_STATE_DIR = Path(os.getenv("STACKCHAN_HOOK_STATE_DIR", "~/.stackchan/hooks")).expanduser()
DEFAULT_TOUCH_PAUSE_PATH = DEFAULT_HOOK_STATE_DIR / "touch-pause-until.json"

TouchListener = Callable[["TouchEvent"], None | Awaitable[None]]


@dataclass(frozen=True)
class TouchEvent:
    """A debounced StackChan head-touch gesture detected from firmware state."""

    event: str
    age_ms: int
    event_time_s: float
    detected_at_s: float
    zone0: bool = False
    zone1: bool = False
    zone2: bool = False
    pressed: bool = False
    raw: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-safe notification payload for MCP logging."""
        payload: dict[str, Any] = {
            "type": "stackchan.touch",
            "event": self.event,
            "age_ms": self.age_ms,
            "zones": [self.zone0, self.zone1, self.zone2],
            "pressed": self.pressed,
            "detected_at_s": self.detected_at_s,
        }
        if self.raw is not None:
            payload["raw"] = self.raw
        return payload


class TouchEventBridge:
    """Poll get_touch_state and notify listeners when a new gesture appears."""

    def __init__(
        self,
        esp32: Any,
        *,
        poll_interval_s: float | None = None,
        max_event_age_ms: int | None = None,
        duplicate_window_s: float = 0.5,
        enable_default_actions: bool = True,
        action_pause_path: Path | None = None,
        action_hold_s: float = 3.0,
        on_touch_activity: Callable[[], None] | None = None,
    ) -> None:
        self._esp32 = esp32
        self.poll_interval_s = (
            poll_interval_s
            if poll_interval_s is not None
            else float(os.getenv("STACKCHAN_TOUCH_POLL_INTERVAL_S", "0.5"))
        )
        self.max_event_age_ms = (
            max_event_age_ms
            if max_event_age_ms is not None
            else int(os.getenv("STACKCHAN_TOUCH_EVENT_MAX_AGE_MS", "2000"))
        )
        self.duplicate_window_s = duplicate_window_s
        self.enable_default_actions = enable_default_actions
        self.action_pause_path = action_pause_path or DEFAULT_TOUCH_PAUSE_PATH
        self.action_hold_s = action_hold_s
        self.on_touch_activity = on_touch_activity
        self._listeners: list[TouchListener] = []
        self._task: asyncio.Task[None] | None = None
        self._last_emitted: tuple[str, float] | None = None

    def add_listener(self, listener: TouchListener) -> Callable[[], None]:
        """Register a listener and return an unregister function."""
        self._listeners.append(listener)

        def unregister() -> None:
            with suppress(ValueError):
                self._listeners.remove(listener)

        return unregister

    async def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="stackchan-touch-bridge")

    async def stop(self) -> None:
        """Stop the background polling task."""
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def poll_once(self, *, now_s: float | None = None) -> TouchEvent | None:
        """Poll the firmware once and emit a listener notification if needed."""
        if (
            not self._listeners
            and not self.enable_default_actions
            and self.on_touch_activity is None
        ):
            return None
        if not getattr(self._esp32, "device_connected", False):
            return None
        has_tool = getattr(self._esp32, "has_tool", None)
        if callable(has_tool) and not has_tool(TOUCH_TOOL_NAME):
            return None  # upstream xiaozhi-esp32 boards: no touch tool, nothing to poll

        try:
            result, error = await self._esp32.call_tool(TOUCH_TOOL_NAME, {})
        except Exception as exc:  # pragma: no cover - defensive bridge guard
            logger.debug("touch poll failed: %s", exc)
            return None
        if error:
            logger.debug("touch poll returned error: %s", error)
            return None

        now = time.monotonic() if now_s is None else now_s
        state = _extract_touch_state(result)
        if state is None or not state.get("available", True):
            return None
        if self._state_has_touch_activity(state) and self.on_touch_activity is not None:
            self.on_touch_activity()
        event = self._event_from_state(state, now_s=now)
        if event is None or not self._is_new_event(event):
            return None

        self._last_emitted = (event.event, event.event_time_s)
        if self.enable_default_actions:
            await self._perform_default_action(event)
        await self._emit(event)
        return event

    async def _run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.poll_interval_s)

    def _event_from_result(self, result: Any, *, now_s: float) -> TouchEvent | None:
        state = _extract_touch_state(result)
        if not state or not state.get("available", True):
            return None
        return self._event_from_state(state, now_s=now_s)

    def _event_from_state(self, state: dict[str, Any], *, now_s: float) -> TouchEvent | None:
        event_name = str(state.get("last_event", "idle")).lower()
        if event_name not in {"tap", "stroke"}:
            return None

        age_raw = state.get("last_event_age_ms", -1)
        try:
            age_ms = int(age_raw)
        except (TypeError, ValueError):
            return None
        if age_ms < 0 or age_ms > self.max_event_age_ms:
            return None

        return TouchEvent(
            event=event_name,
            age_ms=age_ms,
            event_time_s=now_s - (age_ms / 1000.0),
            detected_at_s=now_s,
            zone0=bool(state.get("zone0", False)),
            zone1=bool(state.get("zone1", False)),
            zone2=bool(state.get("zone2", False)),
            pressed=bool(state.get("pressed", False)),
            raw=_optional_int(state.get("raw")),
        )

    def _state_has_touch_activity(self, state: dict[str, Any]) -> bool:
        if bool(state.get("pressed", False)):
            return True
        if any(bool(state.get(zone, False)) for zone in ("zone0", "zone1", "zone2")):
            return True
        age_raw = state.get("last_event_age_ms", -1)
        try:
            age_ms = int(age_raw)
        except (TypeError, ValueError):
            return False
        event_name = str(state.get("last_event", "idle")).lower()
        return event_name in {"tap", "stroke"} and 0 <= age_ms <= self.max_event_age_ms

    def _is_new_event(self, event: TouchEvent) -> bool:
        if self._last_emitted is None:
            return True
        last_name, last_event_time_s = self._last_emitted
        if event.event != last_name:
            return True
        return abs(event.event_time_s - last_event_time_s) > self.duplicate_window_s

    async def _emit(self, event: TouchEvent) -> None:
        for listener in list(self._listeners):
            try:
                maybe_awaitable = listener(event)
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
            except Exception as exc:  # pragma: no cover - listener isolation
                logger.debug("touch listener failed: %s", exc)

    async def _perform_default_action(self, event: TouchEvent) -> None:
        """Hold the hook off the head; firmware owns the touch reaction.

        Firmware ``HandleTap`` / ``HandleHeadStrokeReaction`` already runs
        ``SetAvatarExpressionIfActive("happy")`` and ``PerformHeadPetMotion``
        inside the touch poll. The gateway used to repeat ``set_avatar`` +
        ``set_head_angles`` here, which was a textbook "two action layers
        racing". We keep only
        the pause marker so the hook bridge doesn't talk over the firmware
        reaction for ``action_hold_s`` seconds.
        """
        if event.event != "tap":
            return
        self._write_touch_pause(event)
        _stop_hook_motion(self.action_pause_path.parent)

    def _write_touch_pause(self, event: TouchEvent) -> None:
        try:
            self.action_pause_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "event": event.event,
                "until": time.time() + self.action_hold_s,
                "detected_at_s": event.detected_at_s,
            }
            tmp = self.action_pause_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.action_pause_path)
        except Exception as exc:  # pragma: no cover - fail open
            logger.debug("touch pause marker write failed: %s", exc)


def _extract_touch_state(result: Any) -> dict[str, Any] | None:
    """Extract firmware touch state from raw or MCP content-wrapped results."""
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parsed = _parse_json_object(item.get("text"))
                    if parsed is not None:
                        return parsed
            return None
        return result

    return _parse_json_object(result)


def _parse_json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stop_hook_motion(state_dir: Path) -> None:
    """Stop the hook motion worker if it is currently running."""
    pid_path = state_dir / "active-motion.pid"
    try:
        pid_text = pid_path.read_text(encoding="utf-8").strip()
        pid_path.unlink(missing_ok=True)
        if not pid_text:
            return
        os.killpg(int(pid_text), 15)
    except ProcessLookupError:
        return
    except Exception:
        return
