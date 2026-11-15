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


# ------------------------------------------------------------------ agent-friendly setup commands

def test_agent_commands():
    import io
    import json
    from contextlib import redirect_stdout
    from claude_tele_connect import cli
    def call(*args, stdin=""):
        sys.stdin = io.StringIO(stdin)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(args))
        return code, buf.getvalue()

    # options save only what's given: setting the owner must not reset the provider
    call("setup", "--provider", "openai")
    call("setup", "--owner", "Ana")
    assert os.environ["CLAUDE_TELE_CONNECT_PROVIDER"] == "openai" and config.OWNER_NAME == "Ana"
    # keys from stdin are saved and never printed
    code, out = call("key", "openai", "--stdin", stdin="sk-secret-xyz\n")
    assert code == 0 and "sk-secret-xyz" not in out and config.OPENAI_API_KEY == "sk-secret-xyz", out
    code, out = call("setup", "--api-key", "AIza-secret")
    assert "AIza-secret" not in out and "(hidden)" in out, out
    # status names the next step
    from claude_tele_connect import setupcmds
    st = {"you": {"done": True}, "voice": {"chosen": True, "done": False, "missing_keys": ["XAI_API_KEY"], "problems": []},
          "adb": {"done": True}, "phone": {"done": True}, "audio": {"confirmed": False}}
    assert setupcmds.next_step(st)["step"] == "keys"
    st["voice"].update(missing_keys=[], done=True)
    assert setupcmds.next_step(st)["step"] == "check"
    st["audio"]["confirmed"] = True
    assert setupcmds.next_step(st)["step"] == "done"
    assert setupcmds.resolve_key_names(["grok", "ElevenLabs"]) == ["XAI_API_KEY", "ELEVENLABS_API_KEY"]
    print("ok  agent setup commands (setup options, key --stdin, status next step)")


test_agent_commands()


def test_guides_ship_with_the_tool():
    import contextlib, io
    from claude_tele_connect import cli
    for topic, needle in (("setup", "status --json"), ("call", "Ask once")):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert cli.main(["guide", topic]) == 0
        assert needle in buf.getvalue(), topic
    print("ok  agent guides ship inside the tool")


test_guides_ship_with_the_tool()
