#!/usr/bin/env bash
# Turns voice notes in ./raw into clean, volume-matched clips in ./clips.
# Name each raw file starting with the number she should dial:
#   raw/1993 amma.opus   ->  clips/1993.wav
#   raw/1993 appa.m4a    ->  clips/1993_2.wav
# Converted files move to raw/done so re-running never duplicates them.
# A file identical to one already converted (even under another name) is
# skipped and moved to raw/duplicates.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p raw/done raw/duplicates clips

hash_of() { md5sum "$1" | cut -d' ' -f1; }

# Fingerprints of everything already converted
declare -A seen
shopt -s nullglob
for d in raw/done/*; do
  [ -f "$d" ] && seen[$(hash_of "$d")]=1
done

for f in raw/*; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  num=$(echo "$name" | grep -oE '^[0-9]+' || true)
  if [ -z "$num" ]; then
    echo "Skipping '$name' (name must start with the year, e.g. 1998)"
    continue
  fi
  h=$(hash_of "$f")
  if [ -n "${seen[$h]:-}" ]; then
    mv "$f" raw/duplicates/
    echo "Duplicate, skipped: $name"
    continue
  fi
  out="clips/$num.wav"; i=2
  while [ -e "$out" ]; do out="clips/${num}_$i.wav"; i=$((i+1)); done
  ffmpeg -nostdin -y -loglevel error -i "$f" \
    -af "silenceremove=start_periods=1:start_threshold=-50dB,loudnorm=I=-16:TP=-1.5:LRA=11" \
    -ac 1 -ar 44100 "$out"
  mv "$f" raw/done/
  seen[$h]=1
  echo "$name -> $out"
done
echo "Done."
