#!/usr/bin/env python3
"""
Memory Phone: lift the receiver, dial a year, hear a voice note.

On the Pi:        python3 phone.py
On your Mac:      python3 phone.py --sim   (keyboard pretends to be the dial)

Clips live in ./clips and are named by the number that plays them:
    1993.wav          plays when she dials 1993
    1993_2.wav        plays straight after 1993.wav
    20210612.wav      a full date works too
Special sounds live in ./sounds:
    dialtone.wav        loops while she's holding the receiver
    not_in_service.wav  plays when nothing matches
New messages: when something she hasn't heard arrives, lifting the receiver
gives a stuttering dial tone (like old voicemail). Dial * to hear everything
new, oldest first. With nothing new, * plays the newest clip. # always plays
the newest clip.

After loading clips by hand, run  python3 phone.py --mark-all-heard
so they don't count as new.
"""

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CLIPS_DIR = BASE / "clips"
SOUNDS_DIR = BASE / "sounds"
HEARD_FILE = BASE / ".heard.json"

# ---------------- Settings you may need to tweak ----------------
HOOK_PIN = 17               # BCM numbering = physical pin 11
PULSE_PIN = 22              # BCM numbering = physical pin 15
LATEST_PIN = None           # this phone's #/R is on the dial (12 pulses), so no extra button wire
HOOK_CLOSED_MEANS_LIFTED = True   # flip if lift/hang up come out reversed
PULSE_ON_OPEN = False       # this dial closes the contact per pulse. Flip if digits are wrong
DIGIT_GAP = 0.3             # seconds of quiet that ends one digit
NUMBER_GAP = 4.0            # seconds of quiet that ends the whole number
PULSE_BOUNCE = 0.01         # debounce for the dial contact
AUDIO_DEVICE = os.environ.get("PHONE_AUDIO_DEVICE", "plughw:CARD=Device,DEV=0")  # USB sound card
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".aiff")
HEARD_AFTER = 3.0           # seconds of a clip played before it counts as heard
# ----------------------------------------------------------------


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class _FakeProc:
    """Stands in for audio playback when no player is installed (testing only)."""

    def __init__(self, seconds=1.5):
        self._done = threading.Event()
        threading.Timer(seconds, self._done.set).start()

    def poll(self):
        return 0 if self._done.is_set() else None

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return 0

    def terminate(self):
        self._done.set()

    kill = terminate


