"""The persona file is shared by the Gemini and Grok voice backends."""

from stackchan_mcp import gemini_live_bridge
from stackchan_mcp.personality import load_personality_file, personality_path


def test_env_overrides_default_path(monkeypatch, tmp_path):
    path = tmp_path / "persona.md"
    path.write_text("  一只小白兔  \n", encoding="utf-8")
    monkeypatch.setenv("STACKCHAN_PERSONALITY_FILE", str(path))
    assert personality_path() == path
    assert load_personality_file() == "一只小白兔"


def test_missing_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("STACKCHAN_PERSONALITY_FILE", str(tmp_path / "none.md"))
    assert load_personality_file() == ""


def test_default_path_is_gateway_personality_md(monkeypatch):
    monkeypatch.delenv("STACKCHAN_PERSONALITY_FILE", raising=False)
    assert personality_path().name == "personality.md"
    assert personality_path().parent.name == "gateway"
    assert gemini_live_bridge._personality_path() == personality_path()
