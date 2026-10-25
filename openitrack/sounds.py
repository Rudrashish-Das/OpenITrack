"""Short confirmation beeps, so shortcuts give feedback while a game is fullscreen."""

from __future__ import annotations

import queue
import sys
import threading

if sys.platform == "win32":
    import winsound

# (frequency Hz, duration ms) sequences.
TONES = {
    "on": [(880, 70)],
    "off": [(440, 70)],
    "recenter": [(660, 50)],
    "step": [(990, 50)],
    "up": [(700, 40), (900, 40)],
    "down": [(900, 40), (700, 40)],
    "done": [(660, 70), (880, 70), (1100, 90)],
    "error": [(250, 220)],
}


class Sounds:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.platform == "win32"
        self._q: queue.Queue[str] = queue.Queue(maxsize=8)
        self._thread: threading.Thread | None = None

    def play(self, name: str) -> None:
        if not self.enabled or name not in TONES:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="sounds")
            self._thread.start()
        try:
            self._q.put_nowait(name)
        except queue.Full:
            pass  # don't let a key-mashing user build up a backlog of beeps

    def _run(self) -> None:
        while True:
            for freq, ms in TONES[self._q.get()]:
                try:
                    winsound.Beep(freq, ms)
                except RuntimeError:
                    return  # no sound device
