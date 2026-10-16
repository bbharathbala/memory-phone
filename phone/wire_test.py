#!/usr/bin/env python3
"""
Wire finder: works out which phone wires are the hook switch and the dial.

Connect one phone wire to GPIO17 (pin 11), GPIO27 (pin 13) or GPIO22 (pin 15),
and the other wire of the same pair to GND (pin 9 or 14). Then run:

    python3 wire_test.py

Lift and replace the receiver, spin the dial, press #/R, and watch.
  - A pair that flips once when you lift/hang up  -> hook switch
  - A pair that clicks N times when you dial N    -> dial pulse contact
  - A pair that closes for the whole spin          -> off-normal (not needed)
"""

import threading
import time
from signal import pause

from gpiozero import Button

PINS = {17: "pin 11", 27: "pin 13", 22: "pin 15"}
BURST_GAP = 0.4

counts = {p: 0 for p in PINS}
last = {p: 0.0 for p in PINS}
lock = threading.Lock()


def report():
    while True:
        time.sleep(0.05)
        now = time.monotonic()
        with lock:
            for p in PINS:
                if counts[p] and now - last[p] > BURST_GAP:
                    if counts[p] > 1:
                        print(f"  GPIO{p}: burst of {counts[p]} opens (looks like dial pulses)")
                    counts[p] = 0


def make(pin):
    b = Button(pin, pull_up=True, bounce_time=0.005)

    def closed():
        print(f"{time.strftime('%H:%M:%S')}  GPIO{pin} ({PINS[pin]}) CLOSED")

    def opened():
        print(f"{time.strftime('%H:%M:%S')}  GPIO{pin} ({PINS[pin]}) OPEN")
        with lock:
            counts[pin] += 1
            last[pin] = time.monotonic()

    b.when_pressed = closed
    b.when_released = opened
    state = "CLOSED" if b.is_pressed else "OPEN"
    print(f"GPIO{pin} ({PINS[pin]}) starts {state}")
    return b


buttons = [make(p) for p in PINS]
threading.Thread(target=report, daemon=True).start()
print("\nWatching. Lift the receiver, spin the dial, press buttons. Ctrl+C to stop.\n")
pause()
