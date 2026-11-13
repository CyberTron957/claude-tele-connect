"""Setup wizard steps with scripted answers (no phone, no network).

  python tests/test_onboard.py
"""
import builtins
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ["CLAUDE_TELE_CONNECT_HOME"] = tempfile.mkdtemp()
for k in list(os.environ):
    if k.startswith("CLAUDE_TELE_CONNECT_") and k != "CLAUDE_TELE_CONNECT_HOME":
        os.environ.pop(k)

from claude_tele_connect import config, onboard


def answers(*vals):
    it = iter(vals)
    builtins.input = lambda prompt="": next(it)
    onboard.getpass.getpass = lambda prompt="": next(it)


def test_you():
    answers("", "Sam Lee", "", "+91")      # empty name is asked again
    assert onboard.step_you()
    env = config.ENV_FILE.read_text()
    assert "CLAUDE_TELE_CONNECT_OWNER=Sam Lee" in env and "COUNTRY_CODE=91" in env, env
    assert "PRONOUNS" not in env
    assert config.OWNER_NAME == "Sam Lee"   # applied to the running process too
    answers("", "she/her", "")              # rerun: Enter keeps the saved values
    assert onboard.step_you()
    assert config.OWNER_NAME == "Sam Lee" and config.OWNER_PRONOUNS == "she/her" and config.COUNTRY_CODE == "91"
    print("ok  about-you step saves and keeps values")


def test_voice_openai_without_test():
    answers("2", "sk-test", "n")            # OpenAI, key, skip the live voice test
    assert onboard.step_voice()
    assert config.OPENAI_API_KEY == "sk-test" and os.environ["CLAUDE_TELE_CONNECT_PROVIDER"] == "openai"
    answers("1", "n", "my-project", "n")    # Gemini via Vertex clears any Gemini API key
    os.environ["CLAUDE_TELE_CONNECT_GEMINI_API_KEY"] = "old"
    assert onboard.step_voice()
    assert config.GCP_PROJECT == "my-project" and not config.GEMINI_API_KEY
    if os.name != "nt":
        assert oct(config.ENV_FILE.stat().st_mode)[-3:] == "600"
    print("ok  voice step saves provider and keys, file is private")


def test_choose_accepts_names():
    answers("9", "openai")
    assert onboard.choose("x", [("gemini", "G"), ("openai", "O")]) == "openai"
    print("ok  menus accept numbers or names")


if __name__ == "__main__":
    test_you()
    test_voice_openai_without_test()
    test_choose_accepts_names()
