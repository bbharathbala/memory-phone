#!/usr/bin/env python3
"""
Pulls new messages from the Memory Phone server into ./clips.

Each message is saved as <year>_<100000 + id>.wav, so it plays after any clips
you loaded by hand for that year and never overwrites them. A message you delete
on the admin page is removed from the phone on the next sync.

Settings come from sync.env (see DEPLOY.md):
  SERVER_URL=https://memory-phone-xxx.fly.dev
  PHONE_KEY=...

Run once by hand to test:  set -a; source sync.env; set +a; python3 sync.py
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CLIPS = BASE / "clips"
STATE = BASE / ".synced.json"
SERVER = os.environ.get("SERVER_URL", "").rstrip("/")
KEY = os.environ.get("PHONE_KEY", "")
OFFSET = 100000


def log(msg):
    print(msg, flush=True)


def get(path):
    req = urllib.request.Request(SERVER + path, headers={"X-Phone-Key": KEY})
    return urllib.request.urlopen(req, timeout=90)   # a sleeping server takes a few seconds to wake


def load_state():
    try:
        return {int(k): v for k, v in json.loads(STATE.read_text()).items()}
    except Exception:
        return {}


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def main():
    if not SERVER or not KEY:
        sys.exit("SERVER_URL and PHONE_KEY must be set (see DEPLOY.md)")
    CLIPS.mkdir(exist_ok=True)
    state = load_state()   # message id -> file name on the phone

    try:
        with get("/api/phone/messages") as r:
            data = json.load(r)
    except Exception as e:
        log(f"Could not reach the server: {e}")
        return 1
    if not data.get("ok"):
        log("Server answered without ok; skipping this round")
        return 1

    remote = {int(m["id"]): m for m in data["messages"]}

    added = 0
    for mid, m in sorted(remote.items()):
        if mid in state and (CLIPS / state[mid]).exists():
            continue
        name = f"{int(m['year'])}_{OFFSET + mid}.wav"
        tmp = CLIPS / f".{name}.part"
        try:
            with get(f"/api/phone/audio/{mid}") as r, open(tmp, "wb") as f:
                while chunk := r.read(65536):
                    f.write(chunk)
            tmp.replace(CLIPS / name)   # appears in one step, so the phone never plays half a file
        except Exception as e:
            tmp.unlink(missing_ok=True)
            log(f"Download of message {mid} failed: {e}")
            continue
        state[mid] = name
        save_state(state)
        added += 1
        log(f"New message from {m['name']} for {m['year']} -> {name}")

    # Remove messages that were deleted on the admin page.
    # Safety: an empty list while we hold several messages is more likely a server
    # problem than you deleting everything, so leave the phone alone in that case.
    removed = 0
    if remote or len(state) <= 3:
        for mid in [i for i in state if i not in remote]:
            (CLIPS / state[mid]).unlink(missing_ok=True)
            log(f"Removed deleted message {mid} ({state[mid]})")
            del state[mid]
            removed += 1
        save_state(state)

    log(f"Sync done: {added} new, {removed} removed, {len(state)} from the server on the phone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
