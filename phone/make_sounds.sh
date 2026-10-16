#!/usr/bin/env bash
# Generates the classic dial tone and a "number not in service" tone.
# Later, replace sounds/not_in_service.wav with a recording in your own voice.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p sounds

ffmpeg -y -loglevel error \
  -f lavfi -i "sine=frequency=350:duration=30" \
  -f lavfi -i "sine=frequency=440:duration=30" \
  -filter_complex "amix=inputs=2,volume=0.6" -ac 1 -ar 44100 sounds/dialtone.wav

ffmpeg -y -loglevel error \
  -f lavfi -i "sine=frequency=914:duration=0.33" \
  -f lavfi -i "sine=frequency=1371:duration=0.33" \
  -f lavfi -i "sine=frequency=1777:duration=0.33" \
  -filter_complex "[0][1][2]concat=n=3:v=0:a=1,volume=0.5" -ac 1 -ar 44100 sounds/not_in_service.wav

# Stutter dial tone: ten quick beeps, played when new messages are waiting
ffmpeg -y -loglevel error \
  -f lavfi -i "aevalsrc=0.3*(sin(2*PI*350*t)+sin(2*PI*440*t))*lt(mod(t\\,0.2)\\,0.1):d=2:s=44100" \
  -ac 1 sounds/stutter.wav

echo "Made sounds/dialtone.wav, sounds/stutter.wav and sounds/not_in_service.wav"