class Player:
    """Plays one file at a time. Starting a new file stops the old one."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.is_mac = platform.system() == "Darwin" and shutil.which("afplay")
        self.has_aplay = bool(shutil.which("aplay"))

    def _command(self, path):
        if self.is_mac:
            return ["afplay", str(path)]
        if self.has_aplay and path.suffix.lower() == ".wav":
            return ["aplay", "-q", "-D", AUDIO_DEVICE, str(path)]
        return None

    def _kill(self):
        p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(1)
            except Exception:
                p.kill()
        self.proc = None

    def play(self, path, guard=lambda: True):
        """Blocks until the file finishes or is stopped. guard() is checked just
        before starting, so a stale thread can never interrupt a newer sound."""
        with self.lock:
            if not guard():
                return
            self._kill()
            cmd = self._command(path)
            if cmd is None:
                log(f"  [playing] {path.name}")
                self.proc = _FakeProc()
            else:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            proc = self.proc
        proc.wait()

    def stop(self):
        with self.lock:
            self._kill()


def _order(path, number):
    rest = path.stem[len(number):].lstrip("_")
    return int(rest) if rest.isdigit() else 0


def all_clips():
    CLIPS_DIR.mkdir(exist_ok=True)
    return [p for p in CLIPS_DIR.iterdir()
            if p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")]


class Heard:
    """Remembers which clips she has already listened to."""

    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.names = set(json.loads(HEARD_FILE.read_text()))
        except FileNotFoundError:
            # First run: everything already loaded counts as heard
            self.names = {p.name for p in all_clips()}
            self._save()
        except Exception:
            self.names = set()

    def _save(self):
        tmp = HEARD_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self.names)))
        tmp.replace(HEARD_FILE)

    def mark(self, path):
        with self.lock:
            if path.name not in self.names:
                self.names.add(path.name)
                self._save()

    def mark_all(self):
        with self.lock:
            self.names = {p.name for p in all_clips()}
            self._save()

    def unheard(self):
        with self.lock:
            new = [p for p in all_clips() if p.name not in self.names]
        return sorted(new, key=lambda p: p.stat().st_mtime)


HEARD = None   # set in main()


def find_clips(number):
    audio = all_clips()
    if number == "*" and HEARD is not None:
        new = HEARD.unheard()
        if new:
            return new
    if number in ("*", "#"):
        return [max(audio, key=lambda p: p.stat().st_mtime)] if audio else []
    if not number.isdigit():
        return []
    matches = [p for p in audio if p.stem == number or p.stem.startswith(number + "_")]
    return sorted(matches, key=lambda p: _order(p, number))


class Phone:
    def __init__(self, player):
        self.player = player
        self.lock = threading.RLock()
        self.off_hook = False
        self.pulses = 0
        self.last_pulse = 0.0
        self.digits = ""
        self.last_digit = 0.0
        self.busy = False       # a clip is playing
        self.session = 0        # bumps on every lift and hang-up, cancelling old threads
        threading.Thread(target=self._watch, daemon=True).start()

    # ---- events from hardware (or the keyboard) ----
    def lift(self):
        with self.lock:
            if self.off_hook:
                return
            self.off_hook = True
            self.session += 1
            self._reset()
            s = self.session
        log("Receiver lifted")
        self._dialtone(s)

    def hang_up(self):
        with self.lock:
            if not self.off_hook:
                return
            self.off_hook = False
            self.session += 1
            self._reset()
        self.player.stop()
        log("Hung up")

    def pulse(self):
        with self.lock:
            if not self.off_hook:
                return
            if self.busy:
                # Dialing mid-message cuts it off and starts a fresh number
                self.session += 1
                self._reset()
                log("Message interrupted by dialing")
            self.pulses += 1
            self.last_pulse = time.monotonic()
        # silence the dial tone without holding up the next pulse
        threading.Thread(target=self.player.stop, daemon=True).start()

    def latest(self):
        with self.lock:
            if not self.off_hook:
                return
            self.session += 1   # cancels anything already playing
            self._reset()
            self.busy = True
            s = self.session
        log("Newest message requested")
        threading.Thread(target=self._play_number, args=("0", s), daemon=True).start()

    # ---- internals ----
    def _reset(self):
        self.pulses = 0
        self.digits = ""
        self.busy = False

    def _dialing(self):
        return self.pulses > 0 or self.digits != ""

    def _dialtone(self, s):
        tone = SOUNDS_DIR / "dialtone.wav"
        stutter = SOUNDS_DIR / "stutter.wav"
        if not tone.exists():
            return

        def ok():
            return self.session == s and self.off_hook and not self.busy and not self._dialing()

        def loop():
            if stutter.exists() and HEARD is not None and HEARD.unheard():
                log("New messages waiting: stutter tone")
                self.player.play(stutter, guard=ok)
            while ok():
                self.player.play(tone, guard=ok)

        threading.Thread(target=loop, daemon=True).start()

    def _watch(self):
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            start = None
            with self.lock:
                if self.pulses and now - self.last_pulse > DIGIT_GAP:
                    if self.pulses <= 12:
                        # 1-9 as dialled, 10 = 0, 11 = *, 12 = # (this dial has * and #/R holes)
                        digit = {10: "0", 11: "*", 12: "#"}.get(self.pulses, str(self.pulses))
                        self.digits += digit
                        log(f"Digit {digit}   (dialed so far: {self.digits})")
                    else:
                        log(f"Ignored noisy dial read ({self.pulses} pulses)")
                    self.pulses = 0
                    self.last_digit = now
                if self.digits and not self.pulses and now - self.last_digit > NUMBER_GAP:
                    start = (self.digits, self.session)
                    self.digits = ""
                    self.busy = True
            if start:
                threading.Thread(target=self._play_number, args=start, daemon=True).start()

    def _play_number(self, number, s):
        clips = find_clips(number)
        if clips:
            log(f"Number {number}: playing {', '.join(c.name for c in clips)}")
        else:
            log(f"Number {number}: nothing here")
            nis = SOUNDS_DIR / "not_in_service.wav"
            clips = [nis] if nis.exists() else []

        def still():
            return self.session == s and self.off_hook

        for clip in clips:
            if not still():
                return
            t0 = time.monotonic()
            self.player.play(clip, guard=still)
            played = time.monotonic() - t0
            if HEARD is not None and clip.parent == CLIPS_DIR and (still() or played >= HEARD_AFTER):
                HEARD.mark(clip)
        with self.lock:
            if self.session != s:
                return
            self.busy = False
        self._dialtone(s)  # ready for another number


def run_hardware(phone):
    from gpiozero import Button

    hook = Button(HOOK_PIN, pull_up=True, bounce_time=0.05)

    def hook_changed():
        lifted = hook.is_pressed if HOOK_CLOSED_MEANS_LIFTED else not hook.is_pressed
        phone.lift() if lifted else phone.hang_up()

    hook.when_pressed = hook_changed
    hook.when_released = hook_changed

    # The dial is polled at 1 kHz instead of using edge callbacks: a contact has
    # to hold steady for 4 ms to count, which filters out bounce reliably.
    dial = Button(PULSE_PIN, pull_up=True, bounce_time=None)

    def poll_dial():
        stable = dial.is_pressed
        cand, since = stable, time.monotonic()
        open_since = None
        while True:
            v = dial.is_pressed
            now = time.monotonic()
            if v != cand:
                cand, since = v, now
            elif v != stable and now - since >= 0.004:
                stable = v
                if stable != PULSE_ON_OPEN:
                    gap = now - open_since if open_since else 9
                    if 0.08 <= gap <= 0.2:
                        phone.pulse()  # this dial sometimes skips a contact; the long gap gives it away
                    phone.pulse()
                else:
                    open_since = now
            time.sleep(0.001)

    threading.Thread(target=poll_dial, daemon=True).start()

    extra = None
    if LATEST_PIN is not None:
        extra = Button(LATEST_PIN, pull_up=True, bounce_time=0.05)
        extra.when_pressed = phone.latest

    hook_changed()  # pick up the starting state
    log("Memory Phone ready")
    signal.pause()


def run_sim(phone):
    print(
        "\nPretend phone. Type a command and press Enter:\n"
        "  u      lift the receiver\n"
        "  d      hang up\n"
        "  1998   dial a number (one pulse per digit, like a real dial)\n"
        "  *      new messages (or the newest if nothing is new)\n"
        "  #      dial the #/R hole (newest message)\n"
        "  q      quit\n"
    )
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd in ("u", "up"):
            phone.lift()
        elif cmd in ("d", "down"):
            phone.hang_up()
        elif cmd == "l":
            phone.latest()
        elif cmd and all(c in "0123456789*#" for c in cmd):
            for ch in cmd:
                for _ in range({"0": 10, "*": 11, "#": 12}.get(ch) or int(ch)):
                    phone.pulse()
                    time.sleep(0.06)
                time.sleep(DIGIT_GAP + 0.2)
        elif cmd in ("q", "quit", "exit"):
            break
        elif cmd:
            print("Unknown command")
    phone.player.stop()


def main():
    parser = argparse.ArgumentParser(description="Memory Phone")
    parser.add_argument("--sim", action="store_true", help="use the keyboard instead of GPIO")
    parser.add_argument("--mark-all-heard", action="store_true",
                        help="count every clip on the phone as already heard, then exit")
    args = parser.parse_args()
    CLIPS_DIR.mkdir(exist_ok=True)
    SOUNDS_DIR.mkdir(exist_ok=True)
    global HEARD
    HEARD = Heard()
    if args.mark_all_heard:
        HEARD.mark_all()
        print(f"Marked {len(HEARD.names)} clips as heard. Restart the phone: sudo systemctl restart phone")
        return
    phone = Phone(Player())
    if args.sim:
        run_sim(phone)
    else:
        run_hardware(phone)


if __name__ == "__main__":
    main()
