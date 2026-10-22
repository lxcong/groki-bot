# SPDX-License-Identifier: MIT
"""Optional persona file shared by every voice backend.

``STACKCHAN_PERSONALITY_FILE`` names the file; otherwise ``personality.md``
next to the gateway package is used when it exists. Gemini appends the text
under a "# Personality" heading; Grok passes it as the call description,
which its harness treats as who the agent is and how it speaks.
"""

from __future__ import annotations

import os
from pathlib import Path


def personality_path() -> Path:
    raw = os.getenv("STACKCHAN_PERSONALITY_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[1] / "personality.md"


def load_personality_template() -> str:
    """The persona file as written, placeholders included ('' when missing)."""
    try:
        return personality_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def load_personality_file() -> str:
    """Read the personality file if it exists, otherwise return ''.

    ``{{name}}``, ``{{wake_word}}`` and ``{{appearance}}`` are filled from
    persona.json (stackchan_mcp.persona), so renaming the assistant in a
    conversation changes who the next call speaks as.
    """
    template = load_personality_template()
    if not template:
        return ""
    from .persona import current_persona  # noqa: PLC0415

    return current_persona().render(template).strip()
