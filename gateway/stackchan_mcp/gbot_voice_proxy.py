# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Opt-in Grok Bot voice, retaining the established device audio transport."""

from __future__ import annotations

from .gbot_voice_bridge import GbotVoiceBridge
from .gemini_voice_proxy import GeminiVoiceProxy


class GbotVoiceProxy(GeminiVoiceProxy):
    def _voice_api_key(self):
        return None

    def _voice_requires_key(self):
        return False

    def _voice_requires_device(self):
        return False

    def _voice_hello(self):
        return {"provider": "gbot_voice", "experimental": True}

    def _speak_waits_for_first_audio(self):
        return True

    def _voice_tts_mode(self):
        return "native"

    def _make_bridge(self, esp32, **kwargs):
        kwargs.setdefault("on_user_text", self._handle_user_transcript)
        return (self.bridge_factory or GbotVoiceBridge)(
            esp32=esp32,
            tool_executor=self.voice_tool_executor,
            tool_executor_factory=self.voice_tool_executor_factory,
            tool_extensions=(*self.voice_tool_extensions, *self._music_extensions()),
            **kwargs,
        )
