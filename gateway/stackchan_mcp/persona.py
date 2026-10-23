# SPDX-License-Identifier: MIT
"""The assistant's persona the user can change from a conversation.

``persona.json`` (``STACKCHAN_PERSONA_FILE``, else next to the gateway
package) is the single source of truth for the name the voice backends speak
as and the wake word the device listens for. The device keeps the same values
in its NVS (MCP tools ``self.persona.get`` / ``self.persona.set_name``) and
:func:`sync_device_persona` reconciles the two when a device connects.

The wake word is a MultiNet command word, so the name has to be Chinese and
is matched by its pinyin ("小乐" -> "xiao le"). :func:`wake_pinyin` derives the
pinyin with pypinyin; the device has the final say (its speech model rejects
phrases it cannot parse) and the gateway only stores a name the device took.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_NAME = "小乐"
DEFAULT_WAKE_PINYIN = "xiao le"
DEFAULT_APPEARANCE = "一只住在主人桌上的像素小白兔：长长的耳朵、圆圆的大眼睛、脖子上围着一条浅绿色的小围巾"

MIN_NAME_CHARS = 2
MAX_NAME_CHARS = 4

DEVICE_GET_TOOL = "self.persona.get"
DEVICE_SET_TOOL = "self.persona.set_name"

_CJK = re.compile(r"^[一-鿿]+$")
_PINYIN = re.compile(r"^[a-z]{1,6}( [a-z]{1,6}){0,5}$")


@dataclass(frozen=True)
class Persona:
    name: str = DEFAULT_NAME
    wake_word: str = DEFAULT_NAME
    wake_pinyin: str = DEFAULT_WAKE_PINYIN
    appearance: str = DEFAULT_APPEARANCE
    # The look the user designed in a conversation (stackchan_mcp.avatar): the
    # normalised spec, the built pack/image record, and whether the next call
    # should mention the new look once.
    avatar_spec: dict[str, Any] | None = field(default=None)
    avatar_pack: dict[str, Any] | None = field(default=None)
    avatar_announce: bool = False
    updated_at: float = 0.0

    def render(self, template: str) -> str:
        """Fill ``{{name}}`` / ``{{wake_word}}`` / ``{{appearance}}`` in a persona text."""
        return (
            template.replace("{{name}}", self.name)
            .replace("{{wake_word}}", self.wake_word)
            .replace("{{appearance}}", self.appearance)
        )


class NameError_(ValueError):
    """A name the wake word engine cannot take; ``spoken`` is the reply to say."""

    def __init__(self, reason: str, spoken: str):
        super().__init__(reason)
        self.reason = reason
        self.spoken = spoken


def normalize_name(raw: object) -> str:
    text = str(raw or "").strip()
    for quote in "\"'“”‘’「」『』《》":
        text = text.replace(quote, "")
    return text.strip()


def validate_name(raw: object) -> str:
    """Return the cleaned name or raise :class:`NameError_`."""
    name = normalize_name(raw)
    if not name:
        raise NameError_("empty name", "你想叫我什么呀？说一个两到四个字的中文名字吧。")
    if not _CJK.match(name):
        raise NameError_(
            "name must be Chinese characters",
            "这个名字我还念不准呢，换一个中文名字好不好？两到四个字的那种。",
        )
    if not MIN_NAME_CHARS <= len(name) <= MAX_NAME_CHARS:
        raise NameError_(
            f"name must be {MIN_NAME_CHARS} to {MAX_NAME_CHARS} characters",
            "名字太长或者太短啦，两到四个字我听得最清楚，再想一个呗？",
        )
    return name


def wake_pinyin(name: str) -> str:
    """Space-separated toneless pinyin MultiNet matches, e.g. 小乐 -> "xiao le"."""
    try:
        from pypinyin import Style, lazy_pinyin  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise NameError_(
            "pypinyin is not installed",
            "我这边少了一个拼音工具，暂时改不了名字，让主人检查一下网关吧。",
        ) from exc
    syllables = lazy_pinyin(name, style=Style.NORMAL, errors="ignore")
    pinyin = " ".join(s.strip().lower() for s in syllables if s.strip())
    if not _PINYIN.match(pinyin) or len(pinyin.split()) != len(name):
        raise NameError_(
            f"could not derive pinyin for {name!r}",
            "这个名字的读音我拿不准，换一个常见一点的字吧？",
        )
    return pinyin


def persona_path() -> Path:
    raw = os.getenv("STACKCHAN_PERSONA_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[1] / "persona.json"


class PersonaStore:
    """Reads and writes persona.json; ``exists`` tells whether a user ever saved one."""

    def __init__(self, path: Path | None = None):
        self._path = path or persona_path()
        self._cache: Persona | None = None
        self._mtime: float | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    def load(self) -> Persona:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._cache, self._mtime = Persona(), None
            return self._cache
        if self._cache is not None and mtime == self._mtime:
            return self._cache
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("persona.json unreadable (%s); using defaults", exc)
            data = {}
        fields = {k: v for k, v in data.items() if k in Persona.__dataclass_fields__}
        persona = Persona(**fields) if fields else Persona()
        if not persona.wake_word:
            persona = replace(persona, wake_word=persona.name)
        self._cache, self._mtime = persona, mtime
        return persona

    def save(self, persona: Persona) -> Persona:
        persona = replace(persona, updated_at=time.time())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(persona), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._path)
        self._cache, self._mtime = persona, self._path.stat().st_mtime
        return persona

    def rename(self, name: str, wake_pinyin_: str, wake_word: str | None = None) -> Persona:
        current = self.load()
        return self.save(replace(current, name=name, wake_word=wake_word or name, wake_pinyin=wake_pinyin_))

    def set_avatar(self, appearance: str, spec: dict[str, Any], pack: dict[str, Any]) -> Persona:
        """Record a look the device accepted; the next call announces it once."""
        current = self.load()
        return self.save(replace(current, appearance=appearance, avatar_spec=dict(spec), avatar_pack=dict(pack),
                                 avatar_announce=True))

    def take_avatar_announcement(self) -> str:
        """The one-time 'I have a new look' note for the next voice session ('' when none)."""
        current = self.load()
        if not current.avatar_announce:
            return ""
        self.save(replace(current, avatar_announce=False))
        return current.appearance


_default_store: PersonaStore | None = None


def default_store() -> PersonaStore:
    """Process-wide store; a different STACKCHAN_PERSONA_FILE makes a new one."""
    global _default_store
    path = persona_path()
    if _default_store is None or _default_store.path != path:
        _default_store = PersonaStore(path)
    return _default_store


def current_persona() -> Persona:
    return default_store().load()


def _text_result(result: Any) -> dict[str, Any] | str | None:
    """Device MCP tool results arrive as {"content":[{"type":"text","text":...}]}."""
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        for item in result["content"]:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                try:
                    return json.loads(text)
                except (TypeError, ValueError):
                    return text
    return result


def parse_device_persona(result: Any) -> dict[str, Any] | None:
    data = _text_result(result)
    return data if isinstance(data, dict) else None


def device_set_failed(result: Any) -> str | None:
    """The firmware answers a refused rename with the text "failed: <reason>"."""
    data = _text_result(result)
    if isinstance(data, str) and data.startswith("failed"):
        return data
    if isinstance(data, dict) and data.get("ok") is False:
        return str(data.get("error") or "failed")
    return None


async def rename_on_device(esp32: Any, name: str, pinyin: str) -> tuple[bool, str]:
    """Call self.persona.set_name; returns (ok, error_text)."""
    try:
        result, error = await esp32.call_tool(DEVICE_SET_TOOL, {"name": name, "wake_pinyin": pinyin})
    except Exception as exc:  # pragma: no cover - transport failure
        return False, str(exc)
    if error:
        return False, str(error.get("message", error)) if isinstance(error, dict) else str(error)
    failed = device_set_failed(result)
    if failed:
        return False, failed
    return True, ""


async def sync_device_persona(esp32: Any, store: PersonaStore | None = None) -> str:
    """Reconcile persona.json with a freshly connected device.

    persona.json wins once the user saved one here; before that the device's
    name is adopted so the voice persona matches what the device listens for.
    Returns a short word for the log: pushed / adopted / in_sync / skipped.
    """
    store = store or default_store()
    try:
        result, error = await esp32.call_tool(DEVICE_GET_TOOL, {})
    except Exception as exc:
        logger.warning("persona sync: %s failed: %s", DEVICE_GET_TOOL, exc)
        return "skipped"
    if error:
        logger.info("persona sync skipped: %s", error)
        return "skipped"
    device = parse_device_persona(result)
    if not device:
        return "skipped"
    device_name = normalize_name(device.get("name"))
    device_pinyin = str(device.get("wake_pinyin") or "").strip()
    local = store.load()
    if not store.exists:
        if device_name:
            store.save(replace(local, name=device_name, wake_word=str(device.get("wake_word") or device_name), wake_pinyin=device_pinyin or local.wake_pinyin))
            logger.info("persona sync: adopted device name %s", device_name)
            return "adopted"
        return "in_sync"
    if device_name == local.name and (not device_pinyin or device_pinyin == local.wake_pinyin):
        return "in_sync"
    ok, err = await rename_on_device(esp32, local.name, local.wake_pinyin)
    if ok:
        logger.info("persona sync: pushed name %s to device (was %s)", local.name, device_name)
        return "pushed"
    logger.warning("persona sync: device refused %s: %s", local.name, err)
    return "skipped"
