#!/usr/bin/env python3
"""Build android/src/CallAudio.java into src/ai_phone/callaudio.dex (runs on the phone via app_process).

You only need this if you change CallAudio.java: a prebuilt callaudio.dex is included in the repo,
and ai-phone pushes it to the phone automatically.

Needs JDK 17+ and the Android SDK (a platform of android-33 or newer, and build-tools with d8).
They are found through JAVA_HOME / PATH and ANDROID_HOME / ANDROID_SDK_ROOT, or the usual install
locations on macOS, Linux and Windows.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
OUT = HERE.parent / "src" / "ai_phone" / "callaudio.dex"
WIN = os.name == "nt"


def java_major(javac: Path) -> int:
    try:
        r = subprocess.run([str(javac), "-version"], capture_output=True, text=True)
    except OSError:
        return 0
    m = re.search(r"javac (\d+)(?:\.(\d+))?", r.stdout + r.stderr)
    if not m:
        return 0
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major  # "1.8" -> 8


def find_javac() -> Path:
    exe = "javac.exe" if WIN else "javac"
    cands = []
    if os.environ.get("JAVA_HOME"):
        cands.append(Path(os.environ["JAVA_HOME"]) / "bin" / exe)
    if shutil.which("javac"):
        cands.append(Path(shutil.which("javac")))
    for prefix in ("/opt/homebrew/opt", "/usr/local/opt"):  # Homebrew
        for name in ("openjdk@21", "openjdk@17", "openjdk"):
            cands.append(Path(prefix) / name / "bin" / exe)
    for jvm in (Path("/usr/lib/jvm"), Path("C:/Program Files/Eclipse Adoptium"), Path("C:/Program Files/Java")):
        if jvm.is_dir():
            cands += sorted(jvm.glob(f"*/bin/{exe}"), reverse=True)
    for c in cands:
        if c.exists() and java_major(c) >= 17:
            return c
    sys.exit("JDK 17 or newer not found. Install one (e.g. Temurin 21) and set JAVA_HOME.")


def _version_key(name: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", name))


def find_sdk() -> tuple[Path, Path]:
    """Return (android.jar, d8)."""
    home = Path.home()
    cands = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
             home / "Library/Android/sdk", home / "Android/Sdk",
             Path(os.environ.get("LOCALAPPDATA", home)) / "Android/Sdk",
             "/opt/homebrew/share/android-commandlinetools", "/usr/local/share/android-commandlinetools",
             "/usr/lib/android-sdk", "/opt/android-sdk"]
    d8_name = "d8.bat" if WIN else "d8"
    for c in cands:
        if not c:
            continue
        sdk = Path(c)
        jars = [p for p in (sdk / "platforms").glob("android-*/android.jar")
                if (_version_key(p.parent.name) or (0,))[0] >= 33]
        d8s = list((sdk / "build-tools").glob(f"*/{d8_name}"))
        if jars and d8s:
            jar = max(jars, key=lambda p: _version_key(p.parent.name))
            d8 = max(d8s, key=lambda p: _version_key(p.parent.name))
            return jar, d8
    sys.exit("Android SDK not found (need platforms;android-35 and build-tools;35.0.0). Install the "
             "command-line tools, run sdkmanager for those two packages, and set ANDROID_HOME.")


def run(cmd: list, env=None) -> None:
    r = subprocess.run([str(c) for c in cmd], env=env)
    if r.returncode != 0:
        sys.exit(f"Build step failed: {Path(str(cmd[0])).name}")


def main() -> None:
    javac = find_javac()
    jar, d8 = find_sdk()
    print(f"javac: {javac}\nandroid.jar: {jar}\nd8: {d8}")
    shutil.rmtree(BUILD, ignore_errors=True)
    classes, stubs = BUILD / "classes", BUILD / "stubs"
    classes.mkdir(parents=True)
    stubs.mkdir(parents=True)

    # Stubs for hidden framework classes: compile-only, never dexed (the phone's real classes are used).
    run([javac, "--release", "11", "-nowarn", "-cp", jar, "-d", stubs, *sorted((HERE / "stubs").rglob("*.java"))])
    run([javac, "--release", "11", "-nowarn", "-cp", os.pathsep.join([str(stubs), str(jar)]), "-d", classes,
         *sorted((HERE / "src").glob("*.java"))])

    env = dict(os.environ, JAVA_HOME=str(javac.parent.parent))  # d8 needs a modern JVM too
    env["PATH"] = str(javac.parent) + os.pathsep + env.get("PATH", "")
    run([d8, "--min-api", "33", "--lib", jar, "--classpath", stubs, "--output", BUILD,
         *sorted(classes.rglob("*.class"))], env=env)
    shutil.move(str(BUILD / "classes.dex"), OUT)
    print(f"built {OUT.relative_to(HERE.parent)} (ai-phone pushes it to the phone on every run)")


if __name__ == "__main__":
    main()
