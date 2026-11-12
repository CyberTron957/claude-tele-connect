#!/bin/bash
# Tests the Linux Bluetooth-mode audio path (pactl/parec/pacat) in a Debian container with PulseAudio.
# A virtual sink and a sine-wave source, named like a phone's Bluetooth nodes, stand in for the phone.
# Needs Docker. Usage: tests/linux_audio_test.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
docker run --rm -e HOME=/root -v "$ROOT":/app:ro debian:bookworm-slim bash -c '
set -e
apt-get update -qq >/dev/null && apt-get install -y -qq pulseaudio pulseaudio-utils python3 python3-numpy >/dev/null 2>&1
pulseaudio -D --exit-idle-time=-1 2>/dev/null
for i in $(seq 20); do pactl info >/dev/null 2>&1 && break; sleep 0.5; done
pactl unload-module module-suspend-on-idle
pactl load-module module-null-sink sink_name=bluez_output.AA_BB_CC_DD_EE_FF.1 sink_properties=device.description=Galaxy_Test_Hands-Free rate=16000 >/dev/null
pactl load-module module-sine-source source_name=bluez_input.AA_BB_CC_DD_EE_FF.0 frequency=1000 rate=16000 >/dev/null
python3 /app/tests/linux_audio_check.py'
